"""FastAPI wrapper for hosting the Data Transformation UI on Vibe.

Vibe serves this Python service behind a proxy that strips a base prefix, so all
routes are mounted under BASE_PREFIX ("/_vibes/main/py"). The heavy lifting is reused
verbatim from ui_server.py's transport-agnostic handlers — this module only maps HTTP
to those functions and serves the static UI.

Run locally:  uvicorn app.main:app --port 8001
Health check: GET {BASE_PREFIX}/_meta/health  and  GET /_meta/health
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

# The project modules (config, ui_server, batch_runner, ...) import each other as
# TOP-LEVEL names. Depending on how uvicorn is launched they live either beside this
# file (hosted: py-backend/app/*.py copied next to main.py) or one level up (local:
# app/main.py with the flat repo above). Put both on sys.path so top-level imports
# resolve either way, regardless of the launch CWD.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _candidate in (_HERE, os.path.dirname(_HERE)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from config import ROOT_DIR, get_settings, setup_logging
import ui_server

LOGGER = logging.getLogger(__name__)

# Base path the Vibe proxy routes to this service under. Overridable via env for
# local/other hosting. The frontend uses the same value to build API URLs.
BASE_PREFIX = os.getenv("VIBE_BASE_PREFIX", "/_vibes/main/py").rstrip("/")
UI_DIR = ROOT_DIR / "ui"

_STATIC_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}


def _create_app() -> FastAPI:
    setup_logging()
    settings = get_settings()
    UI_DIR.mkdir(parents=True, exist_ok=True)
    ui_server.CHAT_BATCH_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="Data Transformation Agent", docs_url=None, redoc_url=None)

    def _client_error(exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"error": str(exc)})

    def _run(handler, payload: dict[str, Any]) -> JSONResponse:
        try:
            return JSONResponse(content=handler(settings, payload))
        except ValueError as exc:
            return _client_error(exc)
        except Exception as exc:  # noqa: BLE001 - surface as 500 with message
            LOGGER.exception("Request failed: %s", exc)
            return JSONResponse(status_code=500, content={"error": str(exc)})

    # --- Static UI ---------------------------------------------------------
    def _serve_static(filename: str) -> Response:
        path = (UI_DIR / filename).resolve()
        if not path.exists() or path.parent != UI_DIR.resolve():
            raise HTTPException(status_code=404, detail="Static file not found.")
        content_type = _STATIC_CONTENT_TYPES.get(path.suffix, "application/octet-stream")
        body = path.read_bytes()
        # Inject the API base into index.html so the frontend targets the proxied routes.
        if path.name == "index.html":
            injected = (
                f"<script>window.__API_BASE__ = {BASE_PREFIX!r};</script>"
            ).encode("utf-8")
            text = body.replace(b"<head>", b"<head>\n    " + injected, 1)
            return HTMLResponse(content=text.decode("utf-8"))
        return Response(content=body, media_type=content_type, headers={"Cache-Control": "no-store"})

    @app.get("/", response_class=HTMLResponse)
    @app.get(f"{BASE_PREFIX}/", response_class=HTMLResponse)
    @app.get(BASE_PREFIX, response_class=HTMLResponse)
    def index() -> Response:
        return _serve_static("index.html")

    @app.get("/styles.css")
    @app.get(f"{BASE_PREFIX}/styles.css")
    def styles() -> Response:
        return _serve_static("styles.css")

    @app.get("/app.js")
    @app.get(f"{BASE_PREFIX}/app.js")
    def appjs() -> Response:
        return _serve_static("app.js")

    # --- Health (both prefixed and bare, per Vibe conventions) -------------
    @app.get(f"{BASE_PREFIX}/_meta/health")
    @app.get("/_meta/health")
    def health() -> dict[str, Any]:
        return {"success": True, "data": {"status": "ok", "service": "data-transformation-agent"}}

    # --- Read endpoints ----------------------------------------------------
    @app.get("/api/status")
    @app.get(f"{BASE_PREFIX}/api/status")
    def status() -> JSONResponse:
        return JSONResponse(content={"batches": ui_server.list_batch_summaries()})

    @app.get("/api/workspace")
    @app.get(f"{BASE_PREFIX}/api/workspace")
    def workspace() -> JSONResponse:
        return JSONResponse(content=ui_server.load_workspace_summary(settings))

    @app.get("/api/review-batches")
    @app.get(f"{BASE_PREFIX}/api/review-batches")
    def review_batches() -> JSONResponse:
        return JSONResponse(content={"batches": ui_server._reviewable_batches()})

    @app.get("/api/batches/{batch_name}")
    @app.get(f"{BASE_PREFIX}/api/batches/{{batch_name}}")
    def batch_detail(batch_name: str) -> JSONResponse:
        try:
            return JSONResponse(content=ui_server.load_batch_detail(batch_name))
        except ValueError as exc:
            return _client_error(exc)

    # --- Action endpoints --------------------------------------------------
    @app.post("/api/draft-sample")
    @app.post(f"{BASE_PREFIX}/api/draft-sample")
    async def draft_sample(request: Request) -> JSONResponse:
        return _run(ui_server.handle_draft_sample, await request.json())

    @app.post("/api/approve-sample")
    @app.post(f"{BASE_PREFIX}/api/approve-sample")
    async def approve_sample(request: Request) -> JSONResponse:
        return _run(ui_server.handle_approve_sample, await request.json())

    @app.post("/api/run-extraction")
    @app.post(f"{BASE_PREFIX}/api/run-extraction")
    async def run_extraction(request: Request) -> JSONResponse:
        return _run(ui_server.handle_run_extraction, await request.json())

    @app.post("/api/audit-batch")
    @app.post(f"{BASE_PREFIX}/api/audit-batch")
    async def audit_batch(request: Request) -> JSONResponse:
        return _run(ui_server.handle_audit_batch, await request.json())

    @app.post("/api/review-csv")
    @app.post(f"{BASE_PREFIX}/api/review-csv")
    async def review_csv(request: Request) -> JSONResponse:
        return _run(ui_server.handle_review_csv, await request.json())

    @app.post("/api/fix-reviewed-csv")
    @app.post(f"{BASE_PREFIX}/api/fix-reviewed-csv")
    async def fix_reviewed_csv(request: Request) -> JSONResponse:
        return _run(ui_server.handle_fix_reviewed_csv, await request.json())

    @app.post("/api/review-batch")
    @app.post(f"{BASE_PREFIX}/api/review-batch")
    async def review_batch(request: Request) -> JSONResponse:
        return _run(ui_server.handle_review_batch, await request.json())

    @app.post("/api/fix-reviewed-batch")
    @app.post(f"{BASE_PREFIX}/api/fix-reviewed-batch")
    async def fix_reviewed_batch(request: Request) -> JSONResponse:
        return _run(ui_server.handle_fix_reviewed_batch, await request.json())

    LOGGER.info("FastAPI app initialized. Base prefix: %s", BASE_PREFIX)
    return app


app = _create_app()
