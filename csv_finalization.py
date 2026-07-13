from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from config import RuntimePaths, Settings
from csv_audit import audit_extracted_csv
from schemas import REVIEW_ISSUES_COLUMN, REVIEW_STATUS_COLUMN, get_output_column_name, get_schema_fields


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
    # Keep the exact sample columns, then append the review markers so unfixed
    # rows remain visible and flagged in the deliverable rather than dropped.
    output_columns = [*schema_columns, REVIEW_STATUS_COLUMN, REVIEW_ISSUES_COLUMN]
    clean_path = full_csv_path.with_name(f"{full_csv_path.stem}.clean.csv")
    with full_csv_path.open(newline="", encoding="utf-8-sig") as src:
        reader = csv.DictReader(src)
        with clean_path.open("w", newline="", encoding="utf-8") as dst:
            writer = csv.DictWriter(dst, fieldnames=output_columns, extrasaction="ignore")
            writer.writeheader()
            for row in reader:
                writer.writerow({col: row.get(col, "") for col in output_columns})
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
    finalized_at_utc: str


def finalize_extracted_csv(
    csv_path: Path,
    settings: Settings,
    runtime_paths: RuntimePaths,
    *,
    audit_csv: bool = True,
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
            finalized_at_utc=finalized_at_utc,
        )
        _write_status(runtime_paths.csv_finalization_status_path, result)
        return result

    audit_report_path = ""
    rows_audited = 0
    issue_count = 0
    audit_passed = not audit_csv

    if audit_csv:
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
            finalized_at_utc=finalized_at_utc,
        )
        _write_status(runtime_paths.csv_finalization_status_path, result)
        return result

    result = CsvFinalizationResult(
        status="passed_audit" if audit_csv else "approved_sample_ready",
        message="CSV passed audit and is ready to download." if audit_csv else "Approved sample CSV is ready.",
        csv_path=str(csv_path),
        audit_report_path=audit_report_path,
        rows_audited=rows_audited,
        issue_count=issue_count,
        audit_passed=audit_passed,
        finalized_at_utc=finalized_at_utc,
    )
    _write_status(runtime_paths.csv_finalization_status_path, result)
    return result


def _write_status(path: Path, result: CsvFinalizationResult) -> None:
    path.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
