from __future__ import annotations

import argparse
from dataclasses import asdict
import importlib
import json
from pathlib import Path
import shutil
import sys

from chat_batches import (
    build_chat_batch_request,
    create_model_settings,
    create_schema_from_sample_csv,
    load_chat_batch_request,
    run_chat_batches,
)
from config import DEFAULT_SCHEMA_CONFIG_PATH, INPUT_DIR, OUTPUT_DIR, ROOT_DIR, Settings, ensure_directories, get_runtime_paths, get_settings, setup_logging
from csv_audit import audit_extracted_csv
from csv_finalization import finalize_extracted_csv
from google_sheets_sync import authorize_google_sheets_user
from pipeline import DataTransformationPipeline
from schemas import csv_headers, load_schema_config
from ui_server import list_batch_summaries, serve_ui
from verification import run_offline_smoke_verification


DEFAULT_SAMPLE_SCHEMA_COPY_PATH = ROOT_DIR / "default_sample_schema.csv"
DEFAULT_SCHEMA_SOURCE_PATH = ROOT_DIR / "default_schema_source.json"


def bootstrap_command() -> int:
    ensure_directories()
    print(f"Input directory: {INPUT_DIR}")
    print(f"Output directory: {OUTPUT_DIR}")
    print("Workspace bootstrap complete.")
    return 0


def verify_command(settings: Settings) -> int:
    ensure_directories()
    schema = load_schema_config()
    imports = [
        "crawl4ai",
        "fitz",
        "docx",
        "pydantic",
        "instructor",
        "pandas",
    ]

    report: dict[str, str] = {}
    for module_name in imports:
        module = importlib.import_module(module_name)
        version_value = getattr(module, "__version__", "imported")
        report[module_name] = version_value if isinstance(version_value, str) else str(version_value)

    print(json.dumps(
        {
            "python": sys.version,
            "schema_name": schema.schema_name,
            "csv_headers": csv_headers(),
            "imports": report,
            "input_dir_exists": INPUT_DIR.exists(),
            "output_dir_exists": OUTPUT_DIR.exists(),
            "gateway_mode": "portkey" if settings.portkey_api_key else "direct_provider_keys",
            "portkey_api_key_present": bool(settings.portkey_api_key),
            "watch_interval_seconds": settings.watch_interval_seconds,
            "max_retries": settings.max_retries,
        },
        indent=2,
    ))
    return 0


def run_once_command(settings: Settings) -> int:
    pipeline = DataTransformationPipeline(settings)
    results = pipeline.process_pending_documents()
    finalization_result = {}
    if pipeline.runtime_paths.csv_finalization_status_path.exists():
        finalization_result = json.loads(
            pipeline.runtime_paths.csv_finalization_status_path.read_text(encoding="utf-8")
        )
    sync_result = {}
    if pipeline.runtime_paths.sheet_sync_status_path.exists():
        sync_result = json.loads(pipeline.runtime_paths.sheet_sync_status_path.read_text(encoding="utf-8"))
    print(
        json.dumps(
            {
                "results": [
                    {
                        "path": str(result.path),
                        "status": result.status,
                        "message": result.message,
                        "stage": result.stage,
                    }
                    for result in results
                ],
                "csv_finalization": finalization_result,
                "google_sheets_sync": sync_result,
            },
            indent=2,
        )
    )
    return 0


def status_command() -> int:
    ensure_directories()
    settings = get_settings()
    batch_summaries = list_batch_summaries()
    schema = load_schema_config(str(DEFAULT_SCHEMA_CONFIG_PATH))
    final_csv_path = OUTPUT_DIR / "final_extracted_data.csv"
    manual_review_path = OUTPUT_DIR / "manual_review.json"
    monitor_status_path = OUTPUT_DIR / "monitor_status.json"
    analysis_dir = OUTPUT_DIR / "analysis_plans"
    review_dir = OUTPUT_DIR / "transformation_reviews"
    audit_dir = OUTPUT_DIR / "csv_audits"
    finalization_status_path = OUTPUT_DIR / "csv_finalization_status.json"
    sheet_sync_status_path = OUTPUT_DIR / "google_sheets_sync_status.json"

    input_files = sorted(path.name for path in INPUT_DIR.iterdir() if path.is_file())
    manual_review_entries = []
    if manual_review_path.exists():
        manual_review_entries = json.loads(manual_review_path.read_text(encoding="utf-8"))

    monitor_status = {}
    if monitor_status_path.exists():
        monitor_status = json.loads(monitor_status_path.read_text(encoding="utf-8"))

    default_schema_source = {}
    if DEFAULT_SCHEMA_SOURCE_PATH.exists():
        default_schema_source = json.loads(DEFAULT_SCHEMA_SOURCE_PATH.read_text(encoding="utf-8"))

    google_sheets_sync = {}
    if sheet_sync_status_path.exists():
        google_sheets_sync = json.loads(sheet_sync_status_path.read_text(encoding="utf-8"))

    csv_finalization = {}
    if finalization_status_path.exists():
        csv_finalization = json.loads(finalization_status_path.read_text(encoding="utf-8"))

    print(json.dumps(
        {
            "workspace_mode": "operating_data_transformation_agent",
            "operating_agent_mode": {
                "enabled": True,
                "control_surface": "natural_language_chat_plus_uploaded_or_linked_inputs",
                "default_schema_path": str(DEFAULT_SCHEMA_CONFIG_PATH),
                "requires_valid_critic_verdict_for_append": True,
            },
            "default_schema": {
                "path": str(DEFAULT_SCHEMA_CONFIG_PATH),
                "schema_name": schema.schema_name,
                "field_count": len(schema.fields),
                "sample_contract_present": schema.sample_contract is not None,
                "source": default_schema_source,
            },
            "llm_access": {
                "gateway_mode": "portkey" if settings.portkey_api_key else "direct_provider_keys",
                "portkey_api_key_present": bool(settings.portkey_api_key),
                "gemini_api_key_present": bool(settings.gemini_api_key),
                "openai_api_key_present": bool(settings.openai_api_key),
                "anthropic_api_key_present": bool(settings.anthropic_api_key),
            },
            "parallelism": {
                "extraction_max_workers": settings.extraction_max_workers,
                "batch_max_workers": settings.batch_max_workers,
            },
            "input_documents": {
                "count": len(input_files),
                "files": input_files,
            },
            "final_output": {
                "path": str(final_csv_path),
                "exists": final_csv_path.exists(),
            },
            "google_sheets_delivery": {
                "enabled": bool(settings.google_sheets_sync_enabled),
                "spreadsheet_id_present": bool(settings.google_sheets_spreadsheet_id),
                "service_account_present": bool(
                    settings.google_service_account_json or settings.google_service_account_json_path
                ),
                "oauth_client_secret_present": bool(settings.google_oauth_client_secret_path),
                "oauth_token_present": Path(settings.google_oauth_token_path).expanduser().exists(),
                "oauth_token_path": settings.google_oauth_token_path,
                "status_path": str(sheet_sync_status_path),
                "last_sync": google_sheets_sync,
            },
            "csv_finalization": {
                "status_path": str(finalization_status_path),
                "last_finalization": csv_finalization,
            },
            "manual_review": {
                "path": str(manual_review_path),
                "entries": len(manual_review_entries),
            },
            "analysis_artifacts": {
                "path": str(analysis_dir),
                "count": len(list(analysis_dir.glob("*.analysis.json"))) if analysis_dir.exists() else 0,
            },
            "transformation_reviews": {
                "path": str(review_dir),
                "count": len(list(review_dir.glob("*.review.json"))) if review_dir.exists() else 0,
            },
            "csv_audits": {
                "path": str(audit_dir),
                "count": len(list(audit_dir.glob("*.audit_report.json"))) if audit_dir.exists() else 0,
            },
            "monitor_status": monitor_status,
            "chat_batches": batch_summaries,
            "next_actions": [
                "Use a sample CSV from chat when one is provided.",
                "Otherwise use the workspace default schema for extraction runs.",
                "Append rows only after the critic returns VALID.",
            ],
        },
        indent=2,
    ))
    return 0


def set_default_schema_command(args: argparse.Namespace) -> int:
    ensure_directories()
    sample_csv_path = Path(args.sample_csv).expanduser().resolve()
    schema_config = create_schema_from_sample_csv(sample_csv_path, "workspace_default")
    DEFAULT_SCHEMA_CONFIG_PATH.write_text(
        json.dumps(schema_config.model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )
    shutil.copy2(sample_csv_path, DEFAULT_SAMPLE_SCHEMA_COPY_PATH)
    DEFAULT_SCHEMA_SOURCE_PATH.write_text(
        json.dumps(
            {
                "sample_csv_path": str(sample_csv_path),
                "workspace_copy_path": str(DEFAULT_SAMPLE_SCHEMA_COPY_PATH),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "message": "Default workspace schema updated.",
                "schema_path": str(DEFAULT_SCHEMA_CONFIG_PATH),
                "schema_name": schema_config.schema_name,
                "field_count": len(schema_config.fields),
                "sample_csv_path": str(sample_csv_path),
                "workspace_copy_path": str(DEFAULT_SAMPLE_SCHEMA_COPY_PATH),
            },
            indent=2,
        )
    )
    return 0


def watch_command(settings: Settings) -> int:
    pipeline = DataTransformationPipeline(settings)
    print(f"Watching {INPUT_DIR} every {settings.watch_interval_seconds} seconds...")
    pipeline.watch_forever()
    return 0


def chat_batch_command(args: argparse.Namespace, settings: Settings) -> int:
    if args.config:
        request = load_chat_batch_request(args.config)
    else:
        if not args.name:
            raise SystemExit("chat-batch requires --name when --config is not provided.")
        if not args.files and not args.instructions and not args.sample_csv:
            raise SystemExit(
                "chat-batch requires input files, a sample CSV, or plain-English --instructions for schema/sample generation."
            )
        request = build_chat_batch_request(
            name=args.name,
            files=args.files,
            sample_csv=args.sample_csv,
            instructions=args.instructions,
            infer_schema=args.infer_schema,
            draft_only=args.draft_only,
            output_csv_name=args.output_csv_name,
        )

    results = run_chat_batches(request, settings)
    print(json.dumps([asdict(result) for result in results], indent=2))
    return 0


def offline_verify_command() -> int:
    result = run_offline_smoke_verification()
    print(json.dumps(asdict(result), indent=2))
    return 0


def ui_command(args: argparse.Namespace, settings: Settings) -> int:
    ensure_directories()
    print(f"UI available at http://{args.host}:{args.port}")
    serve_ui(settings=settings, host=args.host, port=args.port)
    return 0


def audit_csv_command(args: argparse.Namespace, settings: Settings) -> int:
    ensure_directories()
    result = audit_extracted_csv(
        Path(args.audit_csv),
        settings,
        sample_csv_path=Path(args.sample_csv) if args.sample_csv else None,
    )
    print(json.dumps(asdict(result), indent=2))
    return 0


def sync_sheet_command(args: argparse.Namespace, settings: Settings) -> int:
    ensure_directories()
    csv_path = Path(args.csv).expanduser().resolve() if args.csv else OUTPUT_DIR / "final_extracted_data.csv"
    active_settings = _resolve_settings_for_csv(csv_path, settings)
    runtime_paths = _resolve_runtime_paths_for_csv(csv_path)
    result = finalize_extracted_csv(
        csv_path,
        active_settings,
        runtime_paths,
        sync_to_sheets=True,
        audit_before_sync=not args.sample,
    )
    print(json.dumps(asdict(result), indent=2))
    return 0


def google_oauth_login_command(args: argparse.Namespace, settings: Settings) -> int:
    ensure_directories()
    result = authorize_google_sheets_user(
        settings,
        client_secret_path=args.client_secret,
        open_browser=not args.no_browser,
        port=args.port,
    )
    print(json.dumps(asdict(result), indent=2))
    return 0


def _resolve_settings_for_csv(csv_path: Path, settings: Settings) -> Settings:
    schema_path = csv_path.parent / "schema_config.json"
    if schema_path.exists():
        return create_model_settings(settings, schema_path)
    return settings


def _resolve_runtime_paths_for_csv(csv_path: Path):
    schema_path = csv_path.parent / "schema_config.json"
    if schema_path.exists() and csv_path.parent.name == "output":
        return get_runtime_paths(csv_path.parent.parent)
    return get_runtime_paths()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Autonomous Data Transformation Agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("bootstrap", help="Create required directories.")
    set_default_schema = subparsers.add_parser("set-default-schema", help="Set the default workspace schema from a sample CSV.")
    set_default_schema.add_argument("--sample-csv", required=True, help="Path to the approved sample CSV to use as the default schema.")
    subparsers.add_parser("status", help="Show the current workspace extraction state.")
    subparsers.add_parser("verify", help="Run environment and dependency verification.")
    subparsers.add_parser("offline-verify", help="Run isolated offline end-to-end pipeline verification.")
    audit_csv = subparsers.add_parser("audit-csv", help="Audit an extracted CSV against its source documents and the active sample contract.")
    audit_csv.add_argument("--audit-csv", required=True, help="Path to the extracted CSV to audit.")
    audit_csv.add_argument("--sample-csv", help="Optional sample CSV to use as the audit contract instead of the workspace default schema.")
    sync_sheet = subparsers.add_parser("sync-sheet", help="Sync a CSV to Google Sheets only when you explicitly run this command. Full extracted CSVs are audited first; approved sample CSVs can use --sample.")
    sync_sheet.add_argument("--csv", help="Optional path to the CSV to sync. Defaults to output/final_extracted_data.csv.")
    sync_sheet.add_argument("--sample", action="store_true", help="Treat the CSV as an approved sample and sync it without running the full extracted-CSV audit.")
    oauth_login = subparsers.add_parser("google-oauth-login", help="Authorize Google Sheets sync using a Google OAuth client secret.")
    oauth_login.add_argument("--client-secret", help="Optional path to the OAuth client secret JSON.")
    oauth_login.add_argument("--no-browser", action="store_true", help="Print the authorization URL instead of opening a browser automatically.")
    oauth_login.add_argument("--port", type=int, default=8787, help="Local callback port for the OAuth flow. Default: 8787.")
    subparsers.add_parser("run-once", help="Process all currently pending documents one time.")
    subparsers.add_parser("watch", help="Continuously monitor input_documents for new files.")
    ui_parser = subparsers.add_parser("ui", help="Start the local workspace UI.")
    ui_parser.add_argument("--host", default="127.0.0.1", help="Host interface for the UI server.")
    ui_parser.add_argument("--port", type=int, default=8765, help="Port for the UI server.")
    chat_batch = subparsers.add_parser("chat-batch", help="Run one or more chat-style extraction batches from local files or pasted URLs.")
    chat_batch.add_argument("--config", help="Path to a JSON file describing multiple extraction batches.")
    chat_batch.add_argument("--name", help="Batch name for single-batch execution.")
    chat_batch.add_argument("--sample-csv", help="Optional sample CSV path or URL to derive the target schema.")
    chat_batch.add_argument("--instructions", help="Plain-English output instructions to generate a draft schema/sample CSV.")
    chat_batch.add_argument("--infer-schema", action="store_true", help="Infer a draft schema when no sample CSV is provided.")
    chat_batch.add_argument("--draft-only", action="store_true", help="Only create schema/template outputs without extraction.")
    chat_batch.add_argument("--output-csv-name", help="Optional filename for the generated batch CSV.")
    chat_batch.add_argument("files", nargs="*", help="Input document paths or document/website URLs for single-batch execution.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    setup_logging()
    settings = get_settings()

    if args.command == "bootstrap":
        return bootstrap_command()
    if args.command == "set-default-schema":
        return set_default_schema_command(args)
    if args.command == "status":
        return status_command()
    if args.command == "verify":
        return verify_command(settings)
    if args.command == "offline-verify":
        return offline_verify_command()
    if args.command == "audit-csv":
        return audit_csv_command(args, settings)
    if args.command == "sync-sheet":
        return sync_sheet_command(args, settings)
    if args.command == "google-oauth-login":
        return google_oauth_login_command(args, settings)
    if args.command == "run-once":
        return run_once_command(settings)
    if args.command == "watch":
        return watch_command(settings)
    if args.command == "ui":
        return ui_command(args, settings)
    if args.command == "chat-batch":
        return chat_batch_command(args, settings)

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
