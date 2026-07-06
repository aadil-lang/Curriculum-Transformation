from __future__ import annotations

import re
from pathlib import Path

from parsers.base import ParsedDocument
from parsers.doc_parser import parse_doc
from parsers.docx_parser import parse_docx
from parsers.pdf_parser import parse_pdf
from parsers.web_parser import parse_website_reference


SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc", ".url", ".webloc", ".txt"}
URL_PATTERN = re.compile(r"https?://\S+")


def discover_supported_files(input_dir: Path) -> list[Path]:
    files = [path for path in input_dir.iterdir() if path.is_file()]
    return sorted(
        [path for path in files if _is_supported(path)],
        key=lambda item: item.name.lower(),
    )


def parse_input(path: Path) -> ParsedDocument:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return parse_pdf(path)
    if suffix == ".docx":
        return parse_docx(path)
    if suffix == ".doc":
        return parse_doc(path)
    return parse_website_reference(path)


def _is_supported(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix in {".pdf", ".docx", ".doc", ".url", ".webloc"}:
        return True
    if suffix == ".txt":
        content = path.read_text(encoding="utf-8", errors="ignore")
        return bool(URL_PATTERN.search(content))
    return False
