from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from parsers.base import ParsedDocument
from parsers.docx_parser import parse_docx


_SOFFICE_FALLBACK_PATHS = (
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/usr/bin/soffice",
    "/usr/local/bin/soffice",
)


def _find_soffice() -> str:
    located = shutil.which("soffice") or shutil.which("libreoffice")
    if located:
        return located
    for candidate in _SOFFICE_FALLBACK_PATHS:
        if Path(candidate).exists():
            return candidate
    raise RuntimeError(
        "LibreOffice 'soffice' binary not found. Install LibreOffice to parse legacy .doc files "
        "(e.g. 'brew install --cask libreoffice')."
    )


def parse_doc(path: Path) -> ParsedDocument:
    soffice = _find_soffice()
    with tempfile.TemporaryDirectory(prefix="doc2docx-") as tmp_dir:
        result = subprocess.run(
            [soffice, "--headless", "--convert-to", "docx", "--outdir", tmp_dir, str(path)],
            capture_output=True,
            text=True,
            timeout=180,
        )
        converted = Path(tmp_dir) / f"{path.stem}.docx"
        if result.returncode != 0 or not converted.exists():
            raise RuntimeError(
                f"LibreOffice failed to convert '{path.name}' to docx. "
                f"returncode={result.returncode} stderr={result.stderr.strip()[:300]}"
            )
        parsed = parse_docx(converted)

    return ParsedDocument(
        document_id=path.stem,
        source_path=str(path),
        source_name=path.name,
        source_type="docx",
        markdown=parsed.markdown,
        metadata={**parsed.metadata, "converted_from": "doc"},
    )
