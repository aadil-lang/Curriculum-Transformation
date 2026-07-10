from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from config import ROOT_DIR, RuntimePaths, Settings, get_runtime_paths
from parsers.router import parse_input
from schemas import load_schema_config


LOGGER = logging.getLogger(__name__)


_AUDIT_SYSTEM_PROMPT = (
    "You are a CSV audit agent. Your job is to confirm that each extracted row is a faithful, "
    "contract-consistent transformation of its source — and to flag only rows with a concrete, "
    "demonstrable defect you can point to. Transformation, mapping, inheritance, and source-faithful "
    "values are expected and correct, not defects. When in doubt, return VALID. Return your response "
    "as a single JSON object."
)

_AUDIT_RULES = """
Default to VALID. Return INVALID only when you can name a CONCRETE, demonstrable problem (quote the
offending value and say which rule it breaks). Do NOT reject on speculation or vague doubt, and do
NOT reject a value merely because the sample contract shows no example for that case — if the value
is faithful to the source and internally consistent, it is acceptable.

Field rules:
- Audit each row against the source content, not against extraction/metadata requirements.
- `grade_level` must be exactly one of: Elementary School, Middle School, High School.
- `display_grade` and `grade_number` carry the source's own band/stage label and MAY be a
  source-faithful label such as `Stage 5`, `Year 11`, or `Life Skills for Stage 4/5`. Do NOT flag a
  display_grade/grade_number value just because it is a Life Skills or other band label not shown in
  the sample — accept it when it reflects the source section the row came from.
- `Standard code` and `czi_standard_code` may be empty; never flag them solely for being blank.
- `Display standard code` may be source-faithful or a synthesized/prefixed code (e.g. `DA.1.1`) when
  needed for uniqueness; flag it only if it is duplicated within the CSV or clearly wrong.
- `topic` may join multiple source-supported topics with ` | `.
- Do not require `_source_citation` columns in this audit mode.

Reject ONLY for concrete defects: a value contradicted by the source, wrong field placement /
row contamination, document noise (headers/footers/nav/page numbers), a truncated or garbled
description, a duplicated `Display standard code`, or a grade_level that is not one of the three
allowed buckets.
""".strip()


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


class ReconciledFinding(BaseModel):
    index: int = Field(description="Zero-based index of the finding in the provided list.")
    suppress: bool = Field(default=False, description="True if the user's instructions make this finding acceptable/not-an-issue.")
    reason: str = Field(default="", description="Short reason it is suppressed, quoting the relevant user instruction.")


class ReconciliationVerdict(BaseModel):
    decisions: list[ReconciledFinding] = Field(default_factory=list)


def audit_extracted_csv(
    audit_csv_path: Path,
    settings: Settings,
    sample_csv_path: Path | None = None,
    runtime_paths: RuntimePaths | None = None,
    source_document_path: Path | None = None,
    user_instructions: str | None = None,
) -> CsvAuditResult:
    """Audit a CSV row-by-row against its source document(s).

    By default each row is grouped by its own ``source`` column value and audited
    against the document that value points to. When ``source_document_path`` is given
    (an externally-produced CSV reviewed against ONE supplied doc), the source column
    is ignored and EVERY row is audited against that single parsed document.

    ``user_instructions`` is optional free-text guidance the reviewer must honor for
    THIS audit (e.g. "this CSV intentionally omits grade columns — do not flag that",
    or "focus on description accuracy"). It shapes what the model flags.
    """
    audit_csv_path = audit_csv_path.expanduser().resolve()
    active_runtime_paths = runtime_paths or get_runtime_paths(ROOT_DIR)
    report_dir = active_runtime_paths.output_dir / "csv_audits"
    report_dir.mkdir(parents=True, exist_ok=True)

    active_settings = settings
    # Contract source, in priority order:
    #  1. an explicit sample CSV, if provided;
    #  2. otherwise, for an external single-doc audit, the audited CSV's OWN header
    #     (so its actual columns define the contract — no "missing column" noise from
    #     a mismatch against the default workspace schema).
    contract_csv = sample_csv_path or (audit_csv_path if source_document_path is not None else None)
    if contract_csv is not None:
        from batch_runner import create_model_settings, create_schema_from_sample_csv

        schema_config = create_schema_from_sample_csv(Path(contract_csv).expanduser().resolve(), "csv_audit")
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

    single_source_override = source_document_path.expanduser().resolve() if source_document_path else None

    with audit_csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row_number, raw_row in enumerate(reader, start=2):
            rows_audited += 1
            if single_source_override is not None:
                # External-CSV mode: audit every row against the one supplied doc,
                # regardless of any source column.
                source_reference = str(single_source_override)
            else:
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
            batch_verdict = _audit_rows_with_model(row_chunk, parsed_document, schema_config, active_settings, user_instructions)
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

    # Reconciliation guarantee: let user instructions override ANY finding (including
    # programmatic ones the audit model never sees). Suppressed findings stay in the
    # report (dimmed in the UI) but do not count as issues.
    if user_instructions and user_instructions.strip():
        _reconcile_findings_with_instructions(report_rows, user_instructions, active_settings)
    issue_count = sum(1 for row in report_rows if not row.get("suppressed"))

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
    user_instructions: str | None = None,
) -> CsvBatchAuditVerdict:
    messages = _build_batch_audit_messages(row_chunk, parsed_document, schema_config, user_instructions)

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

{_AUDIT_RULES}

Sample CSV transformation contract:
{contract_json}

CSV row to audit:
{row_json}

Original source markdown:
{parsed_document.markdown}
""".strip()

    return [
        {"role": "system", "content": _AUDIT_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]


def _build_batch_audit_messages(
    row_chunk: list[tuple[int, dict[str, str]]],
    parsed_document: Any,
    schema_config: Any,
    user_instructions: str | None = None,
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

    instructions_block = (
        f"\nUSER REVIEW INSTRUCTIONS (highest priority — follow these for this audit; if they say a "
        f"particular pattern is acceptable or out of scope, do NOT flag it):\n{user_instructions.strip()}\n"
        if user_instructions and user_instructions.strip()
        else ""
    )

    prompt = f"""
Audit these extracted CSV rows against the original source document and the approved sample contract.
Return exactly one finding for every input row_number.
{instructions_block}
{_AUDIT_RULES}

Sample CSV transformation contract:
{contract_json}

CSV rows to audit:
{rows_json}

Original source markdown:
{parsed_document.markdown}
""".strip()

    return [
        {"role": "system", "content": _AUDIT_SYSTEM_PROMPT},
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

    from batch_runner import _materialize_manifest_source

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


def _reconcile_findings_with_instructions(
    report_rows: list[dict[str, Any]],
    user_instructions: str,
    settings: Settings,
) -> None:
    """Mark findings the user's instructions declare acceptable as suppressed.

    A single cheap LLM call (findings + instructions only — no source doc) judges each
    finding against the user's free-text guidance. This is the guarantee layer that lets
    instructions override ANY finding, including deterministic/programmatic ones (e.g.
    duplicate display codes, missing columns) the audit model never sees. Mutates
    report_rows in place, adding ``suppressed`` and ``suppressed_reason``. Best-effort:
    on any failure, nothing is suppressed (findings stand).
    """
    if not report_rows or not user_instructions.strip():
        return

    listing = json.dumps(
        [
            {
                "index": i,
                "row_number": r.get("row_number"),
                "issue_type": r.get("issue_type"),
                "column_name": r.get("column_name"),
                "issue_message": r.get("issue_message"),
            }
            for i, r in enumerate(report_rows)
        ],
        indent=2,
    )
    system_prompt = (
        "You reconcile automated CSV-review findings against a user's explicit review "
        "instructions. The user knows their data's intended conventions; their instructions "
        "OVERRIDE the findings. For each finding, decide whether the user's instructions make "
        "it acceptable (not a real issue for THIS CSV). Suppress a finding ONLY when an "
        "instruction clearly covers it (e.g. 'duplicate display codes are allowed', "
        "'topics are intentionally merged', 'do not flag missing grade columns'). When in "
        "doubt, do NOT suppress. Return one decision per finding index. Respond as JSON."
    )
    user_prompt = (
        f"User review instructions:\n{user_instructions.strip()}\n\n"
        f"Findings (suppress the ones the instructions make acceptable):\n{listing}"
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    try:
        if settings.portkey_api_key:
            verdict = _call_portkey_json(ReconciliationVerdict, messages, settings)
        elif settings.critic_provider == "openai":
            verdict = _call_openai_json(ReconciliationVerdict, messages, settings)
        elif settings.critic_provider == "anthropic":
            verdict = _call_anthropic_json(ReconciliationVerdict, messages, settings)
        else:
            return
    except Exception as exc:  # noqa: BLE001 - suppression is best-effort; findings stand on failure
        LOGGER.warning("Finding reconciliation failed (%s); no findings suppressed.", exc)
        return

    for decision in getattr(verdict, "decisions", []) or []:
        if 0 <= decision.index < len(report_rows) and decision.suppress:
            report_rows[decision.index]["suppressed"] = True
            report_rows[decision.index]["suppressed_reason"] = decision.reason or "Suppressed by your review instruction."


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
