from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, create_model, field_validator

from config import Settings, get_settings
from parsers.base import ParsedDocument
from schemas import get_extraction_payload_model, load_schema_config


SAMPLE_DRAFT_PROMPT_PATH = Path(__file__).with_name("sample_csv_column_prompt.md")


@lru_cache(maxsize=1)
def load_sample_draft_guide() -> str:
    """The human-authored sample-drafting guide (column fill + row-formation rules).

    This governs the fast draft so the preview follows the same column semantics the
    full run must honor. Returns an empty string if the file is missing so the draft
    still runs on the schema/contract alone.
    """
    try:
        return SAMPLE_DRAFT_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        LOGGER.warning("Sample-drafting guide not found at %s; drafting without it.", SAMPLE_DRAFT_PROMPT_PATH)
        return ""


class ExtractionClientError(RuntimeError):
    pass


class ExtractionOutputTooLargeError(ExtractionClientError):
    """The model truncated its response because the output hit the token cap.

    Signals the chunking layer to split the input further and retry.
    """


LOGGER = logging.getLogger(__name__)


class ExtractionRegion(BaseModel):
    start_anchor: str = Field(
        default="",
        description="Verbatim text marking where extractable content STARTS (a '# Page N' marker for PDFs, or a heading line for docs). Empty if the whole document should be used.",
    )
    end_anchor: str = Field(
        default="",
        description="Verbatim text marking where extractable content ENDS. Empty means continue to the end of the document.",
    )
    skip_anchors: list[str] = Field(
        default_factory=list,
        description="Verbatim heading/marker texts between start and end whose sections must be skipped (e.g. an embedded contents table).",
    )
    confidence: str = Field(
        default="low",
        description="Confidence in these boundaries: one of high, medium, low.",
    )
    notes: str = Field(default="", description="Short explanation of the chosen boundaries.")

    @field_validator("skip_anchors", mode="before")
    @classmethod
    def _normalize_skip_anchors(cls, value: Any) -> list[str]:
        return _normalize_to_string_list(value)


class PreExtractionUnderstanding(BaseModel):
    layout_analysis: list[str] = Field(
        default_factory=list,
        description="Concise notes describing document structure and likely row boundaries.",
    )
    row_formation_logic: list[str] = Field(
        default_factory=list,
        description="How one complete output row is formed from this source and sample contract.",
    )
    column_derivations: dict[str, str] = Field(
        default_factory=dict,
        description="Per-column explanation of what content belongs there and how it is derived.",
    )
    representative_row: dict[str, str] = Field(
        default_factory=dict,
        description="A row-shaped preview showing what one complete row would contain before final cited extraction.",
    )
    exclusion_rules: list[str] = Field(
        default_factory=list,
        description="Source-specific content that must be excluded from output rows.",
    )
    coverage_expectations: list[str] = Field(
        default_factory=list,
        description="Notes about what domains, topics, and row items must be covered so source content is not missed.",
    )
    section_inventory: list[str] = Field(
        default_factory=list,
        description=(
            "Complete enumeration of every content section/sub-section in the source "
            "(from its table of contents AND its body headings), each with the count of "
            "output rows expected from it, e.g. 'Arithmetic > Operations involving real numbers: ~18 rows'. "
            "This is the coverage checklist the extraction must satisfy; no listed section may be silently dropped."
        ),
    )
    expected_total_rows: int = Field(
        default=0,
        description="Best estimate of the total number of output rows the full source should yield when every listed section is extracted.",
    )
    notation_and_symbol_notes: list[str] = Field(
        default_factory=list,
        description=(
            "Notes on mathematical/scientific notation, symbols, equations, radicals, superscripts, "
            "subscripts, Greek letters, and multilingual text present in the source that must be preserved "
            "verbatim, plus any that appear degraded in the parsed text and need careful handling."
        ),
    )
    progression_matrix_legend: list[str] = Field(
        default_factory=list,
        description=(
            "When the source uses a progression matrix with placement symbols (arrow, star, shaded box) "
            "beside option tracks such as CST / TS / S, document what each symbol means and how it gates row scope."
        ),
    )
    row_scope_rules: list[str] = Field(
        default_factory=list,
        description=(
            "Rules for which output rows to create based on placement symbols and option tracks. "
            "A benchmark description applies ONLY to the tracks where its symbol appears; "
            "do not fan out the same description to CST, TS, and S unless each track has a marker."
        ),
    )
    sample_csv_track_structure: list[str] = Field(
        default_factory=list,
        description=(
            "Analysis of how the sample CSV encodes option tracks in display codes. "
            "E.g., 'Some standards use S5.MAT.CST/TS/S segment after subject code, others omit track.' "
            "This teaches the model which display codes should include track suffixes."
        ),
    )
    document_to_sample_track_mapping: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Maps document option-track labels (as discovered or stated in legends) to sample CSV track segments. "
            "E.g., {'CST': 'CST', 'TS': 'TS', 'S': 'S'} or {'Academic': 'ACD', 'Applied': 'APL'}. "
            "Empty when tracks are not present or not yet inferred."
        ),
    )
    variation_anchors: list[str] = Field(
        default_factory=list,
        description=(
            "For a fast sample draft: a VERBATIM snippet (copied exactly from the source markdown) marking "
            "where each DISTINCT TRANSFORMATION CASE first occurs — a place where the source-to-row logic "
            "behaves DIFFERENTLY, not merely a different section or topic. YOU decide what the distinct cases "
            "are for THIS source by comparing its structure against the sample contract; the goal is to surface "
            "every situation a human reviewer must approve. The following are COMMON EXAMPLES, NOT an exhaustive "
            "or required list — include any of these that occur AND any other transformation case you discover "
            "that is not listed: one description/standard applying to MULTIPLE topics (topics merged with ' | '); "
            "a description with parent + child sub-parts (merged as 'parent: child'); a Display standard code "
            "that must be CHANGED/disambiguated (repeated raw code needing a prefix, or bullets needing synthetic "
            "numbering); a benchmark marked for multiple option tracks (one row per track); a value INHERITED "
            "from a section header; real content sitting next to noise that must be excluded. Also include a "
            "couple of ORDINARY straightforward rows as a baseline. Copy the first benchmark line where each "
            "case appears, long enough to string-match uniquely (about 40-80 characters). Group anchors per "
            "distinct case you identify, in source order; do not repeat cases that transform identically."
        ),
    )

    @field_validator(
        "layout_analysis",
        "row_formation_logic",
        "exclusion_rules",
        "coverage_expectations",
        "section_inventory",
        "notation_and_symbol_notes",
        "progression_matrix_legend",
        "row_scope_rules",
        "sample_csv_track_structure",
        "variation_anchors",
        mode="before",
    )
    @classmethod
    def _normalize_string_list_fields(cls, value: Any) -> list[str]:
        return _normalize_to_string_list(value)

    @field_validator("expected_total_rows", mode="before")
    @classmethod
    def _normalize_expected_total_rows(cls, value: Any) -> int:
        if value is None or value == "":
            return 0
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return max(0, value)
        if isinstance(value, float):
            return max(0, int(value))
        if isinstance(value, str):
            match = re.search(r"\d+", value.replace(",", ""))
            return int(match.group(0)) if match else 0
        return 0

    @field_validator("column_derivations", mode="before")
    @classmethod
    def _normalize_column_derivations(cls, value: Any) -> dict[str, str]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return {str(key): _stringify_structured_value(item) for key, item in value.items()}
        if isinstance(value, list):
            normalized: dict[str, str] = {}
            for index, item in enumerate(value, start=1):
                normalized[f"field_{index}"] = _stringify_structured_value(item)
            return normalized
        if isinstance(value, str):
            return {"notes": value.strip()}
        return {"notes": _stringify_structured_value(value)}

    @field_validator("representative_row", mode="before")
    @classmethod
    def _normalize_representative_row(cls, value: Any) -> dict[str, str]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return {str(key): _stringify_structured_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return {f"field_{index}": _stringify_structured_value(item) for index, item in enumerate(value, start=1)}
        if isinstance(value, str):
            return {"notes": value.strip()}
        return {"notes": _stringify_structured_value(value)}

    @field_validator("document_to_sample_track_mapping", mode="before")
    @classmethod
    def _normalize_track_mapping(cls, value: Any) -> dict[str, str]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return {str(key): str(item) for key, item in value.items()}
        if isinstance(value, list):
            # List of pairs like [["CST", "CST"], ["TS", "TS"]]
            result = {}
            for item in value:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    result[str(item[0])] = str(item[1])
            return result
        return {}


def _normalize_to_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, dict):
        normalized_items: list[str] = []
        for key, item in value.items():
            rendered = _stringify_structured_value(item)
            normalized_items.append(f"{key}: {rendered}" if rendered else str(key))
        return [item for item in normalized_items if item.strip()]
    if isinstance(value, list):
        normalized_items: list[str] = []
        for item in value:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    normalized_items.append(text)
                continue
            if isinstance(item, dict):
                normalized_items.extend(_normalize_to_string_list(item))
                continue
            text = _stringify_structured_value(item)
            if text:
                normalized_items.append(text)
        return normalized_items
    text = _stringify_structured_value(value)
    return [text] if text else []


def _stringify_structured_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        parts = [f"{key}: {_stringify_structured_value(item)}" for key, item in value.items()]
        return "; ".join(part for part in parts if part.strip())
    if isinstance(value, list):
        parts = [_stringify_structured_value(item) for item in value]
        return "; ".join(part for part in parts if part.strip())
    return str(value).strip()


def _document_outline(markdown: str, max_chars: int = 12000) -> str:
    """The heading lines and page markers of a document, for the locate pass.

    Feeding only the structure (not the full text) keeps the locate call cheap and
    avoids its own overflow on large documents.
    """
    lines = [
        line.rstrip()
        for line in markdown.splitlines()
        if line.lstrip().startswith("#")
    ]
    outline = "\n".join(lines)
    if len(outline) <= max_chars:
        return outline
    return outline[:max_chars]


def _apply_region(markdown: str, region: "ExtractionRegion") -> str | None:
    """Slice markdown to the region's [start_anchor, end_anchor] with skip sections removed.

    Returns None to signal "use the full document" whenever the boundary is missing,
    low-confidence, or the result is implausibly small — coverage is never sacrificed to
    a bad boundary.
    """
    if region.confidence.strip().lower() == "low":
        return None
    start = region.start_anchor.strip()
    if not start:
        return None
    start_idx = markdown.find(start)
    if start_idx == -1:
        return None

    end = region.end_anchor.strip()
    if end:
        end_idx = markdown.find(end, start_idx + len(start))
        bounded = markdown[start_idx:end_idx] if end_idx != -1 else markdown[start_idx:]
    else:
        bounded = markdown[start_idx:]

    # Remove each skip section: from the skip anchor to the next line that starts with
    # '#' (next heading/page) or end of the bounded text.
    for skip in region.skip_anchors:
        skip = skip.strip()
        if not skip:
            continue
        s_idx = bounded.find(skip)
        if s_idx == -1:
            continue
        newline = bounded.find("\n", s_idx)
        next_heading = -1
        search_from = newline if newline != -1 else s_idx + len(skip)
        for candidate_line_start in _iter_line_starts(bounded, search_from):
            if bounded[candidate_line_start:].lstrip().startswith("#"):
                next_heading = candidate_line_start
                break
        cut_end = next_heading if next_heading != -1 else len(bounded)
        bounded = bounded[:s_idx] + bounded[cut_end:]

    # Guard against detection errors that bound to a trivial fragment. A high-confidence
    # region is trusted even when small (outcomes are often a small slice of a large doc,
    # which is exactly the case region targeting exists for); otherwise require a modest floor.
    bounded_len = len(bounded.strip())
    if bounded_len < 1000:
        return None
    if region.confidence.strip().lower() != "high" and bounded_len < int(len(markdown) * 0.05):
        return None
    return bounded


def _iter_line_starts(text: str, from_index: int):
    idx = from_index
    length = len(text)
    while idx < length:
        yield idx
        nl = text.find("\n", idx)
        if nl == -1:
            return
        idx = nl + 1


def _build_variation_slice(
    markdown: str,
    anchors: list[str],
    *,
    window_chars: int,
    budget_chars: int,
) -> tuple[str, int, int]:
    """A small stitched excerpt covering where each variation first appears.

    For each anchor, locate it by exact substring match and take a window of
    ``window_chars`` around the hit (enough to show a few benchmarks of that
    variation). Overlapping/adjacent windows are merged and emitted in source order,
    separated by a marker so the extractor sees coherent fragments. The total is
    capped at ``budget_chars`` so the downstream extraction context stays one-chunk
    sized regardless of document length — this is the cost guarantee.

    Returns (slice_text, matched_anchor_count, skipped_anchor_count). slice_text is
    empty when no anchor matched, signalling the caller to fall back.
    """
    if not markdown or not anchors:
        return "", 0, 0

    half = max(500, window_chars // 2)
    spans: list[tuple[int, int]] = []
    matched = 0
    skipped = 0
    for anchor in anchors:
        needle = anchor.strip()
        if len(needle) < 12:
            skipped += 1
            continue
        hit = markdown.find(needle)
        if hit == -1:
            skipped += 1
            continue
        matched += 1
        start = max(0, hit - half)
        end = min(len(markdown), hit + len(needle) + half)
        spans.append((start, end))

    if not spans:
        return "", 0, skipped

    # Merge overlapping/adjacent windows, keeping source order.
    spans.sort()
    merged: list[list[int]] = [list(spans[0])]
    for start, end in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    separator = "\n\n... [section break] ...\n\n"
    pieces: list[str] = []
    used = 0
    for start, end in merged:
        if used >= budget_chars:
            break
        fragment = markdown[start:end]
        remaining = budget_chars - used
        if len(fragment) > remaining:
            fragment = fragment[:remaining]
        pieces.append(fragment)
        used += len(fragment) + len(separator)

    return separator.join(pieces), matched, skipped


def _is_output_truncation_error(exc: Exception) -> bool:
    markers = ("incompleteoutput", "max_tokens", "length limit", "finish_reason", "output is incomplete")
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in markers)


def _strip_json_code_fence(content: str) -> str:
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1).strip()
    if text.startswith("{") or text.startswith("["):
        return text
    first_object = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
    if first_object:
        return first_object.group(1).strip()
    return text


def _extract_message_text(response: Any) -> str:
    if isinstance(response, dict):
        choices = response.get("choices") or []
    else:
        choices = getattr(response, "choices", []) or []

    if not choices:
        raise ExtractionClientError("Model response did not include any choices.")

    first_choice = choices[0]
    message = first_choice.get("message") if isinstance(first_choice, dict) else getattr(first_choice, "message", None)
    if message is None:
        raise ExtractionClientError("Model response did not include a message payload.")

    content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_chunks: list[str] = []
        for item in content:
            if isinstance(item, str):
                text_chunks.append(item)
                continue
            if isinstance(item, dict):
                if item.get("type") == "text" and item.get("text"):
                    text_chunks.append(item["text"])
                elif item.get("text"):
                    text_chunks.append(str(item["text"]))
                continue
            item_text = getattr(item, "text", None)
            if item_text:
                text_chunks.append(str(item_text))
        rendered = "".join(text_chunks).strip()
        if rendered:
            return rendered

    raise ExtractionClientError("Model response did not contain parsable text content.")


@dataclass(slots=True)
class ExtractionAttempt:
    payload_rows: list[BaseModel]
    planning: PreExtractionUnderstanding
    layout_analysis: list[str]
    anchoring_plan: dict[str, str]


class ExtractionEngine:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def extract(
        self,
        parsed_document: ParsedDocument,
        prior_error_log: str | None = None,
        region_override: ExtractionRegion | None = None,
    ) -> ExtractionAttempt:
        schema_path = str(self.settings.schema_config_path)
        payload_model = get_extraction_payload_model(
            schema_path, include_citations=self.settings.extraction_citations_enabled
        )

        parsed_document = self._apply_region_targeting(parsed_document, region_override)

        planning = self._analyze_document(parsed_document, prior_error_log)
        citations_enabled = self.settings.extraction_citations_enabled
        payload_description = (
            "All final structured extraction rows with verbatim citations, in source order."
            if citations_enabled
            else "All final structured extraction rows in source order."
        )
        envelope_model = create_model(
            "ExtractionEnvelope",
            __base__=BaseModel,
            __module__=__name__,
            anchoring_plan=(
                dict[str, str],
                Field(description="Field-to-layout anchor map used for this one extraction."),
            ),
            payload_rows=(
                list[payload_model],
                Field(description=payload_description),
            ),
        )

        chunks = self._chunk_markdown(parsed_document.markdown)
        payload_rows: list[BaseModel] = []
        anchoring_plan: dict[str, str] = {}

        # Extract chunks concurrently. The global LLM semaphore in portkey_client
        # is the real ceiling, so this just fills idle capacity; a big multi-chunk
        # document no longer pays the sum of its chunk latencies serially. Results
        # are reassembled in source order to keep row ordering stable.
        max_workers = max(1, min(self.settings.llm_max_concurrency, len(chunks)))
        if len(chunks) <= 1 or max_workers <= 1:
            chunk_results = [
                self._extract_chunk_rows(parsed_document, planning, prior_error_log, envelope_model, chunk)
                for chunk in chunks
            ]
        else:
            indexed: list[tuple[list[BaseModel], dict[str, str]] | None] = [None] * len(chunks)
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(
                        self._extract_chunk_rows,
                        parsed_document,
                        planning,
                        prior_error_log,
                        envelope_model,
                        chunk,
                    ): index
                    for index, chunk in enumerate(chunks)
                }
                first_error: Exception | None = None
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        indexed[index] = future.result()
                    except Exception as exc:  # noqa: BLE001 - re-raised after drain
                        if first_error is None:
                            first_error = exc
                if first_error is not None:
                    raise first_error
            chunk_results = [item for item in indexed if item is not None]

        for rows, plan in chunk_results:
            payload_rows.extend(rows)
            anchoring_plan.update(plan)

        return ExtractionAttempt(
            planning=planning,
            payload_rows=payload_rows,
            layout_analysis=planning.layout_analysis,
            anchoring_plan=anchoring_plan,
        )

    def draft_sample_rows(
        self,
        parsed_document: ParsedDocument,
        max_rows: int,
        min_rows: int | None = None,
    ) -> list[BaseModel]:
        """Fast sample-draft path: representative rows for human approval.

        Unlike `extract`, this skips the review/critic fix-loop entirely. One analysis
        pass, then one extraction call per chunk, pulling successive chunks until at least
        `min_rows` rows accumulate (going up to `max_rows` to capture source variations
        like distinct sections, code formats, option tracks, and row shapes). Rows are raw
        (no QC) — the human approves the pattern before the full run.
        """
        if min_rows is None or min_rows > max_rows:
            min_rows = max_rows
        payload_model = get_extraction_payload_model(
            str(self.settings.schema_config_path),
            include_citations=self.settings.extraction_citations_enabled,
        )
        citations_enabled = self.settings.extraction_citations_enabled
        payload_description = (
            "Representative sample-draft rows with verbatim citations, in source order."
            if citations_enabled
            else "Representative sample-draft rows in source order."
        )
        envelope_model = create_model(
            "DraftExtractionEnvelope",
            __base__=BaseModel,
            __module__=__name__,
            anchoring_plan=(
                dict[str, str],
                Field(description="Field-to-layout anchor map used for this draft."),
            ),
            payload_rows=(
                list[payload_model],
                Field(description=payload_description),
            ),
        )

        bounded_document = self._apply_region_targeting(parsed_document, None)
        chunks = self._chunk_markdown(bounded_document.markdown)
        planning = self._analyze_document(bounded_document, None)

        # Anchor-guided fast path: the analysis pass (already run over the whole region)
        # located where each distinct variation first appears. Slice small windows around
        # those anchors and run ONE extraction call over the stitched excerpt, so variations
        # scattered across a large document are all seen without reading the full region.
        # Falls back to the chunk loop when no anchor matches (uniform doc / match failure).
        budget_chars = self.settings.extraction_max_chars_per_chunk
        slice_text, matched, skipped = _build_variation_slice(
            bounded_document.markdown,
            planning.variation_anchors,
            window_chars=max(2000, budget_chars // max(1, len(planning.variation_anchors) or 1)),
            budget_chars=budget_chars,
        )
        if slice_text:
            LOGGER.info(
                "Draft variation slice: %d anchors matched, %d skipped; slice %d chars (region %d chars).",
                matched,
                skipped,
                len(slice_text),
                len(bounded_document.markdown),
            )
            messages = self._build_extraction_messages(
                bounded_document,
                planning,
                prior_error_log=None,
                markdown_override=slice_text,
                chunk_note=(
                    "This is a SAMPLE DRAFT built from stitched excerpts, each chosen because it needs a "
                    "DIFFERENT transformation (excerpts are separated by '... [section break] ...'). Return "
                    "clean rows that demonstrate how EACH tricky case shown here is handled — multi-topic "
                    "merges with ' | ', parent+child descriptions merged as 'parent: child', changed/"
                    "disambiguated Display standard codes, option-track fan-out, inherited header values, and "
                    "content kept clear of noise — the goal is an approvable preview of the transformation logic."
                ),
                draft_max_rows=max_rows,
                draft_min_rows=min_rows,
            )
            try:
                response = self._call_extractor(envelope_model, messages)
                return list(response.payload_rows)[:max_rows]
            except ExtractionOutputTooLargeError:
                LOGGER.info("Draft variation slice overflowed; falling back to chunk loop.")

        collected: list[BaseModel] = []
        for chunk in chunks:
            messages = self._build_extraction_messages(
                bounded_document,
                planning,
                prior_error_log=None,
                markdown_override=chunk,
                chunk_note=(
                    "This is a SAMPLE DRAFT from one part of the source. Return clean rows that demonstrate the "
                    "distinct TRANSFORMATION CASES in this part — multi-topic merges with ' | ', parent+child "
                    "descriptions merged as 'parent: child', changed/disambiguated Display standard codes, "
                    "option-track fan-out, inherited header values, and content kept clear of noise — plus a "
                    "couple of ordinary rows; the goal is an approvable preview of the transformation logic."
                ),
                draft_max_rows=max_rows,
                draft_min_rows=min_rows,
            )
            try:
                response = self._call_extractor(envelope_model, messages)
            except ExtractionOutputTooLargeError:
                # A draft never needs the full chunk; if even a bounded draft overflows,
                # skip this chunk rather than splitting — the next chunk can supply rows.
                LOGGER.info("Draft chunk overflowed; skipping to next chunk.")
                continue
            collected.extend(response.payload_rows)
            # Keep pulling chunks until the floor is met so a short opening section does
            # not starve the sample; cap total at max_rows to bound cost.
            if len(collected) >= min_rows:
                break

        return collected[:max_rows]

    def _apply_region_targeting(
        self,
        parsed_document: ParsedDocument,
        region_override: ExtractionRegion | None,
    ) -> ParsedDocument:
        if region_override is None and not self.settings.enable_region_targeting:
            return parsed_document

        region = region_override if region_override is not None else self._locate_extraction_region(parsed_document)
        if region is None:
            return parsed_document

        bounded = _apply_region(parsed_document.markdown, region)
        original_len = len(parsed_document.markdown)
        if bounded is None or not bounded.strip():
            LOGGER.info(
                "Region targeting: no reliable boundary for %s; using full document (%d chars).",
                parsed_document.source_name,
                original_len,
            )
            return parsed_document

        LOGGER.info(
            "Region targeting for %s: bounded %d -> %d chars (start=%r end=%r skip=%d, confidence=%s).",
            parsed_document.source_name,
            original_len,
            len(bounded),
            region.start_anchor[:60],
            region.end_anchor[:60],
            len(region.skip_anchors),
            region.confidence,
        )
        return parsed_document.model_copy(update={"markdown": bounded})

    def _locate_extraction_region(self, parsed_document: ParsedDocument) -> ExtractionRegion | None:
        outline = _document_outline(parsed_document.markdown)
        if not outline.strip():
            return None
        schema_config = load_schema_config(settings=self.settings)
        sample_contract_json = (
            json.dumps(schema_config.sample_contract.model_dump(mode="json"), indent=2)
            if schema_config.sample_contract
            else "null"
        )
        system_prompt = (
            "You locate the region of a curriculum document that contains the extractable rows "
            "(typically the syllabus outcomes/standards), so downstream extraction can skip "
            "front matter, rationale, assessment guidance, glossary, appendices, and sample work. "
            "Return anchors as VERBATIM text copied from the provided outline. For paginated "
            "sources use the '# Page N' markers; otherwise use heading lines. "
            "Return your response as a single JSON object."
        )
        user_prompt = f"""
The approved sample CSV contract describes what one extractable row looks like:
{sample_contract_json}

Below is the structural outline (headings and page markers) of the document '{parsed_document.source_name}'.
Identify:
- start_anchor: the verbatim outline line where extractable content begins.
- end_anchor: the verbatim outline line where extractable content ends (empty to run to the end).
- skip_anchors: verbatim outline lines between start and end whose sections must be skipped.
- confidence: high, medium, or low.

If you cannot confidently locate an outcomes region, return empty anchors with confidence=low.

Document outline:
{outline}
""".strip()
        try:
            return self._call_extractor(
                ExtractionRegion,
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
        except Exception as exc:  # noqa: BLE001 - locate is best-effort; fall back to full doc
            LOGGER.info("Region locate pass failed for %s (%s); using full document.", parsed_document.source_name, exc)
            return None

    def _extract_chunk_rows(
        self,
        parsed_document: ParsedDocument,
        planning: PreExtractionUnderstanding,
        prior_error_log: str | None,
        envelope_model: type[BaseModel],
        chunk: str,
    ) -> tuple[list[BaseModel], dict[str, str]]:
        chunk_note = (
            "This is one part of a larger source document. "
            "Extract only rows supported by THIS part; other parts are handled separately."
        )
        messages = self._build_extraction_messages(
            parsed_document, planning, prior_error_log, markdown_override=chunk, chunk_note=chunk_note
        )
        try:
            response = self._call_extractor(envelope_model, messages)
            return list(response.payload_rows), dict(response.anchoring_plan)
        except ExtractionOutputTooLargeError:
            # The chunk yielded more output than the model's token cap allows.
            # Split it in half on a line boundary and extract each part, down to a floor.
            halves = self._split_in_half(chunk)
            if halves is None:
                raise
            LOGGER.info("Chunk output too large; splitting %d chars into 2 sub-chunks.", len(chunk))
            payload_rows: list[BaseModel] = []
            anchoring_plan: dict[str, str] = {}
            for half in halves:
                rows, plan = self._extract_chunk_rows(
                    parsed_document, planning, prior_error_log, envelope_model, half
                )
                payload_rows.extend(rows)
                anchoring_plan.update(plan)
            return payload_rows, anchoring_plan

    def _split_in_half(self, chunk: str) -> list[str] | None:
        lines = chunk.splitlines(keepends=True)
        # Stop shrinking at a floor or when a chunk is a single unsplittable line.
        if len(lines) < 2 or len(chunk) <= 4000:
            return None
        mid = len(lines) // 2
        first = "".join(lines[:mid])
        second = "".join(lines[mid:])
        if not first.strip() or not second.strip():
            return None
        return [first, second]

    def _chunk_markdown(self, markdown: str) -> list[str]:
        limit = self.settings.extraction_max_chars_per_chunk
        if len(markdown) <= limit:
            return [markdown]

        # Split on line boundaries so table rows (one per line in rendered docx/pdf
        # markdown) are never cut mid-row. Pack lines greedily up to the limit.
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for line in markdown.splitlines(keepends=True):
            if current and current_len + len(line) > limit:
                chunks.append("".join(current))
                current = []
                current_len = 0
            current.append(line)
            current_len += len(line)
        if current:
            chunks.append("".join(current))
        return chunks

    def _analyze_document(
        self,
        parsed_document: ParsedDocument,
        prior_error_log: str | None,
    ) -> PreExtractionUnderstanding:
        messages = self._build_analysis_messages(parsed_document, prior_error_log)
        return self._call_extractor(PreExtractionUnderstanding, messages)

    def _build_analysis_messages(
        self,
        parsed_document: ParsedDocument,
        prior_error_log: str | None,
    ) -> list[dict[str, str]]:
        schema_config = load_schema_config(settings=self.settings)
        schema_json = json.dumps(schema_config.model_dump(mode="json"), indent=2)
        sample_contract_json = (
            json.dumps(schema_config.sample_contract.model_dump(mode="json"), indent=2)
            if schema_config.sample_contract
            else "null"
        )
        correction_block = (
            "No prior validation failures.\n"
            if not prior_error_log
            else f"Previous validation failure log. Correct these exact issues:\n{prior_error_log}\n"
        )

        system_prompt = (
            "You are an adaptive data transformation agent performing the pre-extraction understanding pass. "
            "This artifact is the required plan the extractor will follow, so it must be thorough and complete. "
            "Do not use static assumptions about layout. "
            "Before extraction, inspect the unique source structure together with the sample CSV contract. "
            "This step is only for understanding how rows are formed and how each column is derived; do not extract final values yet. "
            "Do not hardcode subject-specific assumptions. "
            "Infer the meaning of each output column for this subject from the approved sample contract first, then from the source structure. "
            "You must account for the ENTIRE source: walk its table of contents and every body heading, and enumerate every "
            "section and sub-section so none is missed during extraction. "
            "Curriculum sources repeat a row pattern many times (one row per benchmark/sub-item a, b, c, ...); estimate counts at that granularity, not per heading. "
            "Preserve source notation exactly: identify mathematical symbols, equations, radicals, superscripts, subscripts, "
            "Greek letters, chemistry notation, and multilingual text that must survive verbatim into the output. "
            "When the parsed markdown includes 'Parsed progression-matrix placement hints', treat those as authoritative "
            "for which CST/TS/S option tracks each benchmark belongs to. "
            "grade_level must be exactly one of: Elementary School, Middle School, High School."
        )

        user_prompt = f"""
Schema to populate:
{schema_json}

Sample CSV transformation contract:
{sample_contract_json}

Document metadata:
- source_name: {parsed_document.source_name}
- source_type: {parsed_document.source_type}
- source_path: {parsed_document.source_path}

Validation feedback:
{correction_block}

Instructions:
1. Analyze this document's unique structure and summarize it in layout_analysis.
2. First infer what each output column means for this subject under the approved sample contract. Do not assume the same subject/domain/topic/grade pattern used by a different subject.
3. Explain in row_formation_logic how one complete output row is formed from this source and how that row pattern repeats across the source (identify the smallest repeating unit, e.g. each lettered/numbered benchmark).
4. For each schema field, explain in column_derivations what data it should contain and how it is derived from the source.
5. Determine whether values such as source, subject, domain, topic, grade_level, display_grade, and grade_number are document-level, section-level, or row-level for this specific subject and source. grade_level must always normalize to exactly one of Elementary School, Middle School, or High School.
6. If the approved sample implies canonical public source links, merged topic paths, row-specific stage labels, or transformed display codes, note that explicitly when supported by the source.
7. Build representative_row as a row-shaped preview showing what one complete row would contain under this sample contract.
8. List exclusion_rules describing what source content must be rejected from output rows.
9. Build section_inventory: walk the document's table of contents AND its body headings and list EVERY content section and sub-section, each with the approximate number of output rows it should yield (e.g. 'Algebra > Understanding dependency relationships: ~20 rows'). Do not omit any section, even short ones. This is the coverage checklist extraction must satisfy.
10. Set expected_total_rows to the sum of the per-section estimates in section_inventory — the total rows the full source should produce.
11. In coverage_expectations, call out sections or repeated sub-items that are easy to under-count or skip so they are not missed.
12. Fill notation_and_symbol_notes with the mathematical/scientific symbols, equations, radicals, superscripts, subscripts, Greek letters, and multilingual text present in the source that must be preserved verbatim, and flag anything that looks degraded in the parsed text.
13. Analyze the sample CSV Display standard code column for track patterns. Look for codes like S5.MAT.CST, S5.MAT.TS, S5.MAT.S where a track segment appears after the subject code for certain standards. Fill sample_csv_track_structure with notes explaining which standards have track suffixes and which do not.
14. When the source uses progression matrices with placement symbols (arrow, star, shaded box) beside option-track labels, read any legend in the document and any `Parsed progression-matrix placement hints` blocks. The hints auto-discover track labels from the page (e.g. CST/TS/S, Academic/Applied, or other vocabulary). Fill progression_matrix_legend with what each symbol means.
15. Map document track labels to sample CSV track segments in document_to_sample_track_mapping. E.g., if the document has CST and the sample uses .CST. in codes, create a mapping from CST to CST. If tracks are not present in either document or sample, leave empty.
16. Fill row_scope_rules with explicit scoping rules: a benchmark description applies ONLY to the option tracks where its placement symbol appears; never fan out the same description across all tracks unless each is marked. Encode display-code/track suffix rules based on the sample_csv_track_structure analysis.
17. Fill variation_anchors so a fast sample draft can showcase every TRANSFORMATION CASE a human must approve. A "variation" here is NOT a different section or topic — it is a place where the source-to-row transformation logic BEHAVES DIFFERENTLY. YOU are responsible for deciding what the distinct cases are for THIS source: compare the source's structure against the sample contract and identify every situation where forming a row requires a non-trivial or non-obvious decision. Scan the whole source and copy a short VERBATIM snippet (the first benchmark line where each case occurs) marking it. The following are COMMON EXAMPLES to look for, but they are NOT exhaustive and NOT required — flag any of these that occur AND any other transformation case you discover that is not on this list: (a) one description/standard applying to MULTIPLE topics → topics merged into one cell with ' | '; (b) a description built from a parent statement plus child sub-parts → merged as 'parent: child'; (c) a Display standard code that must be CHANGED — a raw code repeated across domains needing a domain/topic prefix, or bullet items with no code needing synthetic numbering; (d) a benchmark marked for multiple option tracks (CST/TS/S etc.) → one row per marked track; (e) a value that must be INHERITED from a section/heading because it is not printed beside the benchmark; (f) real content adjacent to noise (N.B., notes, headers, page numbers) that must be excluded. Also include about three ORDINARY straightforward benchmarks as a baseline. For each case you identify, provide up to THREE anchors pointing at three different instances of that case (so the draft can show three rows per case); give fewer only when the source genuinely has fewer instances, and OMIT any listed example case the source does not contain. Copy each snippet EXACTLY (including punctuation and page markers like '# Page N'), roughly 40-80 characters, so it can be located by an exact string match. Group anchors by case in source reading order; do not paraphrase.
18. Do not perform final cited extraction yet. This step is only for understanding the source-to-row mapping.

Source markdown:
{parsed_document.markdown}
""".strip()

        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    def _build_extraction_messages(
        self,
        parsed_document: ParsedDocument,
        planning: PreExtractionUnderstanding,
        prior_error_log: str | None,
        markdown_override: str | None = None,
        chunk_note: str | None = None,
        draft_max_rows: int | None = None,
        draft_min_rows: int | None = None,
    ) -> list[dict[str, str]]:
        schema_config = load_schema_config(settings=self.settings)
        schema_json = json.dumps(schema_config.model_dump(mode="json"), indent=2)
        sample_contract_json = (
            json.dumps(schema_config.sample_contract.model_dump(mode="json"), indent=2)
            if schema_config.sample_contract
            else "null"
        )
        planning_json = json.dumps(planning.model_dump(mode="json"), indent=2)
        source_markdown = markdown_override if markdown_override is not None else parsed_document.markdown
        correction_block = (
            "No prior validation failures.\n"
            if not prior_error_log
            else f"Previous validation failure log. Correct these exact issues:\n{prior_error_log}\n"
        )

        citation_rules = (
            "Citations must be verbatim snippets copied from the source markdown. "
            "If a value is absent, return null for the value and an empty string for its citation. "
        )
        system_prompt = (
            "You are an adaptive data transformation agent. "
            "Do not use static assumptions about layout. "
            "Use the pre-extraction understanding artifact as the required plan before mapping content into the provided schema. "
            "The provided sample CSV contract defines the transformation rules and output style; follow it strictly. "
            "Infer subject-specific column meaning from the sample contract and source rather than hardcoding one hierarchy interpretation across subjects. "
            "grade_level must be exactly one of: Elementary School, Middle School, High School."
            "Description fields must preserve source meaning completely, without truncation, cross-row merges, "
            "neighbor contamination, or forced flattening when multiline structure is meaningful. "
            "Preserve mathematical symbols, equations, radicals, superscripts, subscripts, chemistry notation, "
            "Greek letters, domain-specific notation, semantic punctuation, and multilingual text faithfully. "
            "If notation looks degraded in native extraction, attempt a localized repair only when supported by the source. "
            "Reject noise such as headers, footers, page numbers, continuation fragments, appendix-only noise, N.B. notes, "
            "layout labels, and extraction artifacts unless the contract explicitly requires them. "
            "When row_scope_rules or parsed progression-matrix placement hints are present, emit one output row per "
            "(benchmark, marked option track) pair only — never duplicate a benchmark across CST, TS, and S unless each "
            "track is explicitly marked. "
            + (citation_rules if self.settings.extraction_citations_enabled else "")
            + "Keep anchoring_plan short and field-specific. "
            "Extract every valid row supported by the source and approved sample contract. "
            "If the approved sample supports merged topic cells and one standard genuinely applies to multiple topics, merge those topic names into one topic cell using ' | ' rather than duplicating the row only for topic labels. "
            "If the approved sample supports prefixed or formed display codes and the same raw display code repeats, prefix it with a short domain code or topic code plus a dot when that disambiguation is supported by the source structure. "
            "Do not stop after the first row. "
            "Do not omit valid domains, topics, or descriptions that belong in output. "
            "Return payload_rows in source order with no duplicates and no missing supported rows. "
            "grade_level must be exactly one of: Elementary School, Middle School, High School."
        )

        if draft_max_rows is not None:
            if draft_min_rows is None or draft_min_rows > draft_max_rows:
                draft_min_rows = draft_max_rows
            draft_guide = load_sample_draft_guide()
            system_prompt += (
                " You are producing a SAMPLE DRAFT for human approval, not a full extraction. "
                f"Produce AT LEAST {draft_min_rows} rows, and MORE (up to {draft_max_rows}) so the sample showcases "
                "every distinct TRANSFORMATION CASE — a place where the source-to-row logic behaves DIFFERENTLY, NOT "
                "merely a different section or topic. The reviewer needs to approve how each tricky case is handled, "
                "so include about THREE rows demonstrating EACH transformation case the source exhibits (fewer only "
                "if the source genuinely has fewer than three instances), so the reviewer sees each rule applied "
                "consistently rather than just once. The pre-extraction plan's variation_anchors already identify "
                "the cases found in THIS source — cover each of them. The following are COMMON EXAMPLES of such "
                "cases, but they are NOT exhaustive and NOT required; cover whichever the source actually has, plus "
                "any other transformation case present even if it is not listed here: "
                "(a) one description/standard that applies to MULTIPLE topics → merge the topic names into one cell with ' | ' (do not duplicate the row just to repeat topics); "
                "(b) a description built from a parent statement plus child sub-parts → merge them as 'parent: child' so the row keeps full meaning; "
                "(c) a Display standard code that must be CHANGED — a raw code repeats across domains so it needs a short domain/topic prefix and a dot, or bullet items with no code need synthetic sequence numbers; "
                "(d) a benchmark marked for MULTIPLE option tracks (CST/TS/S etc.) → emit one row per marked track; "
                "(e) a value that must be INHERITED from a section/heading because it is not printed beside the benchmark; "
                "(f) real content next to NOISE (N.B., notes, headers, page numbers) → keep the content, drop the noise. "
                "For each case that the source ACTUALLY contains, give up to three rows (fewer if it has fewer "
                "instances); OMIT example cases the source does not contain — do not invent them. Prefer covering "
                "DIFFERENT transformation cases (three rows each) over many rows of the same easy case. "
                f"Then, if you still have fewer than {draft_min_rows} rows, PAD with ordinary straightforward rows "
                f"until you reach at least {draft_min_rows}. A very clean source with no tricky cases should still "
                f"return {draft_min_rows} ordinary rows. "
                f"Never exceed {draft_max_rows} rows and do NOT attempt exhaustive coverage of the whole document. "
                "Follow the sample-drafting guide below for exact per-column fill rules and row-formation rules."
            )
            if draft_guide:
                system_prompt += "\n\n=== SAMPLE DRAFTING GUIDE ===\n" + draft_guide
            system_prompt += (
                "\n\nCRITICAL - PRESERVE SOURCE CASING: Copy every value verbatim from the source, "
                "keeping the ORIGINAL capitalization exactly as written. Do NOT lowercase, uppercase, "
                "title-case, or otherwise normalize letter case in description, topic, domain, subject, "
                "codes, or any other field. If the source says 'In a predator-prey relationship', return "
                "'In a predator-prey relationship' — never 'in a predator-prey relationship'."
            )
            system_prompt += (
                "\n\nCRITICAL - TOP-PRIORITY COLUMN RULES (these override anything above if they conflict):\n"
                "1. STRIP STRUCTURAL PREFIXES from domain and topic. Remove leading unit/topic/module/cluster "
                "markers and their numbers, keeping only the real heading name. "
                "'Unit 1: The Living World: Ecosystems' -> 'The Living World: Ecosystems'; "
                "'Topic 1.1: Introduction to Ecosystems' -> 'Introduction to Ecosystems'. "
                "Never leave a 'Unit N:', 'Topic N.N:', 'Module N:', or 'Cluster N.N' label in domain or topic "
                "when a real heading name is present.\n"
                "2. display_grade must be the DISPLAYED LEARNER GRADE BAND, expressed as a grade/year/stage — "
                "NEVER the grade_level bucket and NEVER a program/course label. Do NOT put 'Elementary School' / "
                "'Middle School' / 'High School' here, and do NOT put a course or program name such as 'AP', 'IB', "
                "'Honors', 'Advanced Placement', 'GCSE', or 'A-Level' here — those name the course, not the grade. "
                "Use an actual grade band such as 'K', '9', 'Year 12', 'Stage 4', or a range like '9-12'. When the "
                "source only names a program/course (e.g. an AP or other senior secondary course) with no explicit "
                "grade printed, use the grade band that course is taught at ('9-12' for AP and similar senior "
                "secondary courses) — never the program name itself.\n"
                "3. grade_number must be the SORTABLE grade value, never a text label like 'High School' and never a "
                "program name like 'AP'. Expand ranges to a comma-separated sequence with NO spaces: "
                "'9-12' -> '9,10,11,12'. If the source states no grade, derive it from the same band used for "
                "display_grade (e.g. an AP course -> '9,10,11,12').\n"
                "4. l3 MAY be filled when the source genuinely has a THIRD hierarchy level between topic and the "
                "benchmark (i.e. subject > domain > topic > l3 > benchmark). This OVERRIDES the guide's default of "
                "leaving l3 blank: if such a real third level exists, put its named heading in l3; if it does not, "
                "leave l3 empty. Do NOT invent an l3, and do NOT copy the description or a topic/cluster code into "
                "it. l4 and l5 stay blank in the sample draft.\n"
                "5. domain must NEVER be empty; topic MAY be empty. If the source has only ONE named grouping level "
                "above the benchmark (no separate domain and topic), put that grouping in DOMAIN and leave TOPIC "
                "empty — promote topic up into domain, never the reverse. Only fill topic when the source genuinely "
                "has a second, finer grouping beneath domain. Do NOT invent a domain and do NOT duplicate the same "
                "value into both domain and topic."
            )

        exhaustive_or_draft_instruction = (
            f"5. SAMPLE DRAFT MODE: Produce AT LEAST {draft_min_rows} rows, going higher (up to {draft_max_rows}) to "
            "cover every distinct TRANSFORMATION CASE — a place where the source-to-row logic behaves differently, "
            "NOT merely a different section. The distinct cases for this source are whatever the pre-extraction "
            "plan's variation_anchors identified — cover each of them, and any other transformation case you "
            "notice while extracting, even if it is not in the examples. Give about THREE rows for EACH case so "
            "the reviewer sees the rule applied consistently (fewer only if the source has fewer instances). "
            "COMMON examples (not exhaustive): multi-topic standards merged with ' | ', parent+child descriptions "
            "merged as 'parent: child', Display standard codes that had to be changed/disambiguated, option-track "
            "fan-out (CST/TS/S), values inherited from a heading, and real content separated from adjacent noise — "
            "plus about three ordinary rows as a baseline. Prefer covering DIFFERENT transformation cases (three "
            "rows each) over repeating the same easy case. Do NOT extract the whole document and do NOT aim for "
            "expected_total_rows — this is a preview."
            if draft_max_rows is not None
            else "5. CRITICAL - EXHAUSTIVE EXTRACTION REQUIRED: This is NOT a sampling task. You MUST produce rows for EVERY SINGLE benchmark item in the source document. The pre-extraction analysis identified expected_total_rows as the target count. Your extraction MUST approach that count. Treat section_inventory as a mandatory checklist - produce rows for EVERY section and sub-section it lists, at the row granularity given in row_formation_logic (one row per benchmark/sub-item). Do not skip, summarize, truncate, or collapse sub-items. Do not stop after a few examples. If you produce significantly fewer rows than expected_total_rows (e.g., only 10-20 rows when 500+ are expected), you have FAILED this extraction task. Work systematically through the entire source document section by section until all content is extracted."
        )

        user_prompt = f"""
Schema to populate:
{schema_json}

Sample CSV transformation contract:
{sample_contract_json}

Pre-extraction understanding artifact:
{planning_json}

Document metadata:
- source_name: {parsed_document.source_name}
- source_type: {parsed_document.source_type}
- source_path: {parsed_document.source_path}

{("Chunking note:\n" + chunk_note + chr(10)) if chunk_note else ""}Validation feedback:
{correction_block}

Instructions:
1. Use the pre-extraction understanding artifact before extracting any values.
2. Build a temporary field anchoring plan explaining where each schema field appears in this specific layout.
3. Apply the sample-derived meaning of each column consistently across all rows. Once you infer what source, subject, domain, topic, grade_level, display_grade, and grade_number mean for this subject, do not drift to a different interpretation. grade_level must be exactly Elementary School, Middle School, or High School.
4. {"Produce clean, representative rows for the sample draft (see item 5); do not attempt full coverage." if draft_max_rows is not None else "Extract every valid output row from the source, not just one row."}
{exhaustive_or_draft_instruction}
6. Return payload_rows in source reading order with one object per output row.
7. If the approved sample supports canonical public source links, use those links when they are identifiable from the source documents or staging context instead of local file names.
8. If the approved sample supports merged topic cells and one standard genuinely spans multiple topics, merge those topic names into one topic field using ` | ` rather than duplicating the row only for topic labels.
9. If the approved sample supports prefixed or formed display codes and the same raw display standard code repeats, disambiguate it with a short domain code or topic code prefix and a dot when that is needed to keep `Display standard code` unique.
10. If the source contradicts the draft representative_row, follow the source while preserving the sample contract.
11. Honor progression_matrix_legend, row_scope_rules, and document_to_sample_track_mapping from the pre-extraction artifact. When parsed placement hints list `applies_to` and `not_for` tracks for a benchmark, create rows only for `applies_to` tracks and skip `not_for` tracks entirely. Use document_to_sample_track_mapping to translate document track labels (e.g., 'CST') into the correct sample CSV display code segments (e.g., '.CST.') when forming Display standard code values.

Source markdown:
{source_markdown}
""".strip()

        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    def _call_extractor(self, response_model: type[BaseModel], messages: list[dict[str, str]]) -> BaseModel:
        if self.settings.portkey_api_key:
            return self._call_portkey_json(response_model, messages)
        return self._call_gemini_instructor(response_model, messages)

    def _call_portkey_json(self, response_model: type[BaseModel], messages: list[dict[str, str]]) -> BaseModel:
        if not self.settings.portkey_api_key:
            raise ExtractionClientError("PORTKEY_API_KEY is not configured.")

        from portkey_client import call_portkey_structured

        try:
            return call_portkey_structured(
                api_key=self.settings.portkey_api_key,
                provider=self.settings.portkey_extractor_provider,
                model=self.settings.extractor_model,
                response_model=response_model,
                messages=messages,
                max_retries=self.settings.extractor_max_retries,
                max_tokens=self.settings.extractor_max_tokens,
                max_concurrency=self.settings.llm_max_concurrency,
                fallbacks=self.settings.extractor_fallbacks,
                timeout=self.settings.request_timeout_seconds,
            )
        except Exception as exc:
            LOGGER.exception(
                "Portkey extractor call failed. provider=%s model=%s error_type=%s error=%r",
                self.settings.portkey_extractor_provider,
                self.settings.extractor_model,
                type(exc).__name__,
                exc,
            )
            if _is_output_truncation_error(exc):
                raise ExtractionOutputTooLargeError(
                    f"Portkey extractor output truncated ({type(exc).__name__}): {exc}"
                ) from exc
            raise ExtractionClientError(
                f"Portkey extractor failed ({type(exc).__name__}): {exc}"
            ) from exc

    def _call_gemini_instructor(self, response_model: type[BaseModel], messages: list[dict[str, str]]) -> BaseModel:
        if not self.settings.gemini_api_key:
            raise ExtractionClientError("GEMINI_API_KEY is not configured.")

        import instructor
        from google import genai

        raw_client = genai.Client(api_key=self.settings.gemini_api_key)
        wrapper_errors: list[str] = []
        client = None

        wrapper_candidates = [
            lambda: instructor.from_genai(raw_client, mode=instructor.Mode.GEMINI_JSON),
            lambda: instructor.from_gemini(raw_client, mode=instructor.Mode.GEMINI_JSON),
        ]

        for build_client in wrapper_candidates:
            try:
                client = build_client()
                break
            except Exception as exc:  # pragma: no cover - version-compatibility shim
                wrapper_errors.append(repr(exc))

        if client is None:
            raise ExtractionClientError(
                "Unable to initialize Instructor Gemini client. "
                + " | ".join(wrapper_errors)
            )

        call_errors: list[str] = []
        call_candidates = [
            lambda: client.chat.completions.create(
                model=self.settings.extractor_model,
                response_model=response_model,
                messages=messages,
                max_retries=0,
            ),
            lambda: client.messages.create(
                model=self.settings.extractor_model,
                response_model=response_model,
                messages=messages,
                max_retries=0,
            ),
            lambda: client.create(
                model=self.settings.extractor_model,
                response_model=response_model,
                messages=messages,
                max_retries=0,
            ),
        ]

        for invoke in call_candidates:
            try:
                return invoke()
            except Exception as exc:  # pragma: no cover - version-compatibility shim
                call_errors.append(repr(exc))

        raise ExtractionClientError(
            "Instructor Gemini call failed across all known call styles. "
            + " | ".join(call_errors)
        )
