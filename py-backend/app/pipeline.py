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

from extractor import ExtractionEngine
from config import (
    RuntimePaths,
    Settings,
    get_runtime_paths,
    get_settings,
    refresh_environment,
)
from csv_finalization import finalize_extracted_csv
from parsers.router import discover_supported_files, parse_input
from schemas import (
    REVIEW_STATUS_NEEDS_REVIEW,
    csv_headers,
    flatten_row,
    get_schema_fields,
    get_target_row_model,
    load_schema_config,
    schema_only_row_view,
)
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
        region_override: Any | None = None,
        source_url_map: dict[str, str] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.runtime_paths = runtime_paths or get_runtime_paths()
        self.region_override = region_override
        self.source_url_map = source_url_map or {}
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
        row_model = get_target_row_model(
            schema_path, include_citations=self.settings.extraction_citations_enabled
        )

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
                extraction = self.extractor.extract(
                    parsed_document, prior_error_log=prior_error_log, region_override=self.region_override
                )
                self._write_analysis_artifact(path, parsed_document, extraction)
                if not extraction.payload_rows:
                    raise RuntimeError("Extractor returned no rows for this source.")
                
                # Coverage check. With a deterministic target (row markers counted from
                # the source) this is ground truth; otherwise it is the LLM's estimate.
                # Never silently skip: a MISSING target is itself logged, so an
                # unverifiable run is visible rather than passing as "verified".
                extracted_count = len(extraction.payload_rows)
                expected_total = getattr(extraction.planning, "expected_total_rows", None)
                if expected_total and expected_total > 0:
                    coverage_pct = (extracted_count / expected_total) * 100
                    coverage_status = f"{extracted_count}/{expected_total} rows ({coverage_pct:.0f}%)"
                    if coverage_pct < 90:
                        self.logger.warning(
                            "LOW COVERAGE: extracted %d of %d expected rows (%.1f%%). Some source "
                            "rows may be missing from this extraction.",
                            extracted_count, expected_total, coverage_pct,
                        )
                    else:
                        self.logger.info("Coverage: %s.", coverage_status)
                else:
                    coverage_status = f"{extracted_count} rows (target unverified)"
                    self.logger.warning(
                        "COVERAGE UNVERIFIED: no row-count target was established (no source markers "
                        "and no LLM estimate), so extraction completeness for %s cannot be checked.",
                        parsed_document.source_name,
                    )

                validated_rows, review_metadata_by_row, failed_rows = self._review_and_validate_rows(
                    extraction.payload_rows, parsed_document, extraction.planning, row_model
                )

                self._write_review_artifact(path, parsed_document, validated_rows, review_metadata_by_row)
                # Keep unfixed rows in the CSV, flagged NEEDS_REVIEW, rather than
                # dropping them — the human decides whether they truly need removal.
                all_rows = validated_rows + [failure["row"] for failure in failed_rows]
                row_issues = {
                    len(validated_rows) + i: failure.get("issues", [])
                    for i, failure in enumerate(failed_rows)
                }
                self._append_rows_to_csv(all_rows, row_issues=row_issues)
                # Also record the flagged rows in the manual-review log for detail.
                if failed_rows:
                    self._log_failed_rows_manual_review(path, parsed_document, failed_rows)
                self._mark_processed(path, "verified", error_log_history, retryable=False)
                message = f"Reviewed, validated, and appended {len(all_rows)} row(s) to CSV. Coverage: {coverage_status}."
                if failed_rows:
                    message += f" {len(failed_rows)} row(s) flagged NEEDS_REVIEW."
                return ProcessResult(
                    path=path,
                    status="verified",
                    message=message,
                    stage="transformation_review_and_critic",
                )
            except Exception as exc:
                self.logger.exception("Extraction/validation failed on attempt %d:", attempt_number + 1)
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

    def _canonical_source(self, parsed_document: Any) -> str:
        """Best canonical source link for a document's rows.

        Prefers the explicit input URL (source_url_map, keyed by staged path),
        then the parser-captured URL (websites), then the source path.
        """
        source_path = str(getattr(parsed_document, "source_path", "") or "")
        if source_path in self.source_url_map:
            return self.source_url_map[source_path]
        metadata = getattr(parsed_document, "metadata", {}) or {}
        url = str(metadata.get("url") or "").strip()
        if url:
            return url
        return source_path

    def _review_and_validate_rows(
        self,
        payload_rows: list[BaseModel],
        parsed_document: Any,
        planning: Any,
        row_model: type[BaseModel],
    ) -> tuple[list[BaseModel], list[Any], list[dict[str, Any]]]:
        """Fix-loop quality control.

        For each batch: fix rows from the source (reviewer), evaluate against the
        source (critic), then feed each failing row's specific issues back to the
        reviewer for up to `fix_loop_max_passes` targeted re-fixes. Rows that pass
        are validated; rows that still fail after the loop are collected as
        failures rather than raising. Returns (validated_rows, review_metadata,
        failed_rows) — failed_rows is a list of {row, issues} dicts. Raises only
        when the whole document yields zero validated rows, so the outer retry
        loop still fires for a total failure.
        """
        canonical_source = self._canonical_source(parsed_document)
        schema_fields = {spec.name for spec in get_schema_fields(str(self.settings.schema_config_path))}
        has_source_field = "source" in schema_fields

        def build_row(payload: BaseModel) -> BaseModel:
            return row_model.model_validate(
                {
                    "source_document": parsed_document.source_name,
                    "source_type": parsed_document.source_type,
                    "source_identifier": parsed_document.document_id,
                    "processed_at_utc": datetime.now(timezone.utc).isoformat(),
                    **payload.model_dump(),
                }
            )

        def stamp_source(row: BaseModel) -> BaseModel:
            if not (has_source_field and canonical_source):
                return row
            updates: dict[str, Any] = {}
            if not str(getattr(row, "source", "") or "").strip():
                updates["source"] = canonical_source
                if self.settings.extraction_citations_enabled and not str(
                    getattr(row, "source_source_citation", "") or ""
                ).strip():
                    updates["source_source_citation"] = canonical_source
            return row.model_copy(update=updates) if updates else row

        # Precompute ONE document-level review slice from ALL rows of this source, so
        # every batch reviews against the same document-wide region rather than each
        # batch recomputing its own windows. All rows here share one parsed_document
        # (extraction is per-file), so this is the whole relevant document for the set.
        from extractor import build_review_doc_slice
        from schemas import schema_only_row_view

        all_row_views = [schema_only_row_view(build_row(payload)) for payload in payload_rows]
        doc_review_slice = build_review_doc_slice(parsed_document.markdown, all_row_views)

        def verdict_issues(verdict: Any) -> list[str]:
            if isinstance(verdict, Exception):
                return [str(verdict)]
            if getattr(verdict, "tag", "") != "VALID" or not getattr(verdict, "is_valid", False):
                return list(getattr(verdict, "issues", None) or [getattr(verdict, "confidence_notes", "") or "Row rejected by evaluator."])
            return []

        def preflight_issues(row: BaseModel) -> list[str]:
            # Deterministic, local gate (required-field/noise/grade checks). Runs
            # before the LLM call so blank required fields are caught for free.
            preflight = getattr(self.critic, "_run_contract_preflight", None)
            if preflight is None:
                return []
            try:
                preflight(row)
            except Exception as exc:  # noqa: BLE001 - a preflight failure is this row's issue
                return [str(exc)]
            return []

        def evaluate_and_fix(built: list[BaseModel], prior_issues: list[list[str]] | None) -> list[tuple[BaseModel, Any, list[str]]]:
            """One merged call per batch: fix + verdict. Falls back to the
            separate reviewer/critic calls for engines without the merged method."""
            if hasattr(self.reviewer, "evaluate_and_fix_batch"):
                try:
                    merged = self.reviewer.evaluate_and_fix_batch(
                        built, parsed_document, planning, prior_issues, doc_slice=doc_review_slice
                    )
                    out: list[tuple[BaseModel, Any, list[str]]] = []
                    for result in merged:
                        # Combine the model's remaining issues with the deterministic preflight.
                        issues = list(result.remaining_issues) + preflight_issues(result.row)
                        out.append((result.row, result.metadata, issues))
                    return out
                except Exception as exc:  # noqa: BLE001 - fall back to the two-call path
                    self.logger.warning(
                        "Merged evaluate-and-fix failed (%s); falling back to separate review+critic for %d rows.",
                        type(exc).__name__,
                        len(built),
                    )
            # Fallback: reviewer fixes, critic validates (two calls).
            if hasattr(self.reviewer, "review_and_fix_batch"):
                reviewed = self.reviewer.review_and_fix_batch(built, parsed_document, planning, prior_issues)
            else:
                reviewed = [self.reviewer.review_and_fix(row, parsed_document, planning) for row in built]
            reviewed_rows = [row for row, _ in reviewed]
            if hasattr(self.critic, "validate_batch"):
                verdicts = self.critic.validate_batch(reviewed_rows, parsed_document)
            else:
                verdicts = []
                for row in reviewed_rows:
                    try:
                        verdicts.append(self.critic.validate(row, parsed_document))
                    except Exception as exc:  # noqa: BLE001
                        verdicts.append(exc)
            return [
                (row, review_metadata, verdict_issues(verdict))
                for (row, review_metadata), verdict in zip(reviewed, verdicts)
            ]

        def handle_batch(batch: list[BaseModel]) -> tuple[list[tuple[BaseModel, Any]], list[dict[str, Any]]]:
            # Track each row by its position in the batch across fix passes.
            active_rows = [build_row(payload) for payload in batch]
            prior_issues: list[list[str]] = [[] for _ in active_rows]
            passed: dict[int, tuple[BaseModel, Any]] = {}
            index_map = list(range(len(active_rows)))  # active position -> original batch index

            for pass_number in range(self.settings.fix_loop_max_passes):
                issues_arg = prior_issues if any(prior_issues) else None
                evaluated = evaluate_and_fix(active_rows, issues_arg)

                next_rows: list[BaseModel] = []
                next_prior: list[list[str]] = []
                next_index_map: list[int] = []
                for pos, (row, review_metadata, issues) in enumerate(evaluated):
                    if not issues:
                        passed[index_map[pos]] = (stamp_source(row), review_metadata)
                    else:
                        next_rows.append(row)
                        next_prior.append(issues)
                        next_index_map.append(index_map[pos])

                # Carry only the still-failing rows into the next pass. When none
                # remain, these lists become empty and the loop ends with no
                # outstanding failures.
                active_rows = next_rows
                prior_issues = next_prior
                index_map = next_index_map
                if not active_rows:
                    break

            # Whatever rows remain in active_rows after the loop never passed.
            failures: list[dict[str, Any]] = [
                {"row": active_rows[pos], "issues": prior_issues[pos]}
                for pos in range(len(active_rows))
            ]

            ordered_passed = [passed[i] for i in sorted(passed.keys())]
            return ordered_passed, failures

        batch_size = max(1, self.settings.review_batch_size)
        batches = [payload_rows[i:i + batch_size] for i in range(0, len(payload_rows), batch_size)]

        max_workers = max(1, min(self.settings.row_max_workers, len(batches)))
        if max_workers <= 1 or len(batches) <= 1:
            batch_outputs = [handle_batch(batch) for batch in batches]
        else:
            indexed: list[tuple[list[tuple[BaseModel, Any]], list[dict[str, Any]]] | None] = [None] * len(batches)
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(handle_batch, batch): index
                    for index, batch in enumerate(batches)
                }
                # A batch only raises on an unexpected/infra error (not a rejected
                # row); surface the first such error but let in-flight batches settle.
                first_error: Exception | None = None
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        indexed[index] = future.result()
                    except Exception as exc:  # noqa: BLE001 - re-raised after drain
                        if first_error is None:
                            first_error = exc
                if first_error is not None:
                    raise first_error
            batch_outputs = [item for item in indexed if item is not None]

        validated_rows: list[BaseModel] = []
        review_metadata_by_row: list[Any] = []
        failed_rows: list[dict[str, Any]] = []
        for passed_results, failures in batch_outputs:
            for row, metadata in passed_results:
                validated_rows.append(row)
                review_metadata_by_row.append(metadata)
            failed_rows.extend(failures)

        # Total failure still raises so the outer retry loop (re-extract) fires.
        if not validated_rows and failed_rows:
            issue_summary = "; ".join(
                issue for failure in failed_rows for issue in failure["issues"]
            )[:400]
            raise RuntimeError(f"All rows failed evaluation after fix-loop: {issue_summary}")

        return validated_rows, review_metadata_by_row, failed_rows

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
        # The fix-loop already validates every row against the source, so the
        # final full-CSV audit is a redundant second LLM pass by default. Skip it
        # unless explicitly enabled; the clean deliverable is still written either
        # way (the audit is advisory, not a gate on output).
        finalization_result = finalize_extracted_csv(
            self.runtime_paths.final_csv_path,
            self.settings,
            self.runtime_paths,
            sync_to_sheets=False,
            audit_before_sync=self.settings.run_final_audit,
        )
        self.logger.info(
            "CSV finalization status=%s audit_passed=%s sync_status=%s message=%s",
            finalization_result.status,
            finalization_result.audit_passed,
            finalization_result.sync_status,
            finalization_result.message,
        )

    def _dedupe_exact_rows(self, rows: list[BaseModel]) -> list[BaseModel]:
        """Remove rows that are byte-identical across every schema column.

        Keyed on ``schema_only_row_view`` (schema columns + citations + source),
        which excludes pipeline metadata — notably ``processed_at_utc``, stamped
        per row, which would otherwise make every row unique. Keeps the first
        occurrence so a clean row wins over an otherwise-identical flagged one
        (validated rows precede failed rows in the append batch). Rows differing in
        any schema column (track suffix, disambiguated code, etc.) are preserved.
        """
        seen: set[str] = set()
        deduped: list[BaseModel] = []
        for row in rows:
            key = json.dumps(schema_only_row_view(row), sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        removed = len(rows) - len(deduped)
        if removed:
            self.logger.info(
                "Exact-duplicate reduce: dropped %d byte-identical row(s), kept %d.",
                removed,
                len(deduped),
            )
        return deduped

    def _append_rows_to_csv(
        self, rows: list[BaseModel], row_issues: dict[int, list[str]] | None = None
    ) -> None:
        """Append rows to the internal CSV, flagging any unfixed rows.

        `row_issues` maps a row's index (within `rows`) to its outstanding issues;
        such rows are written with review_status=NEEDS_REVIEW rather than dropped,
        so the CSV is complete and the human can decide whether to keep them.
        """
        schema_path = str(self.settings.schema_config_path)
        headers = csv_headers(schema_path, include_citations=self.settings.extraction_citations_enabled)
        row_issues = row_issues or {}
        display_standard_code_column = self._get_display_standard_code_column(schema_path)
        with self._csv_lock:
            # Map issues to row identity from the ORIGINAL list (row_issues keys are
            # indices into it), BEFORE dedup shifts positions. Dropped duplicates keep
            # their object identity, so their ids simply go unused after dedup.
            issues_by_id = {id(rows[i]): issues for i, issues in row_issues.items()}
            # Drop byte-identical re-extractions (chunk-overlap artifacts) before any
            # collision/uniqueness logic. Intentional duplicates (track suffixes,
            # prefix-disambiguated codes, repeated text under different standards) differ
            # in at least one schema column and are preserved.
            rows = self._dedupe_exact_rows(rows)
            # Collision resolution reorders/rewrites rows; track issues by identity
            # so markers stay attached to the right row after any reshuffle.
            # Uniqueness enforcement runs only on clean rows. Flagged NEEDS_REVIEW
            # rows are excluded so a duplicate/blank code on a problem row cannot
            # sink the whole write; the human resolves those during review.
            clean_rows = [row for row in rows if id(row) not in issues_by_id]
            if display_standard_code_column and self._sample_contract_allows_display_code_disambiguation(schema_path):
                resolved_clean = self._resolve_display_standard_code_collisions(
                    clean_rows, schema_path, display_standard_code_column
                )
                # Rebuild the row order: resolved clean rows keep their sequence,
                # flagged rows are appended (they were dropped from clean_rows).
                flagged_rows = [row for row in rows if id(row) in issues_by_id]
                rows = resolved_clean + flagged_rows
                clean_rows = resolved_clean
            if display_standard_code_column:
                self._assert_unique_display_standard_codes(clean_rows, schema_path, display_standard_code_column)
            write_header = not self.runtime_paths.final_csv_path.exists()
            with self.runtime_paths.final_csv_path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=headers)
                if write_header:
                    writer.writeheader()
                for row in rows:
                    issues = issues_by_id.get(id(row))
                    if issues:
                        writer.writerow(
                            flatten_row(
                                row,
                                schema_path,
                                review_status=REVIEW_STATUS_NEEDS_REVIEW,
                                review_issues=" | ".join(issues),
                                include_citations=self.settings.extraction_citations_enabled,
                            )
                        )
                    else:
                        writer.writerow(
                            flatten_row(
                                row,
                                schema_path,
                                include_citations=self.settings.extraction_citations_enabled,
                            )
                        )

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
            flattened = flatten_row(
                row,
                schema_path,
                include_citations=self.settings.extraction_citations_enabled,
            )
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

    def _log_failed_rows_manual_review(
        self, path: Path, parsed_document: Any, failed_rows: list[dict[str, Any]]
    ) -> None:
        """Record individual rows that failed the fix-loop, with their issues.

        Unlike _log_manual_review (whole-document failure), this logs specific
        rows that could not be fixed while the rest of the document succeeded.
        """
        if not failed_rows:
            return
        with self._manual_review_lock:
            existing: list[dict[str, Any]] = []
            if self.runtime_paths.manual_review_path.exists():
                existing = json.loads(self.runtime_paths.manual_review_path.read_text(encoding="utf-8"))
            for failure in failed_rows:
                row = failure.get("row")
                existing.append(
                    {
                        "source_path": str(path),
                        "document_id": getattr(parsed_document, "document_id", path.stem),
                        "stage": "row_fix_loop",
                        "errors": failure.get("issues", []),
                        "row": schema_only_row_view(row) if row is not None else None,
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
            draft_extractor_model=refreshed_settings.draft_extractor_model,
            draft_extractor_provider=refreshed_settings.draft_extractor_provider,
            critic_provider=refreshed_settings.critic_provider,
            critic_model=refreshed_settings.critic_model,
            portkey_extractor_provider=refreshed_settings.portkey_extractor_provider,
            portkey_critic_provider=refreshed_settings.portkey_critic_provider,
            extractor_fallbacks=refreshed_settings.extractor_fallbacks,
            critic_fallbacks=refreshed_settings.critic_fallbacks,
            watch_interval_seconds=refreshed_settings.watch_interval_seconds,
            max_retries=refreshed_settings.max_retries,
            extraction_max_workers=refreshed_settings.extraction_max_workers,
            batch_max_workers=refreshed_settings.batch_max_workers,
            row_max_workers=refreshed_settings.row_max_workers,
            llm_max_concurrency=refreshed_settings.llm_max_concurrency,
            extraction_max_chars_per_chunk=refreshed_settings.extraction_max_chars_per_chunk,
            extraction_citations_enabled=refreshed_settings.extraction_citations_enabled,
            enable_region_targeting=refreshed_settings.enable_region_targeting,
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
