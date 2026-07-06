from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from chat_batches import CHAT_BATCH_OUTPUT_DIR, ChatBatchRequest, ChatBatchSpec, run_chat_batches
from config import ROOT_DIR, Settings


UI_DIR = ROOT_DIR / "ui"
LOGGER = logging.getLogger(__name__)
MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024
VALID_BATCH_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


class UiServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], settings: Settings) -> None:
        super().__init__(server_address, UiRequestHandler)
        self.settings = settings


class UiRequestHandler(BaseHTTPRequestHandler):
    server: UiServer

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._serve_static("index.html", "text/html; charset=utf-8")
            return
        if parsed.path == "/styles.css":
            self._serve_static("styles.css", "text/css; charset=utf-8")
            return
        if parsed.path == "/app.js":
            self._serve_static("app.js", "application/javascript; charset=utf-8")
            return
        if parsed.path == "/api/status":
            self._send_json({"batches": list_batch_summaries()})
            return
        if parsed.path.startswith("/api/batches/"):
            batch_name = unquote(parsed.path.removeprefix("/api/batches/"))
            self._send_json(load_batch_detail(batch_name))
            return
        self._send_error_json(HTTPStatus.NOT_FOUND, "Endpoint not found.")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            payload = self._read_json_payload()
            if parsed.path == "/api/draft-sample":
                response = self._handle_draft_sample(payload)
                self._send_json(response)
                return
            if parsed.path == "/api/run-extraction":
                response = self._handle_run_extraction(payload)
                self._send_json(response)
                return
            self._send_error_json(HTTPStatus.NOT_FOUND, "Endpoint not found.")
        except ValueError as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("UI request failed: %s", exc)
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def log_message(self, format: str, *args: Any) -> None:
        LOGGER.info("UI %s - %s", self.address_string(), format % args)

    def _handle_draft_sample(self, payload: dict[str, Any]) -> dict[str, Any]:
        batch_name = validate_batch_name(payload.get("name"))
        instructions = str(payload.get("instructions", "")).strip()
        if not instructions:
            raise ValueError("Instructions are required to generate a draft sample CSV.")

        document_files = persist_document_uploads(batch_name, payload.get("document_files", []))
        write_batch_manifest(
            batch_name=batch_name,
            instructions=instructions,
            document_files=document_files,
        )

        request = ChatBatchRequest(
            batches=[
                ChatBatchSpec(
                    name=batch_name,
                    input_files=[str(path) for path in document_files],
                    instructions=instructions,
                    infer_schema=True,
                )
            ]
        )
        batch_result = run_chat_batches(request, self.server.settings)[0]
        return {
            "result": serialize_batch_result(batch_result),
            "batch": load_batch_detail(batch_name),
        }

    def _handle_run_extraction(self, payload: dict[str, Any]) -> dict[str, Any]:
        batch_name = validate_batch_name(payload.get("name"))
        instructions = str(payload.get("instructions", "")).strip()
        document_files = persist_document_uploads(batch_name, payload.get("document_files", []))
        if not document_files:
            document_files = list_existing_document_files(batch_name)
        if not document_files:
            raise ValueError("At least one source document is required before extraction can run.")

        sample_csv_content = str(payload.get("sample_csv_content", "")).strip()
        sample_csv_name = str(payload.get("sample_csv_name", "")).strip() or "approved_sample.csv"
        if sample_csv_content:
            sample_csv_path = persist_sample_csv(batch_name, sample_csv_name, sample_csv_content)
        else:
            sample_csv_path = find_existing_sample_csv(batch_name)
        if sample_csv_path is None:
            raise ValueError("An approved sample CSV is required before extraction can run.")

        write_batch_manifest(
            batch_name=batch_name,
            instructions=instructions,
            document_files=document_files,
        )

        request = ChatBatchRequest(
            batches=[
                ChatBatchSpec(
                    name=batch_name,
                    input_files=[str(path) for path in document_files],
                    sample_csv=str(sample_csv_path),
                    output_csv_name=str(payload.get("output_csv_name", "")).strip() or None,
                )
            ]
        )
        batch_result = run_chat_batches(request, self.server.settings)[0]
        return {
            "result": serialize_batch_result(batch_result),
            "batch": load_batch_detail(batch_name),
        }

    def _serve_static(self, filename: str, content_type: str) -> None:
        path = (UI_DIR / filename).resolve()
        if not path.exists() or path.parent != UI_DIR.resolve():
            self._send_error_json(HTTPStatus.NOT_FOUND, "Static file not found.")
            return
        self._send_bytes(HTTPStatus.OK, path.read_bytes(), content_type)

    def _read_json_payload(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            return {}
        body = self.rfile.read(content_length)
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, indent=2, default=str).encode("utf-8")
        self._send_bytes(status, data, "application/json; charset=utf-8")

    def _send_error_json(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"error": message}, status=status)

    def _send_bytes(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def serve_ui(settings: Settings, host: str, port: int) -> None:
    UI_DIR.mkdir(parents=True, exist_ok=True)
    CHAT_BATCH_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with UiServer((host, port), settings) as server:
        LOGGER.info("UI server listening on http://%s:%s", host, port)
        server.serve_forever()


def validate_batch_name(raw_value: Any) -> str:
    batch_name = str(raw_value or "").strip()
    if not batch_name:
        raise ValueError("Batch name is required.")
    if not VALID_BATCH_NAME.fullmatch(batch_name):
        raise ValueError("Batch name may only contain letters, numbers, dashes, and underscores.")
    return batch_name


def serialize_batch_result(result: Any) -> dict[str, Any]:
    return {
        "batch_name": result.batch_name,
        "schema_path": result.schema_path,
        "output_csv_path": result.output_csv_path,
        "manual_review_path": result.manual_review_path,
        "results": result.results,
        "mode": result.mode,
    }


def persist_document_uploads(batch_name: str, document_payloads: list[dict[str, Any]]) -> list[Path]:
    if not document_payloads:
        return list_existing_document_files(batch_name)

    input_dir = get_batch_root(batch_name) / "input_documents"
    input_dir.mkdir(parents=True, exist_ok=True)

    saved_paths: list[Path] = []
    for payload in document_payloads:
        saved_paths.append(_persist_uploaded_file(payload, input_dir))
    return saved_paths


def persist_sample_csv(batch_name: str, filename: str, content: str) -> Path:
    safe_name = sanitize_filename(filename or "approved_sample.csv")
    if not safe_name.lower().endswith(".csv"):
        safe_name = f"{safe_name}.csv"
    path = get_batch_root(batch_name) / safe_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def find_existing_sample_csv(batch_name: str) -> Path | None:
    batch_root = get_batch_root(batch_name)
    explicit = batch_root / "approved_sample.csv"
    if explicit.exists():
        return explicit

    candidates = sorted(batch_root.glob("*.csv"))
    for candidate in candidates:
        if candidate.name == "sample_output_template.csv":
            continue
        return candidate
    return None


def list_existing_document_files(batch_name: str) -> list[Path]:
    input_dir = get_batch_root(batch_name) / "input_documents"
    if not input_dir.exists():
        return []
    return sorted(path for path in input_dir.iterdir() if path.is_file())


def write_batch_manifest(batch_name: str, instructions: str, document_files: list[Path]) -> None:
    manifest_path = get_batch_root(batch_name) / "ui_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "batch_name": batch_name,
        "instructions": instructions,
        "document_files": [path.name for path in document_files],
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def list_batch_summaries() -> list[dict[str, Any]]:
    if not CHAT_BATCH_OUTPUT_DIR.exists():
        return []

    summaries: list[dict[str, Any]] = []
    for batch_root in sorted(CHAT_BATCH_OUTPUT_DIR.iterdir(), key=lambda path: path.stat().st_mtime, reverse=True):
        if not batch_root.is_dir():
            continue
        output_dir = batch_root / "output"
        output_csv = find_output_csv(output_dir)
        summaries.append(
            {
                "name": batch_root.name,
                "updated_at_utc": datetime.fromtimestamp(batch_root.stat().st_mtime, tz=timezone.utc).isoformat(),
                "document_count": len(list_existing_document_files(batch_root.name)),
                "status": derive_batch_status(batch_root),
                "has_schema": (output_dir / "schema_config.json").exists(),
                "has_sample_template": (output_dir / "sample_output_template.csv").exists(),
                "has_final_csv": output_csv is not None,
                "has_manual_review": (output_dir / "manual_review.json").exists(),
            }
        )
    return summaries


def load_batch_detail(batch_name: str) -> dict[str, Any]:
    batch_name = validate_batch_name(batch_name)
    batch_root = get_batch_root(batch_name)
    if not batch_root.exists():
        raise ValueError(f"Batch '{batch_name}' does not exist yet.")

    output_dir = batch_root / "output"
    manifest = read_json_if_exists(batch_root / "ui_manifest.json", {})
    output_csv = find_output_csv(output_dir)
    approved_sample_csv = find_existing_sample_csv(batch_name)
    return {
        "name": batch_name,
        "status": derive_batch_status(batch_root),
        "batch_root": str(batch_root),
        "document_files": [path.name for path in list_existing_document_files(batch_name)],
        "instructions": manifest.get("instructions", ""),
        "schema_config": read_json_if_exists(output_dir / "schema_config.json", {}),
        "sample_template_csv": read_text_if_exists(output_dir / "sample_output_template.csv"),
        "approved_sample_csv": read_text_if_exists(approved_sample_csv) if approved_sample_csv else "",
        "final_csv": read_text_if_exists(output_csv) if output_csv else "",
        "final_csv_path": str(output_csv) if output_csv else "",
        "manual_review": read_json_if_exists(output_dir / "manual_review.json", []),
        "processing_state": read_json_if_exists(output_dir / "processing_state.json", {}),
        "monitor_status": read_json_if_exists(output_dir / "monitor_status.json", {}),
    }


def derive_batch_status(batch_root: Path) -> str:
    output_dir = batch_root / "output"
    output_csv = find_output_csv(output_dir)
    manual_review_path = output_dir / "manual_review.json"
    approved_sample = find_existing_sample_csv(batch_root.name)
    if output_csv is not None:
        return "extracted"
    if manual_review_path.exists():
        return "manual_review"
    if approved_sample is not None:
        return "approved_ready"
    if (output_dir / "sample_output_template.csv").exists():
        return "draft_ready"
    if (batch_root / "input_documents").exists():
        return "uploaded"
    return "new"


def find_output_csv(output_dir: Path) -> Path | None:
    if not output_dir.exists():
        return None
    csv_candidates = sorted(path for path in output_dir.glob("*.csv") if path.name != "sample_output_template.csv")
    return csv_candidates[0] if csv_candidates else None


def read_text_if_exists(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def read_json_if_exists(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def get_batch_root(batch_name: str) -> Path:
    return CHAT_BATCH_OUTPUT_DIR / batch_name


def sanitize_filename(raw_name: str) -> str:
    filename = Path(raw_name).name
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._")
    return cleaned or "uploaded_file"


def _persist_uploaded_file(payload: dict[str, Any], target_dir: Path) -> Path:
    filename = sanitize_filename(str(payload.get("name", "")).strip())
    encoded = payload.get("content_base64")
    if not filename or not isinstance(encoded, str) or not encoded.strip():
        raise ValueError("Each uploaded file must include a filename and base64 content.")

    try:
        content = base64.b64decode(encoded, validate=True)
    except binascii.Error as exc:
        raise ValueError(f"Could not decode uploaded file '{filename}'.") from exc

    if len(content) > MAX_FILE_SIZE_BYTES:
        raise ValueError(f"Uploaded file '{filename}' exceeds the 20 MB UI limit.")

    path = target_dir / filename
    path.write_bytes(content)
    return path
