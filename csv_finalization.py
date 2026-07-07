from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from config import RuntimePaths, Settings
from csv_audit import audit_extracted_csv
from google_sheets_sync import sync_csv_to_configured_google_sheet
from schemas import get_output_column_name, get_schema_fields


def write_schema_only_csv(full_csv_path: Path, settings: Settings) -> Path | None:
    """Write a deliverable CSV containing only the sample contract's columns.

    The internal CSV interleaves pipeline metadata (source_document, ...) and a
    *_source_citation column per field, needed for audit and collision checks.
    The deliverable keeps just the schema's output columns, in order, matching
    the approved sample CSV exactly. Returns the clean path, or None if the
    source CSV is absent.
    """
    if not full_csv_path.exists():
        return None
    schema_columns = [
        get_output_column_name(spec)
        for spec in get_schema_fields(str(settings.schema_config_path))
    ]
    clean_path = full_csv_path.with_name(f"{full_csv_path.stem}.clean.csv")
    with full_csv_path.open(newline="", encoding="utf-8-sig") as src:
        reader = csv.DictReader(src)
        with clean_path.open("w", newline="", encoding="utf-8") as dst:
            writer = csv.DictWriter(dst, fieldnames=schema_columns, extrasaction="ignore")
            writer.writeheader()
            for row in reader:
                writer.writerow({col: row.get(col, "") for col in schema_columns})
    return clean_path


@dataclass(slots=True)
class CsvFinalizationResult:
    status: str
    message: str
    csv_path: str
    audit_report_path: str
    rows_audited: int
    issue_count: int
    audit_passed: bool
    sync_status: str
    sync_message: str
    sheet_name: str
    finalized_at_utc: str


def finalize_extracted_csv(
    csv_path: Path,
    settings: Settings,
    runtime_paths: RuntimePaths,
    *,
    sync_to_sheets: bool = False,
    audit_before_sync: bool = True,
) -> CsvFinalizationResult:
    csv_path = csv_path.expanduser().resolve()
    finalized_at_utc = datetime.now(timezone.utc).isoformat()

    if not csv_path.exists():
        result = CsvFinalizationResult(
            status="missing_csv",
            message="CSV file does not exist; nothing to finalize.",
            csv_path=str(csv_path),
            audit_report_path="",
            rows_audited=0,
            issue_count=0,
            audit_passed=False,
            sync_status="not_run",
            sync_message="Sync skipped because the CSV file does not exist.",
            sheet_name="",
            finalized_at_utc=finalized_at_utc,
        )
        _write_status(runtime_paths.csv_finalization_status_path, result)
        return result

    audit_report_path = ""
    rows_audited = 0
    issue_count = 0
    audit_passed = not audit_before_sync

    if audit_before_sync:
        audit_result = audit_extracted_csv(csv_path, settings, runtime_paths=runtime_paths)
        audit_report_path = audit_result.report_path
        rows_audited = audit_result.rows_audited
        issue_count = audit_result.issue_count
        audit_passed = audit_result.issue_count == 0

    # Always emit the clean, schema-only deliverable once all stages have run,
    # regardless of audit outcome — the audit is advisory, not a gate on output.
    write_schema_only_csv(csv_path, settings)

    if not audit_passed:
        result = CsvFinalizationResult(
            status="failed_audit",
            message="CSV audit found issues.",
            csv_path=str(csv_path),
            audit_report_path=audit_report_path,
            rows_audited=rows_audited,
            issue_count=issue_count,
            audit_passed=False,
            sync_status="blocked_by_audit",
            sync_message=(
                "Sync skipped because the finalized CSV did not pass audit."
                if sync_to_sheets
                else "Sync was not requested, and the finalized CSV did not pass audit."
            ),
            sheet_name="",
            finalized_at_utc=finalized_at_utc,
        )
        _write_status(runtime_paths.csv_finalization_status_path, result)
        return result

    if not sync_to_sheets:
        result = CsvFinalizationResult(
            status="passed_audit" if audit_before_sync else "approved_sample_ready",
            message=(
                "CSV passed audit. Google Sheets sync was not requested."
                if audit_before_sync
                else "Approved sample CSV is ready. Google Sheets sync was not requested."
            ),
            csv_path=str(csv_path),
            audit_report_path=audit_report_path,
            rows_audited=rows_audited,
            issue_count=issue_count,
            audit_passed=audit_passed,
            sync_status="not_requested",
            sync_message="Sync was not requested for this finalization run.",
            sheet_name="",
            finalized_at_utc=finalized_at_utc,
        )
        _write_status(runtime_paths.csv_finalization_status_path, result)
        return result

    sync_result = sync_csv_to_configured_google_sheet(csv_path, settings, runtime_paths)
    sync_completed = sync_result.status in {"synced", "up_to_date"}
    sync_skipped = sync_result.status in {"disabled", "not_configured"}
    if sync_completed:
        status = "passed_audit_synced" if audit_before_sync else "approved_sample_synced"
        message = (
            "CSV passed audit and was finalized for Google Sheets delivery."
            if audit_before_sync
            else "Approved sample CSV synced to Google Sheets."
        )
    elif sync_skipped:
        status = "passed_audit_sync_skipped" if audit_before_sync else "approved_sample_sync_skipped"
        message = (
            "CSV passed audit, but Google Sheets sync is disabled or not configured."
            if audit_before_sync
            else "Approved sample CSV could not sync because Google Sheets sync is disabled or not configured."
        )
    else:
        status = "passed_audit_sync_failed" if audit_before_sync else "approved_sample_sync_failed"
        message = (
            "CSV passed audit, but Google Sheets sync failed."
            if audit_before_sync
            else "Approved sample CSV sync failed."
        )

    result = CsvFinalizationResult(
        status=status,
        message=message,
        csv_path=str(csv_path),
        audit_report_path=audit_report_path,
        rows_audited=rows_audited,
        issue_count=issue_count,
        audit_passed=audit_passed,
        sync_status=sync_result.status,
        sync_message=sync_result.message,
        sheet_name=sync_result.sheet_name,
        finalized_at_utc=finalized_at_utc,
    )
    _write_status(runtime_paths.csv_finalization_status_path, result)
    return result


def _write_status(path: Path, result: CsvFinalizationResult) -> None:
    path.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
