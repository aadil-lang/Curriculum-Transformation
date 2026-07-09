from __future__ import annotations

import logging
import re
import shutil
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Any, Literal

import fitz

from config import Settings, get_settings
from parsers.base import ParsedDocument


LOGGER = logging.getLogger(__name__)
PdfOcrMode = Literal["off", "auto", "always"]
# Default option tracks for PFEQ-style progression matrices (override via PROGRESSION_MATRIX_OPTION_TRACKS)
DEFAULT_OPTION_TRACKS = ("CST", "TS", "S")
# Marker geometry is derived per page from the detected option-track column so the
# detector adapts to tables placed at different x positions; these are only the
# relative widths/tolerances of the placement-symbol column, not absolute page coords.
MARKER_COLUMN_WIDTH = 45.0
MARKER_COLUMN_GAP = 2.0
MARKER_TRACK_TOLERANCE = 14.0
ITEM_MARKER_MAX_DISTANCE = 60.0
MIN_OPTION_LABELS_FOR_MATRIX = 6


def _line_text(line: dict[str, Any]) -> str:
    """Reconstruct a line's text faithfully from its spans.

    PyMuPDF often splits a single visual word into several spans (justified text,
    kerning, style ticks). Joining spans with a blank space corrupts words into
    `Ar ithm etic`. Instead, insert a space only when there is a real horizontal
    gap between consecutive spans, and never strip in-span characters, so spaces,
    symbols, and notation the source actually contains are preserved verbatim.
    """
    parts: list[str] = []
    prev_x1: float | None = None
    for span in line.get("spans", []):
        text = span.get("text", "")
        if not text:
            continue
        bbox = span.get("bbox", (0.0, 0.0, 0.0, 0.0))
        x0, x1 = float(bbox[0]), float(bbox[2])
        char_width = (x1 - x0) / max(1, len(text))
        if prev_x1 is not None:
            gap = x0 - prev_x1
            already_spaced = (parts and parts[-1][-1:].isspace()) or text[:1].isspace()
            if not already_spaced and gap > 0.25 * char_width:
                parts.append(" ")
        parts.append(text)
        prev_x1 = x1
    return "".join(parts).rstrip()


def _block_text(block: dict[str, Any]) -> tuple[str, float]:
    lines: list[str] = []
    font_sizes: list[float] = []

    for line in block.get("lines", []):
        for span in line.get("spans", []):
            if span.get("text", "").strip():
                font_sizes.append(float(span.get("size", 0.0)))
        joined = _line_text(line)
        if joined.strip():
            lines.append(joined)

    average_font_size = sum(font_sizes) / len(font_sizes) if font_sizes else 0.0
    return "\n".join(lines).strip(), average_font_size


def _page_dict_to_markdown(page_index: int, page_dict: dict[str, Any]) -> str:
    text_blocks = [
        block
        for block in page_dict.get("blocks", [])
        if block.get("type") == 0 and block.get("lines")
    ]
    font_samples: list[float] = []
    for block in text_blocks:
        _, block_font_size = _block_text(block)
        if block_font_size:
            font_samples.append(block_font_size)
    font_baseline = median(font_samples) if font_samples else 12.0

    sorted_blocks = sorted(text_blocks, key=lambda block: (block["bbox"][1], block["bbox"][0]))
    markdown_blocks: list[str] = [f"# Page {page_index}"]
    for block in sorted_blocks:
        text, block_font_size = _block_text(block)
        if not text:
            continue
        if block_font_size >= font_baseline * 1.2:
            markdown_blocks.append(f"## {text}")
        else:
            markdown_blocks.append(text)

    return "\n\n".join(markdown_blocks)


def _collect_positioned_spans(page: fitz.Page) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "").strip()
                if not text:
                    continue
                x0, y0, x1, y1 = span["bbox"]
                spans.append(
                    {
                        "text": text,
                        "x0": float(x0),
                        "y0": float(y0),
                        "x1": float(x1),
                        "y1": float(y1),
                        "cy": (float(y0) + float(y1)) / 2.0,
                    }
                )
    return spans


def _discover_option_tracks(spans: list[dict[str, Any]]) -> tuple[str, ...]:
    """Auto-discover candidate option tracks from page text.
    
    Looks for repeated short uppercase labels that appear in a vertical column pattern,
    typically 2-4 characters, repeated at least 6 times. This catches CST/TS/S, Academic/
    Applied, or any similar curriculum-differentiation vocabulary without configuration.
    """
    from collections import Counter
    
    # Short uppercase spans on the right half of the page, likely column headers
    candidates: Counter[str] = Counter()
    for span in spans:
        text = span["text"].strip()
        if not text or len(text) > 10:
            continue
        if span["x0"] < 400:  # Too far left to be an option column
            continue
        # Look for all-caps or title-case short labels
        if text.isupper() or (text[0].isupper() and len(text) <= 8):
            candidates[text] += 1
    
    # Find clusters that repeat at least MIN_OPTION_LABELS_FOR_MATRIX times
    tracks = [
        label
        for label, count in candidates.items()
        if count >= MIN_OPTION_LABELS_FOR_MATRIX
        and 2 <= len(label) <= 8
        and not label.isdigit()
    ]
    
    # Sort by frequency descending, then alphabetically for stability
    tracks.sort(key=lambda t: (-candidates[t], t))
    return tuple(tracks[:5])  # Cap at 5 tracks max to avoid noise


def _matrix_geometry(
    page: fitz.Page, spans: list[dict[str, Any]], option_tracks: tuple[str, ...]
) -> dict[str, float] | None:
    """Derive the option-track column position from the page itself.

    Rather than assuming fixed pixel coordinates, locate where the option-track labels
    (CST/TS/S or other configured tracks) actually sit on this page and express the
    placement-marker column relative to them. This lets the detector adapt to tables
    shifted horizontally, on different pages, or in documents with the same structure
    but a different layout or vocabulary.

    The left edge of the option column is taken from the repeated coloured option cells
    (vector rects that recur once per track row), not from the label text, because the
    cells extend further left than the glyphs and are the true marker/cell boundary.
    """
    track_spans = [span for span in spans if span["text"] in option_tracks]
    if len(track_spans) < MIN_OPTION_LABELS_FOR_MATRIX:
        return None

    label_x0 = median(span["x0"] for span in track_spans)
    # Coloured option cells cluster tightly just left of the label glyphs.
    cell_counts: Counter[int] = Counter()
    for drawing in page.get_drawings():
        rect = fitz.Rect(drawing["rect"])
        if not (6 <= rect.height <= 26 and 8 <= rect.width <= 60):
            continue
        if not (label_x0 - 45 <= rect.x0 <= label_x0 + 5):
            continue
        cell_counts[round(rect.x0)] += 1
    if not cell_counts:
        return None
    max_count = max(cell_counts.values())
    if max_count < 4:
        return None
    # Left edge of the option-cell column (smallest x0 among the dominant cluster).
    cell_left = float(min(x for x, c in cell_counts.items() if c >= max(4, max_count // 2)))
    return {
        "option_x0": cell_left,
        "marker_x_min": cell_left - MARKER_COLUMN_WIDTH,
        "marker_x_max": cell_left - MARKER_COLUMN_GAP,
    }


def _marker_drawings(page: fitz.Page, geometry: dict[str, float]) -> list[fitz.Rect]:
    marker_x_min = geometry["marker_x_min"]
    marker_x_max = geometry["marker_x_max"]
    option_x0 = geometry["option_x0"]
    markers: list[fitz.Rect] = []
    for drawing in page.get_drawings():
        rect = fitz.Rect(drawing["rect"])
        if not (marker_x_min <= rect.x0 <= marker_x_max):
            continue
        # Placement symbols start left of the option-track label column; colored cells do not.
        if rect.x0 >= option_x0 - 0.5:
            continue
        # Skip full-width rules and large table cell borders.
        if rect.height < 5 and rect.width > 30:
            continue
        if rect.width > 50 or rect.height > 26:
            continue
        if rect.width < 8 or rect.height < 6:
            continue
        markers.append(rect)
    return markers


def _arrow_line_drawings(page: fitz.Page, geometry: dict[str, float]) -> list[fitz.Rect]:
    """Horizontal arrow markers in the placement column (vector lines, not text)."""
    marker_x_min = geometry["marker_x_min"]
    option_x0 = geometry["option_x0"]
    arrows: list[fitz.Rect] = []
    for drawing in page.get_drawings():
        rect = fitz.Rect(drawing["rect"])
        if rect.height > 2:
            continue
        if not (15 <= rect.width <= 55):
            continue
        if not (marker_x_min <= rect.x0 < option_x0 - 0.5):
            continue
        arrows.append(rect)
    return arrows


def _placement_markers(page: fitz.Page, geometry: dict[str, float]) -> list[fitz.Rect]:
    return _marker_drawings(page, geometry) + _arrow_line_drawings(page, geometry)


def _option_track_triples(option_rows: list[dict[str, Any]]) -> list[dict[str, dict[str, Any]]]:
    triples: list[dict[str, dict[str, Any]]] = []
    index = 0
    while index < len(option_rows):
        if option_rows[index]["text"] != "CST":
            index += 1
            continue
        triple: dict[str, dict[str, Any]] = {"CST": option_rows[index]}
        if index + 1 < len(option_rows) and option_rows[index + 1]["text"] == "TS":
            triple["TS"] = option_rows[index + 1]
        if index + 2 < len(option_rows) and option_rows[index + 2]["text"] == "S":
            triple["S"] = option_rows[index + 2]
        triples.append(triple)
        index += max(1, len(triple))
    return triples


def _nearest_triple(item_y: float, triples: list[dict[str, dict[str, Any]]]) -> dict[str, dict[str, Any]] | None:
    if not triples:
        return None
    best = min(triples, key=lambda triple: abs(triple["CST"]["cy"] - item_y))
    if abs(best["CST"]["cy"] - item_y) > 36:
        return None
    return best


def _assign_item_tracks(
    items: list[dict[str, Any]],
    markers: list[fitz.Rect],
    option_rows: list[dict[str, Any]],
) -> dict[str, set[str]]:
    """Map each numbered benchmark to the CST/TS/S tracks where a placement marker appears."""
    if not items:
        return {}
    triples = _option_track_triples(option_rows)
    item_tracks: dict[str, set[str]] = {item["num"]: set() for item in items}
    for marker in markers:
        marker_y = (marker.y0 + marker.y1) / 2.0
        best_item = min(items, key=lambda item: abs(item["y"] - marker_y))
        if abs(best_item["y"] - marker_y) > ITEM_MARKER_MAX_DISTANCE:
            continue
        triple = _nearest_triple(best_item["y"], triples)
        if triple is None:
            continue
        best_track: str | None = None
        best_delta = 999.0
        for track, row in triple.items():
            delta = abs(marker_y - row["cy"])
            if delta < best_delta:
                best_delta = delta
                best_track = track
        if best_track and best_delta <= MARKER_TRACK_TOLERANCE:
            item_tracks[best_item["num"]].add(best_track)
    return item_tracks


def _item_descriptions(spans: list[dict[str, Any]], geometry: dict[str, float]) -> list[dict[str, Any]]:
    # Description text lives to the left of the placement-marker column; anchor the
    # left/right bounds on the derived geometry instead of fixed page coordinates.
    desc_right_bound = geometry["marker_x_min"] - 5.0
    number_left_bound = min(120.0, desc_right_bound)
    items: list[dict[str, Any]] = []
    for span in spans:
        if span["x0"] > number_left_bound:
            continue
        if not re.fullmatch(r"\d+\.?", span["text"]):
            continue
        items.append({"num": span["text"].rstrip("."), "y": span["cy"], "desc": ""})
    for span in spans:
        if span["x0"] < 90 or span["x0"] > desc_right_bound:
            continue
        if re.fullmatch(r"\d+\.?", span["text"]):
            continue
        for item in items:
            if abs(span["cy"] - item["y"]) <= 10:
                item["desc"] = f"{item['desc']} {span['text']}".strip()
    return [item for item in items if item["desc"]]


def _progression_matrix_hints_block(
    page: fitz.Page, page_index: int, option_tracks: tuple[str, ...]
) -> str | None:
    """Emit parser-derived placement hints for progression tables with option tracks.

    In PFEQ-style tables, arrow/star/box markers are vector graphics (not text) in a
    column beside option-track labels. A benchmark description applies ONLY to the
    option tracks where a marker is present; empty tracks must not receive rows.
    
    Auto-discovers option tracks from the page first, falling back to configured tracks
    if discovery yields nothing. This makes the detector universal across curricula.
    """
    spans = _collect_positioned_spans(page)
    
    # Try auto-discovery first for truly universal detection
    discovered_tracks = _discover_option_tracks(spans)
    active_tracks = discovered_tracks if discovered_tracks else option_tracks
    
    if not active_tracks:
        return None
    
    geometry = _matrix_geometry(page, spans, active_tracks)
    if geometry is None:
        return None

    option_x0 = geometry["option_x0"]
    option_rows = sorted(
        [
            span
            for span in spans
            if span["text"] in active_tracks and span["x0"] >= option_x0 - 5.0
        ],
        key=lambda span: span["cy"],
    )
    markers = _placement_markers(page, geometry)
    items = _item_descriptions(spans, geometry)
    if not items:
        return None

    item_tracks = _assign_item_tracks(items, markers, option_rows)

    track_list = " / ".join(active_tracks)
    track_list_comma = ", ".join(active_tracks)
    discovery_note = " (auto-discovered)" if discovered_tracks else ""
    
    hint_intro = (
        f"This page uses a {track_list} option-track matrix{discovery_note}. Placement markers "
        "(arrow, star, shaded box) are graphics beside the track labels, not plain text. "
        "Create output rows ONLY for tracks listed as `applies_to` below. "
        "Tracks listed under `not_for` must receive NO row for that benchmark. "
        f"Do not fan out one description to {track_list_comma} unless each track is listed in `applies_to`."
    )
    
    lines = [
        "## Parsed progression-matrix placement hints",
        hint_intro,
    ]
    for item in items:
        applies_to = sorted(item_tracks.get(item["num"], set()))
        if not applies_to:
            continue
        not_for = [track for track in active_tracks if track not in applies_to]
        lines.append(
            f"- Item {item['num']}: {item['desc']} | applies_to={','.join(applies_to)}"
            + (f" | not_for={','.join(not_for)}" if not_for else "")
        )
    if len(lines) <= 2:
        return None
    lines.append(f"(placement hints derived from PDF layout on page {page_index})")
    return "\n".join(lines)


def _native_page_markdown(page: fitz.Page, page_index: int, settings: Settings) -> str:
    markdown = _page_dict_to_markdown(page_index, page.get_text("dict"))
    option_tracks = settings.progression_matrix_option_tracks or DEFAULT_OPTION_TRACKS
    placement_hints = _progression_matrix_hints_block(page, page_index, option_tracks)
    if placement_hints:
        markdown = f"{markdown}\n\n{placement_hints}"
    return markdown


def _ocr_page_markdown(page: fitz.Page, page_index: int, *, dpi: int, language: str) -> str:
    textpage = page.get_textpage_ocr(dpi=dpi, full=True, language=language)
    return _page_dict_to_markdown(page_index, page.get_text("dict", textpage=textpage))


def _native_plain_text_chars(page: fitz.Page) -> int:
    return len(page.get_text("text").strip())


def _page_needs_ocr(page: fitz.Page, native_chars: int, min_chars_per_page: int) -> bool:
    if native_chars >= min_chars_per_page:
        return False
    if page.get_images(full=True):
        return True
    return native_chars < min_chars_per_page


def _tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def assess_pdf_text_extraction(path: Path, settings: Settings | None = None) -> dict[str, Any]:
    """Summarize whether a PDF likely needs OCR before extraction."""
    active_settings = settings or get_settings()
    with fitz.open(path) as document:
        page_count = document.page_count
        native_chars_per_page = [_native_plain_text_chars(document[page_index]) for page_index in range(page_count)]
        low_text_pages = sum(
            1
            for chars in native_chars_per_page
            if chars < active_settings.pdf_ocr_min_chars_per_page
        )
        total_chars = sum(native_chars_per_page)
        average_chars = total_chars / page_count if page_count else 0.0
        low_text_ratio = low_text_pages / page_count if page_count else 0.0

    needs_ocr = False
    if active_settings.pdf_ocr_mode == "always":
        needs_ocr = True
    elif active_settings.pdf_ocr_mode == "auto":
        needs_ocr = (
            average_chars < active_settings.pdf_ocr_min_chars_per_page
            or low_text_ratio >= active_settings.pdf_ocr_low_text_page_ratio
        )

    return {
        "page_count": page_count,
        "total_native_chars": total_chars,
        "average_native_chars_per_page": round(average_chars, 1),
        "low_text_pages": low_text_pages,
        "low_text_page_ratio": round(low_text_ratio, 3),
        "ocr_mode": active_settings.pdf_ocr_mode,
        "likely_needs_ocr": needs_ocr,
        "tesseract_available": _tesseract_available(),
    }


def parse_pdf(path: Path, settings: Settings | None = None) -> ParsedDocument:
    active_settings = settings or get_settings()
    markdown_pages: list[str] = []
    ocr_pages = 0
    native_pages = 0
    ocr_warnings: list[str] = []

    with fitz.open(path) as document:
        for page_index, page in enumerate(document, start=1):
            native_chars = _native_plain_text_chars(page)
            use_ocr = active_settings.pdf_ocr_mode == "always"
            if active_settings.pdf_ocr_mode == "auto":
                use_ocr = _page_needs_ocr(page, native_chars, active_settings.pdf_ocr_min_chars_per_page)

            if use_ocr:
                if not _tesseract_available():
                    ocr_warnings.append(
                        f"Page {page_index} looked image-based but Tesseract is not installed; kept native text."
                    )
                    markdown_pages.append(_native_page_markdown(page, page_index, active_settings))
                    native_pages += 1
                    continue
                try:
                    markdown_pages.append(
                        _ocr_page_markdown(
                            page,
                            page_index,
                            dpi=active_settings.pdf_ocr_dpi,
                            language=active_settings.pdf_ocr_language,
                        )
                    )
                    ocr_pages += 1
                except Exception as exc:
                    LOGGER.warning("OCR failed for %s page %s: %s", path.name, page_index, exc)
                    ocr_warnings.append(f"Page {page_index} OCR failed ({exc}); kept native text.")
                    markdown_pages.append(_native_page_markdown(page, page_index, active_settings))
                    native_pages += 1
            else:
                markdown_pages.append(_native_page_markdown(page, page_index, active_settings))
                native_pages += 1

    metadata: dict[str, Any] = {
        "page_count": len(markdown_pages),
        "ocr_mode": active_settings.pdf_ocr_mode,
        "ocr_pages": ocr_pages,
        "native_pages": native_pages,
        "ocr_language": active_settings.pdf_ocr_language,
        "ocr_dpi": active_settings.pdf_ocr_dpi,
    }
    assessment = assess_pdf_text_extraction(path, settings=active_settings)
    metadata["text_assessment"] = assessment
    if ocr_warnings:
        metadata["ocr_warnings"] = ocr_warnings

    return ParsedDocument(
        document_id=path.stem,
        source_path=str(path),
        source_name=path.name,
        source_type="pdf",
        markdown="\n\n".join(markdown_pages),
        metadata=metadata,
    )
