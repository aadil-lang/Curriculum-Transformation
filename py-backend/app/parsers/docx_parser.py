from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.document import Document as DocumentType
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph

from parsers.base import ParsedDocument


def iter_block_items(parent: DocumentType | _Cell):
    parent_elm = parent.element.body if isinstance(parent, DocumentType) else parent._tc
    for child in parent_elm.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, parent)
        elif isinstance(child, CT_Tbl):
            yield Table(child, parent)


def _render_table(table: Table) -> str:
    rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
    if not rows:
        return ""
    header = rows[0]
    divider = ["---"] * len(header)
    body = rows[1:] or [[""] * len(header)]
    markdown_rows = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(divider) + " |",
    ]
    for row in body:
        markdown_rows.append("| " + " | ".join(row) + " |")
    return "\n".join(markdown_rows)


def parse_docx(path: Path) -> ParsedDocument:
    document = Document(path)
    markdown_blocks: list[str] = []

    for block in iter_block_items(document):
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if not text:
                continue
            style_name = (block.style.name or "").lower() if block.style else ""
            if style_name.startswith("heading"):
                level = 2
                digits = "".join(char for char in style_name if char.isdigit())
                if digits:
                    level = max(1, min(6, int(digits)))
                markdown_blocks.append(f"{'#' * level} {text}")
            elif "list" in style_name:
                markdown_blocks.append(f"- {text}")
            else:
                markdown_blocks.append(text)
        elif isinstance(block, Table):
            table_markdown = _render_table(block)
            if table_markdown:
                markdown_blocks.append(table_markdown)

    return ParsedDocument(
        document_id=path.stem,
        source_path=str(path),
        source_name=path.name,
        source_type="docx",
        markdown="\n\n".join(markdown_blocks),
        metadata={"block_count": len(markdown_blocks)},
    )
