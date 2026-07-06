from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from pydantic import BaseModel

from agent_engine import ExtractionEngine
from config import (
    RuntimePaths,
    Settings,
    get_runtime_paths,
    get_settings,
    refresh_environment,
)
from csv_finalization import finalize_extracted_csv
from parsers.router import discover_supported_files, parse_input
from schemas import csv_headers, flatten_row, get_schema_fields, get_target_row_model, load_schema_config
from validation.critic import ExtractionCritic
from validation.reviewer import TransformationReviewer


@dataclass(slots=True)
class ProcessResult:
    path: Path
    status: str
    message: str
    stage: str


class ProcessingState:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, data: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")


class DataTransformationPipeline:
    def __init__(
        self,
        settings: Settings | None = None,
        runtime_paths: RuntimePaths | None = None,
        extractor: Any | None = None,
        reviewer: Any | None = None,
        critic: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.runtime_paths = runtime_paths or get_runtime_paths()
        self._uses_default_extractor = extractor is None
        self._uses_default_reviewer = reviewer is None
        self._uses_default_critic = critic is None
        self.extractor = extractor or ExtractionEngine(self.settings)
        self.reviewer = reviewer or TransformationReviewer(self.settings)
        self.critic = critic or ExtractionCritic(self.settings)
        self.state = ProcessingState(self.runtime_paths.state_path)
        self.logger = logging.getLogger(__name__)
        self._csv_lock = Lock()
        self._state_lock = Lock()
        self._manual_review_lock = Lock()
        self._monitor_status_lock = Lock()

    def process_pending_documents(self) -> list[ProcessResult]:
        self._refresh_runtime_config()
        return self.process_paths(discover_supported_files(self.runtime_paths.input_dir), refresh_config=False)

    def process_paths(self, paths: list[Path], refresh_config: bool = True) -> list[ProcessResult]:
        if refresh_config:
            self._refresh_runtime_config()
        candidate_paths = [path for path in paths if not self._already_processed(path)]
        if not candidate_paths:
            return []

        results: list[ProcessResult] = []
        max_workers = min(self.settings.extraction_max_workers, len(candidate_paths))
        if max_workers <= 1:
            for path in candidate_paths:
                result = self.process_path(path)
                results.append(result)
                self.logger.info(
                    "Processed %s with status=%s stage=%s message=%s",
                    path.name,
                    result.status,
                    result.stage,
                    result.message,
                )
        else:
            indexed_results: list[ProcessResult | None] = [None] * len(candidate_paths)
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(self.process_path, path): index
                    for index, path in enumerate(candidate_paths)
                }
                for future in as_completed(futures):
                    index = futures[future]
                    result = future.result()
                    indexed_results[index] = result
                    self.logger.info(
                        "Processed %s with status=%s stage=%s message=%s",
                        result.path.name,
                        result.status,
                        result.stage,
                        result.message,
                    )
            results = [result for result in indexed_results if result is not None]

        self._finalize_csv_after_processing(results)
        return results

    def process_path(self, path: Path) -> ProcessResult:
        parsed_document: Any | None = None
        extraction: Any | None = None
        review_metadata_by_row: list[Any] = []
        error_log_history: list[str] = []
        schema_path = str(self.settings.schema_config_path)
        row_model = get_target_row_model(schema_path)

        try:
            parsed_document = parse_input(path)
        except Exception as exc:
            failure_message = f"attempt=1 error={exc}"
            error_log_history.append(failure_message)
            self._log_manual_review(path, parsed_document, error_log_history, stage="parse")
            self._mark_processed(path, "manual_review", error_log_history, retryable=False)
            return ProcessResult(path=path, status="manual_review", message=failure_message, stage="parse")

        for attempt_number in range(self.settings.max_retries + 1):
            prior_error_log = "\n\n".join(error_log_history) if error_log_history else None
            try:
                extraction = self.extractor.extract(parsed_document, prior_error_log=prior_error_log)
                self._write_analysis_artifact(path, parsed_document, extraction)
                if not extraction.payload_rows:
                    raise RuntimeError("Extractor returned no rows for this source.")

                validated_rows: list[BaseModel] = []
                review_metadata_by_row = []
                for payload in extraction.payload_rows:
                    row = row_model.model_validate(
                        {
                            "source_document": parsed_document.source_name,
                            "source_type": parsed_document.source_type,
                            "source_identifier": parsed_document.document_id,
                            "processed_at_utc": datetime.now(timezone.utc).isoformat(),
                            **payload.model_dump(),
                        }
                    )
                    row, review_metadata = self.reviewer.review_and_fix(row, parsed_document, extraction.planning)
                    verdict = self.critic.validate(row, parsed_document)
                    if verdict.tag != "VALID":
                        raise RuntimeError("Critic did not return a VALID tag.")
                    validated_rows.append(row)
                    review_metadata_by_row.append(review_metadata)

                self._write_review_artifact(path, parsed_document, validated_rows, review_metadata_by_row)
                self._append_rows_to_csv(validated_rows)
                self._mark_processed(path, "verified", error_log_history, retryable=False)
                return ProcessResult(
                    path=path,
                    status="verified",
                    message=f"Reviewed, validated, and appended {len(validated_rows)} row(s) to CSV.",
                    stage="transformation_review_and_critic",
                )
            except Exception as exc:
                failure_message = f"attempt={attempt_number + 1} error={exc}"
                error_log_history.append(failure_message)
                if attempt_number >= self.settings.max_retries:
                    retryable = self._errors_are_retryable(error_log_history)
                    self._log_manual_review(
                        path,
                        parsed_document,
                        error_log_history,
                        stage="extract_or_critic",
                        extraction=extraction,
                        review_metadata=review_metadata_by_row,
                    )
                    final_status = "pending_retry" if retryable else "manual_review"
                    self._mark_processed(path, final_status, error_log_history, retryable=retryable)
                    return ProcessResult(
                        path=path,
                        status=final_status,
                        message=(
                            f"{failure_message} Waiting for runtime configuration change before retry."
                            if retryable
                            else failure_message
                        ),
                        stage="extract_or_critic",
                    )

        raise RuntimeError("Unreachable retry loop state.")

    def watch_forever(self) -> None:
        self._write_monitor_status(status="running", last_error="", last_results=[])
        while True:
            cycle_started_at = datetime.now(timezone.utc)
            try:
                results = self.process_pending_documents()
                self._write_monitor_status(
                    status="running",
                    last_error="",
                    last_results=results,
                    cycle_started_at=cycle_started_at,
                )
            except Exception as exc:
                self.logger.exception("Watcher cycle failed: %s", exc)
                self._write_monitor_status(
                    status="running_with_errors",
                    last_error=str(exc),
                    last_results=[],
                    cycle_started_at=cycle_started_at,
                )
            time.sleep(self.settings.watch_interval_seconds)

    def _finalize_csv_after_processing(self, results: list[ProcessResult]) -> None:
        if not results:
            return
        if not any(result.status == "verified" for result in results):
            return
        finalization_result = finalize_extracted_csv(
            self.runtime_paths.final_csv_path,
            self.settings,
            self.runtime_paths,
            sync_to_sheets=False,
        )
        self.logger.info(
            "CSV finalization status=%s audit_passed=%s sync_status=%s message=%s",
            finalization_result.status,
            finalization_result.audit_passed,
            finalization_result.sync_status,
            finalization_result.message,
        )

    def _append_rows_to_csv(self, rows: list[BaseModel]) -> None:
        schema_path = str(self.settings.schema_config_path)
        headers = csv_headers(schema_path)
        display_standard_code_column = self._get_display_standard_code_column(schema_path)
        with self._csv_lock:
            if display_standard_code_column and self._sample_contract_allows_display_code_disambiguation(schema_path):
                rows = self._resolve_display_standard_code_collisions(rows, schema_path, display_standard_code_column)
            if display_standard_code_column:
                self._assert_unique_display_standard_codes(rows, schema_path, display_standard_code_column)
            write_header = not self.runtime_paths.final_csv_path.exists()
            with self.runtime_paths.final_csv_path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=headers)
                if write_header:
                    writer.writeheader()
                for row in rows:
                    writer.writerow(flatten_row(row, schema_path))

    def _get_display_standard_code_column(self, schema_path: str) -> str | None:
        for spec in get_schema_fields(schema_path):
            if spec.name == "display_standard_code":
                return spec.output_column or spec.name
        return None

    def _sample_contract_allows_display_code_disambiguation(self, schema_path: str) -> bool:
        contract = load_schema_config(schema_path, settings=self.settings).sample_contract
        if contract is None:
            return False
        logic = " ".join(
            [
                contract.display_standard_code_logic,
                " ".join(contract.description_integrity_rules),
                " ".join(contract.disallowed_output_content),
            ]
        ).lower()
        return any(
            marker in logic
            for marker in (
                "synthetic",
                "transformed",
                "prefix",
                "domain code",
                "topic code",
                "disambiguate",
                "unique",
            )
        )

    def _resolve_display_standard_code_collisions(
        self,
        rows: list[BaseModel],
        schema_path: str,
        display_standard_code_column: str,
    ) -> list[BaseModel]:
        schema_fields = get_schema_fields(schema_path)
        display_standard_code_field = next(
            (spec.name for spec in schema_fields if spec.name == "display_standard_code"),
            None,
        )
        domain_field = next((spec.name for spec in schema_fields if spec.name == "domain"), None)
        topic_field = next((spec.name for spec in schema_fields if spec.name == "topic"), None)
        if not display_standard_code_field:
            return rows

        assigned_codes: set[str] = set()
        if self.runtime_paths.final_csv_path.exists():
            with self.runtime_paths.final_csv_path.open(newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                for existing_row in reader:
                    existing_code = (existing_row.get(display_standard_code_column) or "").strip()
                    if existing_code:
                        assigned_codes.add(existing_code)

        normalized_rows: list[BaseModel] = []
        for row in rows:
            current_code = str(getattr(row, display_standard_code_field, "") or "").strip()
            if not current_code:
                normalized_rows.append(row)
                continue
            if current_code not in assigned_codes:
                assigned_codes.add(current_code)
                normalized_rows.append(row)
                continue

            resolved_row = row
            resolved_code = current_code
            for prefix in self._iter_display_standard_code_prefixes(row, domain_field, topic_field):
                candidate_code = f"{prefix}.{current_code}"
                if candidate_code not in assigned_codes:
                    resolved_row = row.model_copy(update={display_standard_code_field: candidate_code})
                    resolved_code = candidate_code
                    assigned_codes.add(candidate_code)
                    break
            normalized_rows.append(resolved_row)
            if resolved_code == current_code:
                # Leave unresolved collisions to the hard uniqueness assertion below.
                continue
        return normalized_rows

    def _iter_display_standard_code_prefixes(
        self,
        row: BaseModel,
        domain_field: str | None,
        topic_field: str | None,
    ) -> list[str]:
        candidates: list[str] = []
        seen: set[str] = set()
        for field_name in (domain_field, topic_field):
            if not field_name:
                continue
            raw_value = str(getattr(row, field_name, "") or "").strip()
            if not raw_value:
                continue
            parts = [part.strip() for part in raw_value.split("|") if part.strip()]
            for part in parts or [raw_value]:
                for prefix in self._abbreviation_candidates(part):
                    if prefix not in seen:
                        seen.add(prefix)
                        candidates.append(prefix)
        return candidates

    def _abbreviation_candidates(self, value: str) -> list[str]:
        tokens = re.findall(r"[A-Za-z0-9]+", value.upper())
        if not tokens:
            return []
        collapsed = "".join(tokens)
        initials = "".join(token[0] for token in tokens if token)
        candidates: list[str] = []
        for source in (initials, collapsed):
            for length in (2, 3, 4):
                if len(source) >= length:
                    candidate = source[:length]
                    if candidate not in candidates:
                        candidates.append(candidate)
        return candidates

    def _assert_unique_display_standard_codes(
        self,
        rows: list[BaseModel],
        schema_path: str,
        display_standard_code_column: str,
    ) -> None:
        existing_codes: dict[str, int] = {}
        if self.runtime_paths.final_csv_path.exists():
            with self.runtime_paths.final_csv_path.open(newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                for row_number, existing_row in enumerate(reader, start=2):
                    display_code = (existing_row.get(display_standard_code_column) or "").strip()
                    if display_code:
                        existing_codes.setdefault(display_code, row_number)

        seen_new_codes: dict[str, int] = {}
        for index, row in enumerate(rows, start=1):
            flattened = flatten_row(row, schema_path)
            display_code = str(flattened.get(display_standard_code_column) or "").strip()
            if not display_code:
                continue
            if display_code in seen_new_codes:
                first_index = seen_new_codes[display_code]
                raise RuntimeError(
                    f"Duplicate Display standard code '{display_code}' found among new rows "
                    f"(row positions {first_index} and {index} in the current append batch)."
                )
            if display_code in existing_codes:
                existing_row_number = existing_codes[display_code]
                raise RuntimeError(
                    f"Duplicate Display standard code '{display_code}' conflicts with existing CSV row {existing_row_number}."
                )
            seen_new_codes[display_code] = index

    def _write_analysis_artifact(self, path: Path, parsed_document: Any, extraction: Any) -> None:
        self.runtime_paths.analysis_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = self.runtime_paths.analysis_dir / f"{path.stem}.analysis.json"
        artifact = {
            "source_path": str(path),
            "document_id": getattr(parsed_document, "document_id", path.stem),
            "source_name": getattr(parsed_document, "source_name", path.name),
            "logged_at_utc": datetime.now(timezone.utc).isoformat(),
            "planning": extraction.planning.model_dump(mode="json"),
            "anchoring_plan": extraction.anchoring_plan,
        }
        artifact_path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")

    def _write_review_artifact(
        self,
        path: Path,
        parsed_document: Any,
        rows: list[BaseModel],
        review_metadata_by_row: list[Any],
    ) -> None:
        self.runtime_paths.review_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = self.runtime_paths.review_dir / f"{path.stem}.review.json"
        artifact = {
            "source_path": str(path),
            "document_id": getattr(parsed_document, "document_id", path.stem),
            "source_name": getattr(parsed_document, "source_name", path.name),
            "logged_at_utc": datetime.now(timezone.utc).isoformat(),
            "review_rows": [
                metadata.model_dump(mode="json") if metadata is not None else None
                for metadata in review_metadata_by_row
            ],
            "corrected_rows": [row.model_dump(mode="json") for row in rows],
        }
        artifact_path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")

    def _log_manual_review(
        self,
        path: Path,
        parsed_document: Any,
        errors: list[str],
        stage: str,
        extraction: Any | None = None,
        review_metadata: Any | None = None,
    ) -> None:
        with self._manual_review_lock:
            existing: list[dict[str, Any]] = []
            if self.runtime_paths.manual_review_path.exists():
                existing = json.loads(self.runtime_paths.manual_review_path.read_text(encoding="utf-8"))
            existing.append(
                {
                    "source_path": str(path),
                    "document_id": getattr(parsed_document, "document_id", path.stem),
                    "stage": stage,
                    "errors": errors,
                    "markdown_excerpt": getattr(parsed_document, "markdown", "")[:4000],
                    "analysis_plan": (
                        extraction.planning.model_dump(mode="json")
                        if extraction and getattr(extraction, "planning", None) is not None
                        else None
                ),
                "transformation_review": (
                    [metadata.model_dump(mode="json") if metadata is not None else None for metadata in review_metadata]
                    if isinstance(review_metadata, list)
                    else review_metadata.model_dump(mode="json")
                    if review_metadata is not None
                    else None
                ),
                "logged_at_utc": datetime.now(timezone.utc).isoformat(),
            }
        )
            self.runtime_paths.manual_review_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")

    def _already_processed(self, path: Path) -> bool:
        state = self.state.load()
        fingerprint = self._fingerprint(path)
        entry = state.get(str(path))
        if not entry or entry.get("fingerprint") != fingerprint:
            return False

        normalized_entry = self._normalize_retryable_entry(path, state, entry)
        if normalized_entry is not None:
            entry = normalized_entry

        status = entry.get("status")
        if status == "verified":
            return True
        if status == "pending_retry":
            return entry.get("config_signature") == self._config_signature()
        return True

    def _mark_processed(self, path: Path, status: str, errors: list[str], retryable: bool) -> None:
        with self._state_lock:
            state = self.state.load()
            state[str(path)] = {
                "fingerprint": self._fingerprint(path),
                "status": status,
                "retryable": retryable,
                "config_signature": self._config_signature() if retryable else None,
                "errors": errors,
                "processed_at_utc": datetime.now(timezone.utc).isoformat(),
            }
            self.state.save(state)

    @staticmethod
    def _fingerprint(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _write_monitor_status(
        self,
        status: str,
        last_error: str,
        last_results: list[ProcessResult],
        cycle_started_at: datetime | None = None,
    ) -> None:
        with self._monitor_status_lock:
            existing_status: dict[str, Any] = {}
            if self.runtime_paths.monitor_status_path.exists():
                try:
                    existing_status = json.loads(
                        self.runtime_paths.monitor_status_path.read_text(encoding="utf-8")
                    )
                except json.JSONDecodeError:
                    existing_status = {}

            rendered_results = [
                {
                    "path": str(result.path),
                    "status": result.status,
                    "message": result.message,
                    "stage": result.stage,
                }
                for result in last_results
            ]
            processed_total = int(existing_status.get("documents_processed_total", 0)) + len(last_results)
            last_activity_results = (
                rendered_results if rendered_results else existing_status.get("last_activity_results", [])
            )

            payload = {
                "status": status,
                "watch_interval_seconds": self.settings.watch_interval_seconds,
                "config_signature": self._config_signature(),
                "last_error": last_error,
                "updated_at_utc": datetime.now(timezone.utc).isoformat(),
                "cycle_started_at_utc": cycle_started_at.isoformat() if cycle_started_at else None,
                "documents_processed_total": processed_total,
                "last_cycle_results": rendered_results,
                "last_activity_results": last_activity_results,
            }
            self.runtime_paths.monitor_status_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _refresh_runtime_config(self) -> None:
        refresh_environment()
        preserved_schema_path = self.settings.schema_config_path
        refreshed_settings = get_settings()
        self.settings = Settings(
            extractor_model=refreshed_settings.extractor_model,
            extractor_provider=refreshed_settings.extractor_provider,
            critic_provider=refreshed_settings.critic_provider,
            critic_model=refreshed_settings.critic_model,
            portkey_extractor_provider=refreshed_settings.portkey_extractor_provider,
            portkey_critic_provider=refreshed_settings.portkey_critic_provider,
            watch_interval_seconds=refreshed_settings.watch_interval_seconds,
            max_retries=refreshed_settings.max_retries,
            extraction_max_workers=refreshed_settings.extraction_max_workers,
            batch_max_workers=refreshed_settings.batch_max_workers,
            schema_config_path=preserved_schema_path,
            portkey_api_key=refreshed_settings.portkey_api_key,
            gemini_api_key=refreshed_settings.gemini_api_key,
            openai_api_key=refreshed_settings.openai_api_key,
            anthropic_api_key=refreshed_settings.anthropic_api_key,
            google_sheets_sync_enabled=refreshed_settings.google_sheets_sync_enabled,
            google_sheets_spreadsheet_id=refreshed_settings.google_sheets_spreadsheet_id,
            google_sheets_sheet_name=refreshed_settings.google_sheets_sheet_name,
            google_service_account_json=refreshed_settings.google_service_account_json,
            google_service_account_json_path=refreshed_settings.google_service_account_json_path,
            google_oauth_client_secret_path=refreshed_settings.google_oauth_client_secret_path,
            google_oauth_token_path=refreshed_settings.google_oauth_token_path,
        )
        if self._uses_default_extractor:
            self.extractor = ExtractionEngine(self.settings)
        if self._uses_default_critic:
            self.critic = ExtractionCritic(self.settings)

    def _config_signature(self) -> str:
        signature_payload = {
            "extractor_model": self.settings.extractor_model,
            "extractor_provider": self.settings.extractor_provider,
            "critic_provider": self.settings.critic_provider,
            "critic_model": self.settings.critic_model,
            "portkey_extractor_provider": self.settings.portkey_extractor_provider,
            "portkey_critic_provider": self.settings.portkey_critic_provider,
            "schema_config_path": str(self.settings.schema_config_path),
            "portkey_api_key_present": bool(self.settings.portkey_api_key),
            "gemini_api_key_present": bool(self.settings.gemini_api_key),
            "openai_api_key_present": bool(self.settings.openai_api_key),
            "anthropic_api_key_present": bool(self.settings.anthropic_api_key),
        }
        return json.dumps(signature_payload, sort_keys=True)

    @staticmethod
    def _errors_are_retryable(errors: list[str]) -> bool:
        retryable_markers = (
            "PORTKEY_API_KEY is not configured",
            "Portkey support requires the 'portkey-ai' package",
            "GEMINI_API_KEY is not configured",
            "OPENAI_API_KEY is not configured",
            "ANTHROPIC_API_KEY is not configured",
        )
        return any(marker in error for error in errors for marker in retryable_markers)

    def _normalize_retryable_entry(
        self,
        path: Path,
        state: dict[str, Any],
        entry: dict[str, Any],
    ) -> dict[str, Any] | None:
        if entry.get("status") == "manual_review" and self._errors_are_retryable(entry.get("errors", [])):
            updated_entry = {
                **entry,
                "status": "pending_retry",
                "retryable": True,
                "config_signature": entry.get("config_signature") or self._config_signature(),
            }
            state[str(path)] = updated_entry
            self.state.save(state)
            return updated_entry
        return None
