from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from agent_engine import _extract_message_text
from config import ROOT_DIR, RuntimePaths, Settings, get_runtime_paths
from parsers.router import parse_input
from schemas import load_schema_config


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class CsvAuditResult:
    audit_csv_path: str
    report_path: str
    rows_audited: int
    issue_count: int


class CsvAuditIssue(BaseModel):
    column_name: str = ""
    issue_type: str
    issue_message: str
    suggested_fix: str = ""


class CsvRowAuditVerdict(BaseModel):
    tag: Literal["VALID", "INVALID"]
    issues: list[CsvAuditIssue] = Field(default_factory=list)
    confidence_notes: str = ""


class CsvBatchAuditFinding(BaseModel):
    row_number: int
    tag: Literal["VALID", "INVALID"] = "VALID"
    issues: list[CsvAuditIssue] = Field(default_factory=list)
    confidence_notes: str = ""


class CsvBatchAuditVerdict(BaseModel):
    findings: list[CsvBatchAuditFinding] = Field(default_factory=list)
    confidence_notes: str = ""


def audit_extracted_csv(
    audit_csv_path: Path,
    settings: Settings,
    sample_csv_path: Path | None = None,
    runtime_paths: RuntimePaths | None = None,
) -> CsvAuditResult:
    audit_csv_path = audit_csv_path.expanduser().resolve()
    active_runtime_paths = runtime_paths or get_runtime_paths(ROOT_DIR)
    report_dir = active_runtime_paths.output_dir / "csv_audits"
    report_dir.mkdir(parents=True, exist_ok=True)

    active_settings = settings
    if sample_csv_path is not None:
        from chat_batches import create_model_settings, create_schema_from_sample_csv

        schema_config = create_schema_from_sample_csv(sample_csv_path.expanduser().resolve(), "csv_audit")
        schema_path = report_dir / f"{audit_csv_path.stem}.schema_config.json"
        schema_path.write_text(json.dumps(schema_config.model_dump(mode="json"), indent=2), encoding="utf-8")
        active_settings = create_model_settings(settings, schema_path)

    schema_config = load_schema_config(str(active_settings.schema_config_path), settings=active_settings)
    report_rows: list[dict[str, Any]] = []
    issue_count = 0
    rows_audited = 0
    parsed_document_cache: dict[str, Any] = {}
    rows_by_source: dict[str, list[tuple[int, dict[str, str]]]] = {}
    display_standard_code_rows: dict[str, list[int]] = {}
    display_standard_code_column = _get_display_standard_code_column(schema_config)

    with audit_csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row_number, raw_row in enumerate(reader, start=2):
            rows_audited += 1
            source_reference = (raw_row.get("source") or "").strip()
            if not source_reference:
                issues = [
                    CsvAuditIssue(
                        column_name="source",
                        issue_type="missing_source",
                        issue_message="Row does not contain a usable source reference.",
                    )
                ]
                report_rows.extend(_render_report_rows(row_number, source_reference, issues, "INVALID"))
                issue_count += len(issues)
                continue
            if display_standard_code_column:
                display_code = (raw_row.get(display_standard_code_column) or "").strip()
                if display_code:
                    display_standard_code_rows.setdefault(display_code, []).append(row_number)
            rows_by_source.setdefault(source_reference, []).append((row_number, raw_row))

    duplicate_display_standard_codes = {
        code: row_numbers
        for code, row_numbers in display_standard_code_rows.items()
        if len(row_numbers) > 1
    }

    for source_reference, grouped_rows in rows_by_source.items():
        try:
            parsed_document = _load_source_document(source_reference, runtime_paths, parsed_document_cache)
        except Exception as exc:
            issues = [
                CsvAuditIssue(
                    column_name="source",
                    issue_type="source_load_error",
                    issue_message=str(exc),
                )
            ]
            for row_number, _raw_row in grouped_rows:
                report_rows.extend(_render_report_rows(row_number, source_reference, issues, "INVALID"))
                issue_count += len(issues)
            continue

        for row_chunk in _chunk_rows(grouped_rows, size=12):
            batch_verdict = _audit_rows_with_model(row_chunk, parsed_document, schema_config, active_settings)
            finding_map = {finding.row_number: finding for finding in batch_verdict.findings}

            for row_number, raw_row in row_chunk:
                programmatic_issues = _programmatic_row_issues(
                    raw_row,
                    schema_config,
                    row_number=row_number,
                    duplicate_display_standard_codes=duplicate_display_standard_codes,
                )
                finding = finding_map.get(row_number)
                model_issues = finding.issues if finding else [
                    CsvAuditIssue(
                        column_name="",
                        issue_type="missing_row_audit",
                        issue_message="Audit model did not return a finding for this row.",
                    )
                ]
                model_tag = finding.tag if finding else "INVALID"
                combined_issues = [*programmatic_issues, *model_issues]

                if combined_issues:
                    issue_count += len(combined_issues)
                    final_tag = "INVALID" if model_tag == "INVALID" or combined_issues else "VALID"
                    report_rows.extend(_render_report_rows(row_number, source_reference, combined_issues, final_tag))

    report_path = report_dir / f"{audit_csv_path.stem}.audit_report.json"
    report_path.write_text(json.dumps(report_rows, indent=2), encoding="utf-8")
    return CsvAuditResult(
        audit_csv_path=str(audit_csv_path),
        report_path=str(report_path),
        rows_audited=rows_audited,
        issue_count=issue_count,
    )


def _programmatic_row_issues(
    raw_row: dict[str, str],
    schema_config: Any,
    *,
    row_number: int,
    duplicate_display_standard_codes: dict[str, list[int]],
) -> list[CsvAuditIssue]:
    issues: list[CsvAuditIssue] = []
    contract = schema_config.sample_contract
    required_columns = contract.required_columns if contract else []
    display_standard_code_column = _get_display_standard_code_column(schema_config)

    for column_name in required_columns:
        if column_name not in raw_row:
            issues.append(
                CsvAuditIssue(
                    column_name=column_name,
                    issue_type="missing_required_column",
                    issue_message=f"Required column '{column_name}' is not present in the audited CSV.",
                )
            )
            continue
        if not (raw_row.get(column_name) or "").strip():
            issues.append(
                CsvAuditIssue(
                    column_name=column_name,
                    issue_type="missing_required_value",
                    issue_message=f"Required column '{column_name}' is blank.",
                )
            )

    if display_standard_code_column:
        display_code = (raw_row.get(display_standard_code_column) or "").strip()
        duplicate_rows = duplicate_display_standard_codes.get(display_code, [])
        if display_code and len(duplicate_rows) > 1:
            joined_rows = ", ".join(str(number) for number in duplicate_rows)
            issues.append(
                CsvAuditIssue(
                    column_name=display_standard_code_column,
                    issue_type="duplicate_display_standard_code",
                    issue_message=(
                        f"Display standard code '{display_code}' is duplicated in CSV rows {joined_rows}. "
                        "Display standard code must be unique within the CSV."
                    ),
                    suggested_fix=(
                        "If one standard genuinely spans multiple topics, merge those topic names with ' | ' where appropriate. "
                        "Otherwise disambiguate the repeated code by adding a short domain code or topic code prefix such as 'DA.1.1' when supported by the source structure."
                    ),
                )
            )

    return issues


def _get_display_standard_code_column(schema_config: Any) -> str | None:
    for spec in schema_config.fields:
        if spec.name == "display_standard_code":
            return spec.output_column or spec.name
    return None


def _audit_row_with_model(
    raw_row: dict[str, str],
    parsed_document: Any,
    schema_config: Any,
    settings: Settings,
) -> CsvRowAuditVerdict:
    messages = _build_audit_messages(raw_row, parsed_document, schema_config)

    if settings.portkey_api_key:
        return _call_portkey_json(CsvRowAuditVerdict, messages, settings)
    provider = settings.critic_provider
    if provider == "openai":
        return _call_openai_json(CsvRowAuditVerdict, messages, settings)
    if provider == "anthropic":
        return _call_anthropic_json(CsvRowAuditVerdict, messages, settings)

    return CsvRowAuditVerdict(
        tag="VALID",
        issues=[],
        confidence_notes="No supported audit model configured; only programmatic checks were run.",
    )


def _audit_rows_with_model(
    row_chunk: list[tuple[int, dict[str, str]]],
    parsed_document: Any,
    schema_config: Any,
    settings: Settings,
) -> CsvBatchAuditVerdict:
    messages = _build_batch_audit_messages(row_chunk, parsed_document, schema_config)

    if settings.portkey_api_key:
        return _call_portkey_json(CsvBatchAuditVerdict, messages, settings)
    provider = settings.critic_provider
    if provider == "openai":
        return _call_openai_json(CsvBatchAuditVerdict, messages, settings)
    if provider == "anthropic":
        return _call_anthropic_json(CsvBatchAuditVerdict, messages, settings)

    return CsvBatchAuditVerdict(
        findings=[
            CsvBatchAuditFinding(row_number=row_number, tag="VALID", issues=[], confidence_notes="")
            for row_number, _raw_row in row_chunk
        ],
        confidence_notes="No supported audit model configured; only programmatic checks were run.",
    )


def _build_audit_messages(raw_row: dict[str, str], parsed_document: Any, schema_config: Any) -> list[dict[str, str]]:
    row_json = json.dumps({key: (value or "").strip() for key, value in raw_row.items()}, indent=2)
    audit_contract = schema_config.sample_contract.model_copy(deep=True) if schema_config.sample_contract else None
    if audit_contract is not None:
        audit_contract.required_columns = [
            column
            for column in audit_contract.required_columns
            if column not in {"Standard code", "czi_standard_code"}
        ]
    contract_json = (
        json.dumps(audit_contract.model_dump(mode="json"), indent=2)
        if audit_contract
        else "null"
    )

    prompt = f"""
Audit this extracted CSV row against the original source document and the approved sample contract.

Important audit rules:
- Audit the row directly from the source link content, not from final extraction metadata requirements.
- `Standard code` may be empty and must not be flagged solely for being blank.
- `czi_standard_code` may be empty or absent and must not be flagged solely for being blank or missing.
- `Display standard code` may be synthetic/transformed or source-faithful if that matches the sample contract.
- `topic` may contain multiple source-supported topic names joined with ` | ` when one standard genuinely spans multiple topics and the approved sample contract allows that topic style.
- `Display standard code` must never be duplicated within the same CSV; if duplicates appear, mark every affected row `INVALID`.
- A prefixed display code such as `DA.1.1` or `TOP.1.1` is acceptable when needed to keep the display code unique and the approved sample contract allows that display-code style.
- Detect row-level transformation issues such as wrong subject/domain/topic placement, incorrect display-grade logic, noise, row contamination, truncation, bad merges, and unsupported description wording.
- Do not require `_source_citation` columns in this audit mode.
- Return `VALID` only if the row is materially consistent with the source and sample contract.
- Return `INVALID` with explicit issues when any field looks wrong, unsupported, noisy, or structurally mis-mapped.

Sample CSV transformation contract:
{contract_json}

CSV row to audit:
{row_json}

Original source markdown:
{parsed_document.markdown}
""".strip()

    return [
        {
            "role": "system",
            "content": "You are a strict CSV audit agent. Judge extracted rows against their original source and report concrete row-level issues.",
        },
        {"role": "user", "content": prompt},
    ]


def _build_batch_audit_messages(
    row_chunk: list[tuple[int, dict[str, str]]],
    parsed_document: Any,
    schema_config: Any,
) -> list[dict[str, str]]:
    rows_json = json.dumps(
        [
            {
                "row_number": row_number,
                "row": {key: (value or "").strip() for key, value in raw_row.items()},
            }
            for row_number, raw_row in row_chunk
        ],
        indent=2,
    )
    audit_contract = schema_config.sample_contract.model_copy(deep=True) if schema_config.sample_contract else None
    if audit_contract is not None:
        audit_contract.required_columns = [
            column
            for column in audit_contract.required_columns
            if column not in {"Standard code", "czi_standard_code"}
        ]
    contract_json = json.dumps(audit_contract.model_dump(mode="json"), indent=2) if audit_contract else "null"

    prompt = f"""
Audit these extracted CSV rows against the original source document and the approved sample contract.

Important audit rules:
- Audit every row directly from the source link content, not from final extraction metadata requirements.
- Return one finding for every input row_number.
- `Standard code` may be empty and must not be flagged solely for being blank.
- `czi_standard_code` may be empty or absent and must not be flagged solely for being blank or missing.
- `Display standard code` may be synthetic/transformed or source-faithful if that matches the sample contract.
- `topic` may contain multiple source-supported topic names joined with ` | ` when one standard genuinely spans multiple topics and the approved sample contract allows that topic style.
- `Display standard code` must never be duplicated within the same CSV; if duplicates appear, mark every affected row `INVALID`.
- A prefixed display code such as `DA.1.1` or `TOP.1.1` is acceptable when needed to keep the display code unique and the approved sample contract allows that display-code style.
- Detect row-level transformation issues such as wrong subject/domain/topic placement, incorrect display-grade logic, noise, row contamination, truncation, bad merges, unsupported description wording, and hierarchy mistakes.
- Do not require `_source_citation` columns in this audit mode.
- Return `VALID` only if the row is materially consistent with the source and sample contract.
- Return `INVALID` with explicit issues when any field looks wrong, unsupported, noisy, or structurally mis-mapped.

Sample CSV transformation contract:
{contract_json}

CSV rows to audit:
{rows_json}

Original source markdown:
{parsed_document.markdown}
""".strip()

    return [
        {
            "role": "system",
            "content": "You are a strict CSV audit agent. Judge extracted rows against their original source and report concrete row-level issues for each row_number.",
        },
        {"role": "user", "content": prompt},
    ]


def _call_openai_json(response_model: type[BaseModel], messages: list[dict[str, str]], settings: Settings) -> BaseModel:
    if not settings.openai_api_key:
        return response_model.model_validate({"tag": "VALID", "issues": [], "confidence_notes": "OPENAI_API_KEY not configured."})

    import instructor
    from openai import OpenAI

    client = instructor.from_openai(OpenAI(api_key=settings.openai_api_key))
    return client.chat.completions.create(
        model=settings.critic_model,
        response_model=response_model,
        messages=messages,
        max_retries=0,
    )


def _call_anthropic_json(response_model: type[BaseModel], messages: list[dict[str, str]], settings: Settings) -> BaseModel:
    if not settings.anthropic_api_key:
        return response_model.model_validate({"tag": "VALID", "issues": [], "confidence_notes": "ANTHROPIC_API_KEY not configured."})

    import instructor
    from anthropic import Anthropic

    client = instructor.from_anthropic(
        Anthropic(api_key=settings.anthropic_api_key),
        mode=instructor.Mode.ANTHROPIC_JSON,
    )
    return client.messages.create(
        model=settings.critic_model,
        response_model=response_model,
        messages=messages,
        max_retries=0,
    )


def _call_portkey_json(response_model: type[BaseModel], messages: list[dict[str, str]], settings: Settings) -> BaseModel:
    from portkey_client import call_portkey_structured

    provider = settings.portkey_extractor_provider or settings.portkey_critic_provider or "@openai"
    try:
        # Route through Instructor (schema-enforced) like every other stage, so the
        # audit verdict is coerced to response_model and re-prompted on mismatch
        # instead of silently failing to parse (which caused every row to fall back
        # to a "missing_row_audit" finding).
        return call_portkey_structured(
            api_key=settings.portkey_api_key,
            provider=provider,
            model=settings.extractor_model,
            response_model=response_model,
            messages=messages,
            max_tokens=32000,
            max_concurrency=settings.llm_max_concurrency,
            fallbacks=settings.extractor_fallbacks,
        )
    except Exception as exc:
        LOGGER.exception(
            "CSV audit Portkey call failed. provider=%s model=%s error_type=%s error=%r",
            provider,
            settings.extractor_model,
            type(exc).__name__,
            exc,
        )
        return response_model.model_validate(
            {
                "tag": "INVALID",
                "issues": [
                    {
                        "column_name": "",
                        "issue_type": "audit_model_error",
                        "issue_message": f"Audit model call failed: {type(exc).__name__}: {exc}",
                        "suggested_fix": "",
                    }
                ],
                "confidence_notes": "Audit fell back due to model-call failure.",
            }
        )


def _normalize_audit_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, list) and payload:
        payload = payload[0]

    if not isinstance(payload, dict):
        raise ValueError(f"Unexpected audit payload shape: {type(payload).__name__}")

    normalized = dict(payload)
    if "tag" not in normalized:
        for alternate_key in ("row_status", "audit_result", "verdict", "result", "status"):
            if alternate_key in normalized and normalized.get(alternate_key):
                normalized["tag"] = str(normalized[alternate_key]).upper()
                break
    if "issues" in normalized and isinstance(normalized["issues"], list):
        normalized["issues"] = [_normalize_issue_entry(issue) for issue in normalized["issues"]]
    elif "issues" not in normalized:
        raw_issues = (
            normalized.get("issue_list")
            or normalized.get("problems")
            or normalized.get("findings")
            or normalized.get("errors")
            or []
        )
        if isinstance(raw_issues, list):
            normalized["issues"] = [
                _normalize_issue_entry(issue)
                for issue in raw_issues
            ]
        else:
            normalized["issues"] = []
    if normalized.get("tag") not in {"VALID", "INVALID"}:
        if normalized["issues"]:
            normalized["tag"] = "INVALID"
        else:
            normalized["tag"] = "VALID"
    normalized.setdefault("confidence_notes", normalized.get("summary", ""))
    return normalized


def _normalize_batch_audit_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, list) and payload:
        payload = payload[0]

    if not isinstance(payload, dict):
        raise ValueError(f"Unexpected batch audit payload shape: {type(payload).__name__}")

    normalized = dict(payload)
    raw_findings = normalized.get("findings") or normalized.get("rows") or normalized.get("results") or []
    if not isinstance(raw_findings, list):
        raw_findings = []
    normalized["findings"] = [_normalize_batch_finding_entry(finding) for finding in raw_findings]
    normalized.setdefault("confidence_notes", normalized.get("summary", ""))
    return normalized


def _normalize_issue_entry(issue: Any) -> dict[str, str]:
    if isinstance(issue, dict):
        issue_message = (
            issue.get("issue_message")
            or issue.get("message")
            or issue.get("reason")
            or issue.get("finding")
            or json.dumps(issue)
        )
        return {
            "column_name": issue.get("column_name") or issue.get("column") or issue.get("field") or "",
            "issue_type": issue.get("issue_type") or issue.get("type") or "audit_issue",
            "issue_message": issue_message,
            "suggested_fix": issue.get("suggested_fix") or issue.get("fix") or issue.get("suggestion") or "",
        }
    return {
        "column_name": "",
        "issue_type": "audit_issue",
        "issue_message": str(issue),
        "suggested_fix": "",
    }


def _normalize_batch_finding_entry(finding: Any) -> dict[str, Any]:
    if not isinstance(finding, dict):
        raise ValueError(f"Unexpected batch finding payload shape: {type(finding).__name__}")

    normalized = dict(finding)
    row_number = normalized.get("row_number") or normalized.get("row") or normalized.get("line")
    if row_number is None:
        raise ValueError(f"Batch audit finding is missing row_number: {finding}")

    tag = normalized.get("tag")
    if not tag:
        for alternate_key in ("row_status", "audit_result", "verdict", "result", "status"):
            if normalized.get(alternate_key):
                tag = normalized[alternate_key]
                break

    raw_issues = (
        normalized.get("issues")
        or normalized.get("issue_list")
        or normalized.get("problems")
        or normalized.get("errors")
        or []
    )
    if not isinstance(raw_issues, list):
        raw_issues = [raw_issues]

    issues = [_normalize_issue_entry(issue) for issue in raw_issues if issue not in (None, "")]
    normalized_tag = str(tag).upper() if tag else ("INVALID" if issues else "VALID")
    if normalized_tag not in {"VALID", "INVALID"}:
        normalized_tag = "INVALID" if issues else "VALID"

    return {
        "row_number": int(row_number),
        "tag": normalized_tag,
        "issues": issues,
        "confidence_notes": normalized.get("confidence_notes") or normalized.get("summary") or "",
    }


def _chunk_rows(
    grouped_rows: list[tuple[int, dict[str, str]]],
    size: int,
) -> list[list[tuple[int, dict[str, str]]]]:
    return [grouped_rows[index:index + size] for index in range(0, len(grouped_rows), size)]


def _load_source_document(source_reference: str, runtime_paths: Any, cache: dict[str, Any]) -> Any:
    if source_reference in cache:
        cached = cache[source_reference]
        if isinstance(cached, Exception):
            raise cached
        return cached

    from chat_batches import _materialize_manifest_source

    materialized = _materialize_manifest_source(
        reference=source_reference,
        destination_dir=runtime_paths.input_dir,
        default_stub="audit_source",
    )
    try:
        parsed_document = parse_input(materialized)
    except Exception as exc:
        cache[source_reference] = exc
        raise
    cache[source_reference] = parsed_document
    return parsed_document


def _render_report_rows(
    row_number: int,
    source_reference: str,
    issues: list[CsvAuditIssue],
    final_verdict: str,
) -> list[dict[str, Any]]:
    return [
        {
            "row_number": row_number,
            "source_reference": source_reference,
            "issue_type": issue.issue_type,
            "column_name": issue.column_name,
            "issue_message": issue.issue_message,
            "suggested_fix": issue.suggested_fix,
            "final_verdict": final_verdict,
            "logged_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        for issue in issues
    ]
