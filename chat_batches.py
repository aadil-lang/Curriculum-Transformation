from __future__ import annotations

import csv
import html.parser
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field, create_model

from config import DEFAULT_SCHEMA_CONFIG_PATH, ROOT_DIR, RuntimePaths, Settings, get_runtime_paths
from pipeline import DataTransformationPipeline, ProcessResult
from schemas import (
    SampleTransformationContract,
    SchemaFieldSpec,
    TargetSchemaConfig,
    load_schema_config,
)


CHAT_BATCH_OUTPUT_DIR = ROOT_DIR / "output" / "chat_batches"
URL_PATTERN = re.compile(r"^https?://", re.IGNORECASE)
DOWNLOADABLE_REMOTE_SUFFIXES = {".pdf", ".docx", ".csv"}
DIRECT_DOCUMENT_SUFFIXES = {".pdf", ".docx"}
MANIFEST_SOURCE_COLUMN_NAMES = ("source", "source_url", "document_url", "pdf_url", "url")
MIN_SAMPLE_ROWS = 6
MAX_SAMPLE_ROWS = 10
OPTIONAL_SAMPLE_COLUMNS = {"l3", "l4", "l5", "standard code", "czi_standard_code"}


class ChatBatchSpec(BaseModel):
    name: str = Field(..., pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
    input_files: list[str]
    sample_csv: str | None = None
    instructions: str | None = None
    infer_schema: bool = False
    draft_only: bool = False
    output_csv_name: str | None = None


class ChatBatchRequest(BaseModel):
    batches: list[ChatBatchSpec]


@dataclass(slots=True)
class ChatBatchExecutionResult:
    batch_name: str
    schema_path: str
    output_csv_path: str
    manual_review_path: str
    results: list[dict[str, str]]
    mode: str


def run_chat_batches(request: ChatBatchRequest, settings: Settings) -> list[ChatBatchExecutionResult]:
    return run_chat_batches_with_factory(request, settings)


def run_chat_batches_with_factory(
    request: ChatBatchRequest,
    settings: Settings,
    pipeline_factory: Callable[[Settings, RuntimePaths], DataTransformationPipeline] | None = None,
) -> list[ChatBatchExecutionResult]:
    if len(request.batches) <= 1 or settings.batch_max_workers <= 1:
        return [
            _run_single_batch(batch, settings, pipeline_factory=pipeline_factory)
            for batch in request.batches
        ]

    indexed_results: list[ChatBatchExecutionResult | None] = [None] * len(request.batches)
    max_workers = min(settings.batch_max_workers, len(request.batches))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_run_single_batch, batch, settings, pipeline_factory): index
            for index, batch in enumerate(request.batches)
        }
        for future in as_completed(futures):
            index = futures[future]
            indexed_results[index] = future.result()

    return [result for result in indexed_results if result is not None]


def load_chat_batch_request(path: str) -> ChatBatchRequest:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return ChatBatchRequest.model_validate(payload)


def build_chat_batch_request(
    *,
    name: str,
    files: list[str],
    sample_csv: str | None,
    instructions: str | None,
    infer_schema: bool,
    draft_only: bool,
    output_csv_name: str | None,
) -> ChatBatchRequest:
    return ChatBatchRequest(
        batches=[
            ChatBatchSpec(
                name=name,
                input_files=files,
                sample_csv=sample_csv,
                instructions=instructions,
                infer_schema=infer_schema,
                draft_only=draft_only,
                output_csv_name=output_csv_name,
            )
        ]
    )


def _run_single_batch(
    batch: ChatBatchSpec,
    settings: Settings,
    pipeline_factory: Callable[[Settings, RuntimePaths], DataTransformationPipeline] | None = None,
) -> ChatBatchExecutionResult:
    batch_output_dir = CHAT_BATCH_OUTPUT_DIR / batch.name
    runtime_paths = get_runtime_paths(batch_output_dir)
    runtime_paths.final_csv_path = runtime_paths.output_dir / _default_batch_csv_name(batch)
    schema_path = runtime_paths.output_dir / "schema_config.json"

    resolved_input_files = _materialize_input_references(batch.input_files, runtime_paths)
    resolved_sample_csv = _materialize_optional_sample_csv(batch.sample_csv, runtime_paths)
    sample_manifest_inputs = (
        _expand_manifest_csv_inputs(
            manifest_path=resolved_sample_csv,
            destination_dir=runtime_paths.input_dir,
            default_stub_prefix=f"{batch.name}_sample_source",
        )
        if resolved_sample_csv
        else []
    )
    resolved_input_files = _merge_unique_paths(resolved_input_files, sample_manifest_inputs)

    if resolved_sample_csv:
        schema_config = create_schema_from_sample_csv(resolved_sample_csv, batch.name)
        mode = "sample_csv"
    elif batch.instructions:
        schema_config = create_schema_from_instructions(
            instructions=batch.instructions,
            input_files=resolved_input_files,
            settings=settings,
            batch_name=batch.name,
        )
        mode = "instruction_schema"
    elif batch.infer_schema:
        schema_config = infer_schema_from_documents(resolved_input_files, settings, batch.name)
        mode = "inferred_schema"
    else:
        schema_config = load_schema_config(str(DEFAULT_SCHEMA_CONFIG_PATH), settings=settings)
        mode = "workspace_default_schema"

    schema_path.write_text(json.dumps(schema_config.model_dump(mode="json"), indent=2), encoding="utf-8")
    _write_sample_csv_template(runtime_paths.output_dir / "sample_output_template.csv", schema_config)

    if mode in {"instruction_schema", "inferred_schema"}:
        return ChatBatchExecutionResult(
            batch_name=batch.name,
            schema_path=str(schema_path),
            output_csv_path=str(runtime_paths.final_csv_path),
            manual_review_path=str(runtime_paths.manual_review_path),
            results=[],
            mode=f"{mode}_draft_pending_approval",
        )

    if batch.draft_only:
        return ChatBatchExecutionResult(
            batch_name=batch.name,
            schema_path=str(schema_path),
            output_csv_path=str(runtime_paths.final_csv_path),
            manual_review_path=str(runtime_paths.manual_review_path),
            results=[],
            mode=f"{mode}_draft_only",
        )

    batch_settings = create_model_settings(settings, schema_path)
    pipeline = (
        pipeline_factory(batch_settings, runtime_paths)
        if pipeline_factory
        else DataTransformationPipeline(settings=batch_settings, runtime_paths=runtime_paths)
    )
    process_results = pipeline.process_paths(resolved_input_files)
    output_csv_path = str(runtime_paths.final_csv_path)

    return ChatBatchExecutionResult(
        batch_name=batch.name,
        schema_path=str(schema_path),
        output_csv_path=output_csv_path,
        manual_review_path=str(runtime_paths.manual_review_path),
        results=[
            {
                "path": str(result.path),
                "status": result.status,
                "message": result.message,
                "stage": result.stage,
            }
            for result in process_results
        ],
        mode=mode,
    )


def create_schema_from_sample_csv(sample_csv_path: Path, batch_name: str) -> TargetSchemaConfig:
    with sample_csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        rows = list(reader)

    fields: list[SchemaFieldSpec] = []
    used_names: set[str] = set()
    for header in headers:
        internal_name = _slugify_header(header, used_names)
        inferred_type = _infer_field_type([row.get(header, "") for row in rows])
        fields.append(
            SchemaFieldSpec(
                name=internal_name,
                description=f"Value extracted for sample CSV column '{header}'.",
                field_type=inferred_type,
                required=False,
                output_column=header,
                example_value=_first_non_empty([row.get(header, "") for row in rows]),
            )
        )

    return TargetSchemaConfig(
        schema_name=f"{batch_name}_sample_csv_schema",
        description=f"Schema derived from sample CSV '{sample_csv_path.name}'.",
        fields=fields,
        sample_contract=_derive_sample_contract(headers, rows),
    )


def infer_schema_from_documents(
    input_files: list[Path],
    settings: Settings,
    batch_name: str,
) -> TargetSchemaConfig:
    if settings.gemini_api_key:
        inferred = _infer_schema_with_gemini(input_files, settings, batch_name)
        if inferred is not None:
            return inferred
    return _heuristic_schema_from_documents(batch_name)


def create_schema_from_instructions(
    *,
    instructions: str,
    input_files: list[Path],
    settings: Settings,
    batch_name: str,
) -> TargetSchemaConfig:
    if settings.gemini_api_key:
        inferred = _infer_schema_from_instructions_with_gemini(
            instructions=instructions,
            input_files=input_files,
            settings=settings,
            batch_name=batch_name,
        )
        if inferred is not None:
            return inferred
    return _heuristic_schema_from_instructions(batch_name, instructions)


def create_model_settings(settings: Settings, schema_path: Path) -> Settings:
    return Settings(
        extractor_model=settings.extractor_model,
        extractor_provider=settings.extractor_provider,
        critic_provider=settings.critic_provider,
        critic_model=settings.critic_model,
        portkey_extractor_provider=settings.portkey_extractor_provider,
        portkey_critic_provider=settings.portkey_critic_provider,
        watch_interval_seconds=settings.watch_interval_seconds,
        max_retries=settings.max_retries,
        extraction_max_workers=settings.extraction_max_workers,
        batch_max_workers=settings.batch_max_workers,
        schema_config_path=schema_path,
        portkey_api_key=settings.portkey_api_key,
        gemini_api_key=settings.gemini_api_key,
        openai_api_key=settings.openai_api_key,
        anthropic_api_key=settings.anthropic_api_key,
        google_sheets_sync_enabled=settings.google_sheets_sync_enabled,
        google_sheets_spreadsheet_id=settings.google_sheets_spreadsheet_id,
        google_sheets_sheet_name=settings.google_sheets_sheet_name,
        google_service_account_json=settings.google_service_account_json,
        google_service_account_json_path=settings.google_service_account_json_path,
        google_oauth_client_secret_path=settings.google_oauth_client_secret_path,
        google_oauth_token_path=settings.google_oauth_token_path,
    )


def _default_batch_csv_name(batch: ChatBatchSpec) -> str:
    requested_name = (batch.output_csv_name or "").strip()
    if requested_name:
        return requested_name
    return f"{batch.name}.csv"


def _materialize_optional_sample_csv(sample_csv: str | None, runtime_paths: RuntimePaths) -> Path | None:
    if not sample_csv:
        return None
    return _materialize_reference(
        reference=sample_csv,
        destination_dir=runtime_paths.output_dir / "sample_schema",
        default_stub="sample_schema",
    )


def _materialize_input_references(references: list[str], runtime_paths: RuntimePaths) -> list[Path]:
    materialized: list[Path] = []
    for index, reference in enumerate(references):
        path = _materialize_reference(
            reference=reference,
            destination_dir=runtime_paths.input_dir,
            default_stub=f"chat_input_{index + 1}",
        )
        if path.suffix.lower() == ".csv":
            manifest_inputs = _expand_manifest_csv_inputs(
                manifest_path=path,
                destination_dir=runtime_paths.input_dir,
                default_stub_prefix=f"{path.stem}_row",
            )
            if not manifest_inputs:
                raise ValueError(
                    f"CSV input '{path.name}' did not contain a usable source column or any source values."
                )
            materialized.extend(manifest_inputs)
            continue
        materialized.append(path)

    return _merge_unique_paths(materialized, [])


def _materialize_reference(reference: str, destination_dir: Path, default_stub: str) -> Path:
    destination_dir.mkdir(parents=True, exist_ok=True)
    if _looks_like_url(reference):
        return _materialize_remote_reference(reference, destination_dir, default_stub)

    source_path = Path(reference).expanduser().resolve()
    if not source_path.exists():
        raise FileNotFoundError(f"Input reference not found: {reference}")
    return source_path


def _materialize_remote_reference(reference: str, destination_dir: Path, default_stub: str) -> Path:
    parsed = urllib.parse.urlparse(reference)
    candidate_name = Path(parsed.path).name
    suffix = Path(candidate_name).suffix.lower()

    if suffix in DOWNLOADABLE_REMOTE_SUFFIXES:
        return _download_remote_file(reference, destination_dir, candidate_name or default_stub)

    return _write_website_reference(reference, destination_dir, candidate_name or default_stub)


def _download_remote_file(reference: str, destination_dir: Path, candidate_name: str) -> Path:
    safe_name = _safe_filename(candidate_name)
    destination_path = _dedupe_path(destination_dir / safe_name)

    try:
        with urllib.request.urlopen(reference) as response:
            payload = response.read()
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not download remote input: {reference}. {exc}") from exc

    destination_path.write_bytes(payload)
    return destination_path


def _expand_manifest_csv_inputs(
    manifest_path: Path | None,
    destination_dir: Path,
    default_stub_prefix: str,
) -> list[Path]:
    if manifest_path is None or manifest_path.suffix.lower() != ".csv":
        return []

    with manifest_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        source_column = _find_manifest_source_column(headers)
        if not source_column:
            return []

        expanded_paths: list[Path] = []
        for row_index, row in enumerate(reader, start=1):
            source_reference = (row.get(source_column) or "").strip()
            if not source_reference:
                continue
            expanded_paths.append(
                _materialize_manifest_source(
                    reference=source_reference,
                    destination_dir=destination_dir,
                    default_stub=f"{default_stub_prefix}_{row_index}",
                )
            )

    return _merge_unique_paths(expanded_paths, [])


def _materialize_manifest_source(reference: str, destination_dir: Path, default_stub: str) -> Path:
    destination_dir.mkdir(parents=True, exist_ok=True)
    if _looks_like_url(reference):
        parsed = urllib.parse.urlparse(reference)
        candidate_name = Path(parsed.path).name or default_stub
        suffix = Path(candidate_name).suffix.lower()
        if suffix in DIRECT_DOCUMENT_SUFFIXES:
            return _download_remote_file(reference, destination_dir, candidate_name)
        return _materialize_webpage_source(reference, destination_dir, candidate_name or default_stub)

    source_path = Path(reference).expanduser().resolve()
    if not source_path.exists():
        raise FileNotFoundError(f"Manifest source not found: {reference}")
    return source_path


def _materialize_webpage_source(reference: str, destination_dir: Path, candidate_name: str) -> Path:
    discovered_pdf_url = _discover_pdf_url_from_webpage(reference)
    if discovered_pdf_url:
        discovered_name = Path(urllib.parse.urlparse(discovered_pdf_url).path).name or f"{candidate_name}.pdf"
        return _download_remote_file(discovered_pdf_url, destination_dir, discovered_name)
    return _write_website_reference(reference, destination_dir, candidate_name or "website_reference")


def _find_manifest_source_column(headers: list[str]) -> str | None:
    normalized_lookup = {
        re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_"): header for header in headers
    }
    for candidate in MANIFEST_SOURCE_COLUMN_NAMES:
        header = normalized_lookup.get(candidate)
        if header:
            return header
    return None


def _discover_pdf_url_from_webpage(reference: str) -> str | None:
    try:
        with urllib.request.urlopen(reference) as response:
            html_text = response.read().decode("utf-8", errors="ignore")
            base_url = response.geturl() or reference
    except urllib.error.URLError:
        return None

    parser = _PdfLinkParser(base_url)
    parser.feed(html_text)
    if not parser.pdf_links:
        return None

    ranked_links = sorted(parser.pdf_links, key=_pdf_link_rank, reverse=True)
    return ranked_links[0][0]


def _pdf_link_rank(item: tuple[str, str]) -> tuple[int, int]:
    url, label = item
    lowered_url = url.lower()
    lowered_label = label.lower()
    score = 0
    for keyword in ("syllabus", "curriculum", "download", "pdf", "stage", "year"):
        if keyword in lowered_url:
            score += 2
        if keyword in lowered_label:
            score += 1
    return score, -len(lowered_url)


def _write_website_reference(reference: str, destination_dir: Path, candidate_name: str) -> Path:
    stem = Path(candidate_name).stem or "website_reference"
    reference_path = _dedupe_path(destination_dir / f"{_safe_stem(stem)}.url")
    reference_path.write_text(f"[InternetShortcut]\nURL={reference}\n", encoding="utf-8")
    return reference_path


def _looks_like_url(value: str) -> bool:
    return bool(URL_PATTERN.match(value.strip()))


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return cleaned or "downloaded_input"


def _safe_stem(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return cleaned or "input"


def _dedupe_path(path: Path) -> Path:
    if not path.exists():
        return path

    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate

    raise RuntimeError(f"Could not allocate a unique file path for {path}")


def _merge_unique_paths(primary: list[Path], secondary: list[Path]) -> list[Path]:
    merged: list[Path] = []
    seen: set[str] = set()
    for path in [*primary, *secondary]:
        resolved = str(path.resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        merged.append(path)
    return merged


def _write_sample_csv_template(path: Path, schema_config: TargetSchemaConfig) -> None:
    headers = ["source_document", "source_type", "source_identifier", "processed_at_utc"]
    for spec in schema_config.fields:
        output_column = spec.output_column or spec.name
        headers.append(output_column)
        headers.append(f"{output_column}_source_citation")

    sample_rows: list[list[str]] = []
    for index in range(1, MIN_SAMPLE_ROWS + 1):
        row = [
            f"example_document_{index}.pdf",
            "pdf",
            f"example_document_{index}",
            datetime.now(timezone.utc).isoformat(),
        ]
        for spec in schema_config.fields:
            example_value = _expand_example_value(spec.example_value, index)
            row.append(example_value)
            row.append(example_value)
        sample_rows.append(row)

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(sample_rows)


def _expand_example_value(example_value: str | None, index: int) -> str:
    value = (example_value or "").strip()
    if not value:
        return ""
    if re.fullmatch(r"-?\d+", value):
        return str(int(value) + index - 1)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return f"{float(value) + index - 1:g}"
    return f"{value} {index}"


def _slugify_header(header: str, used_names: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_") or "column"
    if not base[0].isalpha():
        base = f"field_{base}"

    candidate = base
    counter = 2
    while candidate in used_names:
        candidate = f"{base}_{counter}"
        counter += 1
    used_names.add(candidate)
    return candidate


def _infer_field_type(values: list[str | None]) -> str:
    normalized = [value.strip() for value in values if value and value.strip()]
    if not normalized:
        return "string"
    if all(_looks_like_bool(value) for value in normalized):
        return "boolean"
    if all(_looks_like_int(value) for value in normalized):
        return "integer"
    if all(_looks_like_float(value) for value in normalized):
        return "number"
    return "string"


def _first_non_empty(values: list[str | None]) -> str | None:
    for value in values:
        if value and value.strip():
            return value.strip()
    return None


def _looks_like_bool(value: str) -> bool:
    return value.lower() in {"true", "false", "yes", "no", "0", "1"}


def _looks_like_int(value: str) -> bool:
    try:
        int(value.replace(",", ""))
    except ValueError:
        return False
    return True


def _looks_like_float(value: str) -> bool:
    try:
        float(value.replace(",", ""))
    except ValueError:
        return False
    return True


def _derive_sample_contract(headers: list[str], rows: list[dict[str, str]]) -> SampleTransformationContract:
    normalized_headers = {header.strip().lower(): header for header in headers}
    non_empty_rows = [row for row in rows if any((value or "").strip() for value in row.values())]
    exemplar_rows = non_empty_rows[:MAX_SAMPLE_ROWS]

    subject_examples = _collect_examples(exemplar_rows, normalized_headers.get("subject"))
    grade_level_examples = _collect_examples(exemplar_rows, normalized_headers.get("grade_level"))
    display_grade_examples = _collect_examples(exemplar_rows, normalized_headers.get("display_grade"))
    grade_number_examples = _collect_examples(
        exemplar_rows,
        normalized_headers.get("grade_number") or normalized_headers.get("grade_string"),
    )
    display_standard_examples = _collect_examples(
        exemplar_rows,
        normalized_headers.get("display standard code"),
    )
    source_examples = _collect_examples(exemplar_rows, normalized_headers.get("source"))
    description_examples = _collect_examples(exemplar_rows, normalized_headers.get("description"))

    sample_rows = [{header: (row.get(header, "") or "").strip() for header in headers} for row in exemplar_rows]
    required_columns = _infer_required_columns(headers, non_empty_rows)

    contract = SampleTransformationContract(
        column_order=headers,
        required_columns=required_columns,
        subject_naming=_infer_subject_naming(subject_examples),
        grade_level_naming=_infer_grade_level_naming(grade_level_examples),
        display_grade_logic=_infer_display_grade_logic(display_grade_examples),
        grade_string_logic=_infer_grade_string_logic(grade_number_examples, normalized_headers),
        display_standard_code_logic=_infer_display_standard_code_logic(display_standard_examples),
        source_link_format=_infer_source_link_format(source_examples),
        description_style=_infer_description_style(description_examples),
        description_integrity_rules=_infer_description_integrity_rules(description_examples),
        description_multiline_style=_infer_description_multiline_style(description_examples),
        description_merge_split_style=_infer_description_merge_split_style(description_examples),
        bullet_and_punctuation_style=_infer_bullet_and_punctuation_style(description_examples),
        symbol_preservation_rules=_infer_symbol_preservation_rules(description_examples),
        field_placement_rules=_infer_field_placement_rules(normalized_headers, exemplar_rows),
        disallowed_output_content=_infer_disallowed_output_content(headers, exemplar_rows),
        noise_rejection_rules=_infer_noise_rejection_rules(headers),
        sample_rows=sample_rows,
    )
    return contract


def _infer_required_columns(headers: list[str], rows: list[dict[str, str]]) -> list[str]:
    required_columns: list[str] = []
    for header in headers:
        normalized = header.strip().lower()
        if normalized in OPTIONAL_SAMPLE_COLUMNS:
            continue
        if any((row.get(header, "") or "").strip() for row in rows):
            required_columns.append(header)
    return required_columns


def _collect_examples(rows: list[dict[str, str]], header: str | None) -> list[str]:
    if not header:
        return []
    values: list[str] = []
    for row in rows:
        value = (row.get(header, "") or "").strip()
        if value:
            values.append(value)
    return values


def _infer_subject_naming(examples: list[str]) -> str:
    if not examples:
        return "Use the official subject naming shown in the approved sample CSV."
    if any("(" in value and ")" in value for value in examples):
        return (
            "Preserve the full official subject name exactly as shown in the sample, including "
            "parenthetical abbreviations when they appear."
        )
    return "Preserve the official subject label style shown in the sample CSV without inventing shorter aliases."


def _infer_grade_level_naming(examples: list[str]) -> str:
    if not examples:
        return "Use the same broad grade-band naming style shown in the sample CSV."
    joined = ", ".join(sorted(set(examples)))
    return f"Use the sample's broad grade-level labels, such as: {joined}."


def _infer_display_grade_logic(examples: list[str]) -> str:
    if not examples:
        return "Use the display-grade wording from the sample CSV."
    joined = ", ".join(sorted(set(examples)))
    return f"Render display_grade in the same user-facing style as the sample, such as: {joined}."


def _infer_grade_string_logic(examples: list[str], normalized_headers: dict[str, str]) -> str:
    if "grade_string" not in normalized_headers and (
        "grade number" in normalized_headers or "grade_number" in normalized_headers
    ):
        if examples:
            joined = ", ".join(sorted(set(examples)))
            return (
                "The sample does not define a separate grade_string column. Use the sample's grade representation "
                f"through the existing grade_number/display_grade fields, such as: {joined}. "
                "If grade_number is a numeric range like 9-12, expand it to a comma-separated list like 9,10,11,12, with no spaces after commas. "
                "Do not invent extra grade fields."
            )
        return (
            "The sample does not define a separate grade_string column. Use only the grade fields present in the schema "
            "and do not invent extra grade outputs. If grade_number is a numeric range like 9-12, expand it to a comma-separated list like 9,10,11,12, with no spaces after commas."
        )
    if not examples:
        return "Use the grade string style shown in the sample CSV. If grade_number is a numeric range like 9-12, expand it to a comma-separated list like 9,10,11,12, with no spaces after commas."
    joined = ", ".join(sorted(set(examples)))
    return (
        f"Use the sample's grade string style, such as: {joined}. "
        "If grade_number is a numeric range like 9-12, expand it to a comma-separated list like 9,10,11,12, with no spaces after commas."
    )


def _infer_display_standard_code_logic(examples: list[str]) -> str:
    if not examples:
        return (
            "Display standard code may either mirror the source notation or be synthetic/transformed, "
            "depending on the approved sample and run instructions. "
            "It must be unique per row and must never be duplicated within the same CSV. "
            "If the same raw code appears in multiple domains or sections, add a short domain code prefix to keep each Display standard code unique. "
            "If the approved sample supports prefixed or formed display codes, repeated rows with the same description and raw code may use a short domain code or topic code prefix, whichever makes the display code unique."
        )
    if len(set(examples)) == 1:
        return (
            "Display standard code may either mirror the source notation or be synthetic/transformed. "
            f"Match the sample's display style for this run (for example: {examples[0]}), rather than assuming it must equal the raw source text. "
            "Display standard code must stay unique within the CSV. "
            "If the same raw code repeats across multiple domains or sections, add a short domain code prefix to disambiguate it. "
            "If the approved sample supports prefixed or formed display codes, repeated rows with the same description and raw code may use a short domain code or topic code prefix, whichever makes the display code unique."
        )
    joined = ", ".join(sorted(set(examples)))
    return (
        "Render Display standard code in the exact display style shown by the sample, whether that style is "
        f"source-faithful or synthetic/transformed, such as: {joined}. "
        "Display standard code must be unique within the CSV. "
        "If the same raw code repeats across multiple domains or sections, add a short domain code prefix to disambiguate it. "
        "If the approved sample supports prefixed or formed display codes, repeated rows with the same description and raw code may use a short domain code or topic code prefix, whichever makes the display code unique."
    )


def _infer_source_link_format(examples: list[str]) -> str:
    if not examples:
        return "Use direct source links in the same format shown in the sample CSV."
    first = examples[0]
    parsed = urllib.parse.urlparse(first)
    if parsed.scheme and parsed.netloc:
        return (
            "Use a direct absolute source URL in the source column. Preserve the canonical document or page link format "
            f"shown in the sample, such as: {first}."
        )
    return "Use the source link format shown in the sample CSV without wrapping it in markdown or extra text."


def _infer_description_style(examples: list[str]) -> str:
    if not examples:
        return "Write description values in the same concise prose style shown in the sample CSV."
    first = examples[0]
    starts_lower = first[:1].islower()
    no_trailing_period = not first.rstrip().endswith(".")
    clauses = []
    clauses.append("Use outcome-style prose rather than notes or commentary")
    if starts_lower:
        clauses.append("keep the wording in sentence-fragment style starting with lowercase when the source does so")
    if no_trailing_period:
        clauses.append("omit a trailing period unless the source clearly includes one")
    return ". ".join(clauses) + "."


def _infer_bullet_and_punctuation_style(examples: list[str]) -> str:
    if not examples:
        return "Do not include raw bullets; preserve only meaningful punctuation from the source."
    has_bullets = any(value.lstrip().startswith(("-", "*", "\u2022")) for value in examples)
    if has_bullets:
        return "Remove bullet markers from output values while preserving the actual sentence content and punctuation."
    return "Do not prefix values with bullets or numbering. Preserve source punctuation only when it belongs to the content."


def _infer_description_integrity_rules(examples: list[str]) -> list[str]:
    rules = [
        "Do not truncate description text.",
        "Do not merge text from neighboring rows into the description.",
        "Do not drop required sub-parts that belong to the same description entry.",
        "Do not add stray headers, footers, notes, or layout labels into description.",
    ]
    if examples and any("\n" in value for value in examples):
        rules.append("Preserve meaningful line breaks from the sample style when they carry structure.")
    return rules


def _infer_description_multiline_style(examples: list[str]) -> str:
    if not examples:
        return "Preserve multiline structure only when the sample style and source structure require it."
    if any("\n" in value for value in examples):
        return (
            "The sample preserves multiline description structure. Keep intentional line breaks or split lines when they "
            "represent meaningful sub-parts."
        )
    return (
        "The sample uses a single-cell prose style for description. Do not force multiline formatting unless the approved "
        "sample for the run explicitly preserves it."
    )


def _infer_description_merge_split_style(examples: list[str]) -> str:
    if not examples:
        return "If the sample expects merged sub-parts, merge them in the sample style; if it expects split lines, preserve them."
    if any("\n" in value for value in examples):
        return (
            "The sample indicates split-line or multiline preservation. Keep distinct lines when they belong to the same row."
        )
    return (
        "The sample indicates merged prose descriptions. Merge same-row sub-parts into one description only when they belong "
        "to the same source entry and without crossing row boundaries."
    )


def _infer_field_placement_rules(
    normalized_headers: dict[str, str],
    rows: list[dict[str, str]],
) -> dict[str, str]:
    rules: dict[str, str] = {}
    present = {key for key in normalized_headers}
    if "domain" in present:
        rules["domain"] = "Use Domain for the named syllabus, strand, domain, or official grouping above the standard level."
    if "topic" in present:
        rules["topic"] = (
            "Use Topic only for a true topic/subtopic label. Leave it blank when the source does not expose one. "
            "If the approved sample supports merged topic cells and one standard genuinely spans multiple topics, merge those topic names into one Topic cell using ' | '."
        )
    if "l3" in present:
        rules["l3"] = "Use L3 only for a genuine third hierarchy level from the source. Do not copy description text into L3."
    if "l4" in present:
        rules["l4"] = "Use L4 only for a genuine fourth hierarchy level from the source. Otherwise leave it blank."
    if "l5" in present:
        rules["l5"] = "Use L5 only for a genuine fifth hierarchy level from the source. Otherwise leave it blank."
    if "description" in present:
        rules["description"] = (
            "Use description for the actual learning outcome, standard statement, or descriptive sentence. "
            "Do not place hierarchy labels, page headers, or bullet markers in description."
        )
    return rules


def _infer_symbol_preservation_rules(examples: list[str]) -> list[str]:
    rules = [
        "Preserve mathematical symbols, equations, radicals, superscripts, and subscripts when represented in the source.",
        "Preserve chemistry notation, Greek letters, domain-specific notation, and punctuation with semantic value.",
        "Preserve non-Latin scripts and mixed-language content without flattening them into incorrect plain text.",
        "If native extraction degrades special notation, attempt localized repair from surrounding context and the source evidence before returning the value.",
        "If notation cannot be repaired confidently, leave the value blank or send the row to manual review rather than inventing plain-text substitutes.",
    ]
    if examples and any(any(ord(char) > 127 for char in value) for value in examples):
        rules.append("The sample already contains non-ASCII content; preserve Unicode faithfully.")
    return rules


def _infer_disallowed_output_content(headers: list[str], rows: list[dict[str, str]]) -> list[str]:
    disallowed = [
        "Do not include commentary, analysis notes, or explanations in output cells.",
        "Do not copy raw HTML, navigation labels, page numbers, or file names into output cells.",
        "Do not place citation text inside the value columns; citations belong only in the paired _source_citation columns.",
        "Do not merge multiple standards or outcomes into one row unless the sample CSV clearly does so.",
    ]
    lowered_headers = {header.strip().lower() for header in headers}
    if "topic" in lowered_headers or "l3" in lowered_headers or "l4" in lowered_headers or "l5" in lowered_headers:
        disallowed.append("Do not force text into Topic/L3/L4/L5 when the source does not provide those hierarchy levels.")
        disallowed.append(
            "Do not duplicate a row only to repeat the same standard across multiple topics when the approved sample supports merging the topic names into one Topic cell using ' | '."
        )
    if any((row.get("description", "") or "").strip() for row in rows):
        disallowed.append("Do not prepend bullets, numbering, or labels like 'Description:' to description values.")
    return disallowed


def _infer_noise_rejection_rules(headers: list[str]) -> list[str]:
    rules = [
        "Exclude appendix-only items when they are outside the requested extraction scope.",
        "Exclude notes such as 'N.B.' unless the sample or instructions explicitly require them.",
        "Exclude repeated headers, repeated footers, page numbers, and continuation fragments from adjacent rows.",
        "Exclude source layout labels, technical extraction artifacts, and non-row structural text.",
    ]
    lowered_headers = {header.strip().lower() for header in headers}
    if "description" in lowered_headers:
        rules.append("Reject description values contaminated by neighboring row text or document chrome.")
    return rules


class _PdfLinkParser(html.parser.HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.pdf_links: list[tuple[str, str]] = []
        self._current_href: str | None = None
        self._current_text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attributes = {key.lower(): value or "" for key, value in attrs}
        href = attributes.get("href", "").strip()
        if not href:
            return
        absolute_href = urllib.parse.urljoin(self.base_url, href)
        if self._looks_like_pdf_link(absolute_href, attributes):
            self._current_href = absolute_href
            self._current_text_parts = []

    def handle_data(self, data: str) -> None:
        if self._current_href is not None:
            self._current_text_parts.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._current_href is None:
            return
        label = " ".join(part for part in self._current_text_parts if part).strip()
        self.pdf_links.append((self._current_href, label))
        self._current_href = None
        self._current_text_parts = []

    @staticmethod
    def _looks_like_pdf_link(url: str, attributes: dict[str, str]) -> bool:
        lowered_url = url.lower()
        if ".pdf" in lowered_url:
            return True
        type_attr = attributes.get("type", "").lower()
        if "pdf" in type_attr:
            return True
        return False


def _infer_schema_with_gemini(
    input_files: list[Path],
    settings: Settings,
    batch_name: str,
) -> TargetSchemaConfig | None:
    from parsers.router import parse_input

    documents = [parse_input(path) for path in input_files[:3]]
    snippets = []
    for document in documents:
        snippets.append(
            {
                "source_name": document.source_name,
                "source_type": document.source_type,
                "markdown_excerpt": document.markdown[:4000],
            }
        )

    import instructor
    from google import genai

    raw_client = genai.Client(api_key=settings.gemini_api_key)
    build_candidates = [
        lambda: instructor.from_genai(raw_client, mode=instructor.Mode.GEMINI_JSON),
        lambda: instructor.from_gemini(raw_client, mode=instructor.Mode.GEMINI_JSON),
    ]
    client = None
    for builder in build_candidates:
        try:
            client = builder()
            break
        except Exception:
            continue
    if client is None:
        return None


def _infer_schema_from_instructions_with_gemini(
    *,
    instructions: str,
    input_files: list[Path],
    settings: Settings,
    batch_name: str,
) -> TargetSchemaConfig | None:
    snippets: list[dict[str, str]] = []
    if input_files:
        from parsers.router import parse_input

        documents = [parse_input(path) for path in input_files[:2]]
        for document in documents:
            snippets.append(
                {
                    "source_name": document.source_name,
                    "source_type": document.source_type,
                    "markdown_excerpt": document.markdown[:3000],
                }
            )

    import instructor
    from google import genai

    raw_client = genai.Client(api_key=settings.gemini_api_key)
    build_candidates = [
        lambda: instructor.from_genai(raw_client, mode=instructor.Mode.GEMINI_JSON),
        lambda: instructor.from_gemini(raw_client, mode=instructor.Mode.GEMINI_JSON),
    ]
    client = None
    for builder in build_candidates:
        try:
            client = builder()
            break
        except Exception:
            continue
    if client is None:
        return None

    prompt = f"""
Create a CSV extraction schema from the user's instructions.
Return a TargetSchemaConfig with:
- snake_case internal field names
- human-friendly output_column labels
- example_value when the instruction suggests one
- 3 to 10 focused fields

Batch name: {batch_name}
User instructions:
{instructions}

Optional document snippets:
{json.dumps(snippets, indent=2)}
""".strip()

    try:
        return client.chat.completions.create(
            model=settings.extractor_model,
            response_model=TargetSchemaConfig,
            messages=[
                {
                    "role": "system",
                    "content": "You create practical CSV schemas from user extraction instructions.",
                },
                {"role": "user", "content": prompt},
            ],
            max_retries=0,
        )
    except Exception:
        return None

    prompt = f"""
Infer a practical CSV schema for this document batch.
Return a TargetSchemaConfig with snake_case field names and human-friendly output_column values.
Prefer 4 to 8 fields. Avoid redundant metadata fields already captured separately.

Batch name: {batch_name}
Document snippets:
{json.dumps(snippets, indent=2)}
""".strip()

    try:
        return client.chat.completions.create(
            model=settings.extractor_model,
            response_model=TargetSchemaConfig,
            messages=[
                {
                    "role": "system",
                    "content": "You design compact CSV schemas for semi-structured document extraction.",
                },
                {"role": "user", "content": prompt},
            ],
            max_retries=0,
        )
    except Exception:
        return None


def _heuristic_schema_from_documents(batch_name: str) -> TargetSchemaConfig:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return TargetSchemaConfig(
        schema_name=f"{batch_name}_draft_schema_{timestamp}",
        description="Fallback draft schema inferred heuristically from uploaded documents.",
        fields=[
            SchemaFieldSpec(
                name="record_title",
                description="Primary title or heading for the document record.",
                field_type="string",
                output_column="Record Title",
                example_value="Invoice 1042",
            ),
            SchemaFieldSpec(
                name="record_date",
                description="Most relevant date found in the document.",
                field_type="string",
                output_column="Record Date",
                example_value="2026-07-05",
            ),
            SchemaFieldSpec(
                name="record_party",
                description="Main person, company, or entity associated with the record.",
                field_type="string",
                output_column="Record Party",
                example_value="ACME Corp",
            ),
            SchemaFieldSpec(
                name="record_amount",
                description="Most relevant amount or value if present.",
                field_type="number",
                output_column="Record Amount",
                example_value="1250.75",
            ),
            SchemaFieldSpec(
                name="record_summary",
                description="Short summary of the document content.",
                field_type="string",
                output_column="Record Summary",
                example_value="Monthly service invoice for June",
            ),
        ],
    )


def _heuristic_schema_from_instructions(batch_name: str, instructions: str) -> TargetSchemaConfig:
    requested_columns = _extract_requested_columns(instructions)
    if not requested_columns:
        requested_columns = [
            "Record Title",
            "Record Date",
            "Record Party",
            "Record Amount",
            "Record Summary",
        ]

    fields: list[SchemaFieldSpec] = []
    used_names: set[str] = set()
    for column in requested_columns[:10]:
        internal_name = _slugify_header(column, used_names)
        fields.append(
            SchemaFieldSpec(
                name=internal_name,
                description=f"Field requested by user instruction for column '{column}'.",
                field_type=_infer_instruction_field_type(column),
                output_column=column,
                example_value=_infer_instruction_example(column),
            )
        )

    return TargetSchemaConfig(
        schema_name=f"{batch_name}_instruction_schema",
        description=f"Schema drafted from user instructions: {instructions[:160]}",
        fields=fields,
    )


def _extract_requested_columns(instructions: str) -> list[str]:
    candidates: list[str] = []
    for line in instructions.splitlines():
        stripped = line.strip().lstrip("-*").strip()
        if not stripped:
            continue
        if "," in stripped and len(stripped.split(",")) > 1:
            candidates.extend(part.strip() for part in stripped.split(",") if part.strip())
            continue
        if ":" in stripped and any(keyword in stripped.lower() for keyword in ("column", "field", "include")):
            tail = stripped.split(":", 1)[1]
            candidates.extend(part.strip() for part in tail.split(",") if part.strip())
            continue
        if stripped.lower().startswith(("column ", "field ")):
            candidates.append(stripped.split(" ", 1)[1].strip())
            continue
        if len(stripped.split()) <= 5 and any(char.isalpha() for char in stripped):
            candidates.append(stripped)

    normalized: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        cleaned = _normalize_instruction_column(candidate)
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        normalized.append(cleaned)
    return normalized


def _normalize_instruction_column(candidate: str) -> str:
    cleaned = candidate.strip(" .")
    prefixes = (
        "create columns for ",
        "create column for ",
        "columns for ",
        "column for ",
        "include ",
        "field ",
        "fields ",
        "and ",
    )
    lowered = cleaned.lower()
    for prefix in prefixes:
        if lowered.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip(" .")
            lowered = cleaned.lower()
    return cleaned


def _infer_instruction_field_type(column_name: str) -> str:
    lowered = column_name.lower()
    if any(token in lowered for token in ("amount", "total", "price", "cost", "balance", "value")):
        return "number"
    if any(token in lowered for token in ("count", "quantity", "units", "number of")):
        return "integer"
    if any(token in lowered for token in ("is ", "has ", "flag", "status ok", "approved")):
        return "boolean"
    return "string"


def _infer_instruction_example(column_name: str) -> str:
    lowered = column_name.lower()
    if "date" in lowered:
        return "2026-07-05"
    if any(token in lowered for token in ("amount", "total", "price", "cost", "balance", "value")):
        return "1250.75"
    if any(token in lowered for token in ("email",)):
        return "name@example.com"
    if any(token in lowered for token in ("phone", "mobile")):
        return "+1 555 010 1234"
    if any(token in lowered for token in ("invoice", "reference", "id", "number")):
        return "INV-001"
    if any(token in lowered for token in ("name", "vendor", "party", "company", "customer")):
        return "ACME Corp"
    if any(token in lowered for token in ("summary", "description", "notes")):
        return "Short description"
    return f"sample_{_slugify_header(column_name, set())}"
