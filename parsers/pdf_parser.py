from __future__ import annotations

from pathlib import Path
from statistics import median

import fitz

from parsers.base import ParsedDocument


def _block_text(block: dict) -> tuple[str, float]:
    lines: list[str] = []
    font_sizes: list[float] = []

    for line in block.get("lines", []):
        line_parts: list[str] = []
        for span in line.get("spans", []):
            text = span.get("text", "").strip()
            if not text:
                continue
            line_parts.append(text)
            font_sizes.append(float(span.get("size", 0.0)))
        joined = " ".join(line_parts).strip()
        if joined:
            lines.append(joined)

    average_font_size = sum(font_sizes) / len(font_sizes) if font_sizes else 0.0
    return "\n".join(lines).strip(), average_font_size


def parse_pdf(path: Path) -> ParsedDocument:
    markdown_pages: list[str] = []

    with fitz.open(path) as document:
        for page_index, page in enumerate(document, start=1):
            page_dict = page.get_text("dict")
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

            markdown_pages.append("\n\n".join(markdown_blocks))

    return ParsedDocument(
        document_id=path.stem,
        source_path=str(path),
        source_name=path.name,
        source_type="pdf",
        markdown="\n\n".join(markdown_pages),
        metadata={"page_count": len(markdown_pages)},
    )
