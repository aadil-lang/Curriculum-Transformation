from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from parsers.base import ParsedDocument
from parsers.docx_parser import parse_docx


# LibreOffice shares a single user profile by default, so two concurrent
# headless conversions collide: one silently no-ops (returncode 0, no output
# file). Each conversion below runs against an isolated profile via
# -env:UserInstallation; the lock serializes the brief spawn window as a
# belt-and-suspenders against any residual shared-state races.
_SOFFICE_LOCK = threading.Lock()


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
        "LibreOffice 'soffice' binary not found. Install LibreOffice to parse legacy .doc/.rtf files "
        "(e.g. 'brew install --cask libreoffice')."
    )


def parse_doc(path: Path) -> ParsedDocument:
    """Parse a legacy word-processing document (.doc or .rtf) via LibreOffice.

    LibreOffice converts both formats to .docx identically, so this handles
    Florida DOE-style .rtf curriculum frameworks as well as legacy .doc files.
    """
    soffice = _find_soffice()
    converted_from = path.suffix.lower().lstrip(".") or "doc"
    with tempfile.TemporaryDirectory(prefix="doc2docx-") as tmp_dir:
        profile_dir = Path(tmp_dir) / "profile"
        profile_uri = profile_dir.as_uri()
        command = [
            soffice,
            "--headless",
            f"-env:UserInstallation={profile_uri}",
            "--convert-to",
            "docx",
            "--outdir",
            tmp_dir,
            str(path),
        ]
        with _SOFFICE_LOCK:
            result = subprocess.run(
                command,
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
        metadata={**parsed.metadata, "converted_from": converted_from},
    )
