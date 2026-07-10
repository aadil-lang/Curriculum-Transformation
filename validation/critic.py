from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from extractor import _normalize_to_string_list, build_review_doc_slice
from config import Settings, get_settings
from parsers.base import ParsedDocument
from schemas import ALLOWED_GRADE_LEVELS, TargetSchemaConfig, critic_row_view, get_output_column_name, load_schema_config


class CriticValidationError(RuntimeError):
    pass


LOGGER = logging.getLogger(__name__)


def _citation_audit_guidance(schema_config: TargetSchemaConfig, include_citations: bool) -> str:
    if not include_citations:
        return ""
    verbatim_columns = [
        get_output_column_name(spec) for spec in schema_config.fields if spec.requires_citation
    ]
    derived_columns = [
        get_output_column_name(spec) for spec in schema_config.fields if not spec.requires_citation
    ]
    verbatim_list = ", ".join(verbatim_columns) or "(none)"
    derived_list = ", ".join(derived_columns) or "(none)"
    return f"""
Field citation policy:
- Verbatim-cited fields ({verbatim_list}): the citation should contain the field's text as it
  appears in the source. Ignore differences in surrounding whitespace, tabs, leading list
  markers/codes, and line breaks — a match that differs only in formatting IS a valid verbatim
  citation. Reject only if the cited text is genuinely absent from the source or clearly
  supports a different value.
- Derived/transformed fields ({derived_list}): these carry no citation and must NOT be judged on
  citations at all. Accept them as long as the value is a reasonable transformation consistent
  with the source context and the sample contract (e.g. a canonical source URL, a grade band
  mapped to Elementary/Middle/High School, a topic taken from a heading, a formed/prefixed
  display code, a merged ` | ` topic).
""".strip()


def _citation_reject_bullets(include_citations: bool) -> str:
    if include_citations:
        return (
            "- A verbatim-cited field's cited text is not present in the source at all.\n"
            "- A description is truncated mid-sentence or drops required sub-parts that the citation shows.\n"
            "- Loss/garbling of mathematical, chemistry, Greek, notation, or multilingual characters that the "
            "source citation shows."
        )
    return (
        "- A description is truncated mid-sentence or drops required sub-parts visible in the source.\n"
        "- Loss/garbling of mathematical, chemistry, Greek, notation, or multilingual characters that the "
        "source clearly contains."
    )


def _citation_do_not_reject_clause(include_citations: bool) -> str:
    base = (
        "a prefixed/synthesized display code, a canonical source URL not printed in the body, a null "
        "optional field, or duplicate display codes (uniqueness is resolved later by the pipeline)."
    )
    if include_citations:
        return (
            "Do NOT reject for: an empty citation on a derived field, a formatting-only citation difference, "
            + base
        )
    return f"Do NOT reject for: {base}"


class SemanticAuditVerdict(BaseModel):
    tag: Literal["VALID", "INVALID"] = Field(
        description="Programmatic validation tag used to gate final CSV writes."
    )
    is_valid: bool = Field(description="Whether the extracted row is semantically supported by the source.")
    issues: list[str] = Field(default_factory=list, description="Specific issues with unsupported or irrelevant fields.")
    confidence_notes: str = Field(default="", description="Short explanation for the verdict.")

    @field_validator("issues", mode="before")
    @classmethod
    def _normalize_issues(cls, value: Any) -> list[str]:
        return _normalize_to_string_list(value)


class SemanticAuditFinding(BaseModel):
    row_index: int = Field(description="Zero-based index of the row this verdict applies to.")
    tag: Literal["VALID", "INVALID"] = Field(description="Validation tag for this row.")
    is_valid: bool = Field(description="Whether this row is semantically supported by the source.")
    issues: list[str] = Field(default_factory=list, description="Specific issues for this row.")
    confidence_notes: str = Field(default="", description="Short explanation for this row's verdict.")

    @field_validator("issues", mode="before")
    @classmethod
    def _normalize_issues(cls, value: Any) -> list[str]:
        return _normalize_to_string_list(value)


class SemanticBatchAuditVerdict(BaseModel):
    findings: list[SemanticAuditFinding] = Field(
        default_factory=list, description="One verdict per input row, echoing its row_index."
    )


class ExtractionCritic:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def validate(self, row: BaseModel, parsed_document: ParsedDocument) -> SemanticAuditVerdict:
        row.__class__.model_validate(row.model_dump())
        self._run_contract_preflight(row)
        verdict = self._semantic_audit(row, parsed_document)
        if verdict.tag != "VALID" or not verdict.is_valid:
            issue_block = " | ".join(verdict.issues) if verdict.issues else verdict.confidence_notes
            raise CriticValidationError(issue_block or "Semantic audit failed.")
        return verdict

    def validate_batch(
        self, rows: list[BaseModel], parsed_document: ParsedDocument
    ) -> list[SemanticAuditVerdict | CriticValidationError]:
        """Validate several rows, returning a per-row verdict OR the error for that row.

        Preserves the single-row gate: each row is Pydantic-revalidated and run
        through the programmatic preflight locally; a row that fails either gets a
        CriticValidationError in its slot without an LLM call. Rows that pass
        preflight are audited together in one model call. Any batch-level failure
        falls back to per-row validate() so semantics never silently change.
        """
        if not rows:
            return []
        if len(rows) == 1:
            try:
                return [self.validate(rows[0], parsed_document)]
            except CriticValidationError as exc:
                return [exc]

        results: list[SemanticAuditVerdict | CriticValidationError | None] = [None] * len(rows)
        to_audit: list[tuple[int, BaseModel]] = []
        for index, row in enumerate(rows):
            try:
                row.__class__.model_validate(row.model_dump())
                self._run_contract_preflight(row)
            except CriticValidationError as exc:
                results[index] = exc
                continue
            to_audit.append((index, row))

        if to_audit:
            audited = self._semantic_audit_batch([row for _, row in to_audit], parsed_document)
            for (index, _row), verdict in zip(to_audit, audited):
                if verdict.tag != "VALID" or not verdict.is_valid:
                    issue_block = " | ".join(verdict.issues) if verdict.issues else verdict.confidence_notes
                    results[index] = CriticValidationError(issue_block or "Semantic audit failed.")
                else:
                    results[index] = verdict

        return [
            result if result is not None else CriticValidationError("Critic produced no verdict for this row.")
            for result in results
        ]

    def _semantic_audit_batch(
        self, rows: list[BaseModel], parsed_document: ParsedDocument
    ) -> list[SemanticAuditVerdict]:
        """One batched semantic audit call; falls back to per-row on any failure."""
        try:
            batch = self._audit_batch_with_model(rows, parsed_document)
        except Exception as exc:  # noqa: BLE001 - fall back to proven per-row audit
            LOGGER.warning(
                "Batch semantic audit failed (%s); falling back to per-row audit for %d rows.",
                type(exc).__name__,
                len(rows),
            )
            return [self._semantic_audit(row, parsed_document) for row in rows]

        by_index: dict[int, SemanticAuditFinding] = {}
        for finding in batch.findings:
            if 0 <= finding.row_index < len(rows) and finding.row_index not in by_index:
                by_index[finding.row_index] = finding
        if len(by_index) != len(rows):
            LOGGER.warning(
                "Batch audit covered %d of %d rows; falling back to per-row audit.",
                len(by_index),
                len(rows),
            )
            return [self._semantic_audit(row, parsed_document) for row in rows]

        return [
            SemanticAuditVerdict(
                tag=by_index[index].tag,
                is_valid=by_index[index].is_valid,
                issues=by_index[index].issues,
                confidence_notes=by_index[index].confidence_notes,
            )
            for index in range(len(rows))
        ]

    def _semantic_audit(self, row: BaseModel, parsed_document: ParsedDocument) -> SemanticAuditVerdict:
        if self.settings.portkey_api_key:
            return self._audit_with_portkey(row, parsed_document)
        provider = self.settings.critic_provider
        if provider == "openai":
            return self._audit_with_openai(row, parsed_document)
        if provider == "anthropic":
            return self._audit_with_anthropic(row, parsed_document)
        raise CriticValidationError(f"Unsupported critic provider: {provider}")

    def _build_audit_messages(self, row: BaseModel, parsed_document: ParsedDocument) -> list[dict[str, str]]:
        schema_config = load_schema_config(settings=self.settings)
        include_citations = self.settings.extraction_citations_enabled
        row_view = critic_row_view(row, schema_config, include_citations=include_citations)
        row_json = json.dumps(row_view, indent=2)
        doc_slice = build_review_doc_slice(parsed_document.markdown, [row_view])
        sample_contract_json = (
            json.dumps(schema_config.sample_contract.model_dump(mode="json"), indent=2)
            if schema_config.sample_contract
            else "null"
        )
        citation_guidance = _citation_audit_guidance(schema_config, include_citations)
        citation_guidance_block = f"\n\n{citation_guidance}" if citation_guidance else ""
        prompt = f"""
Audit this extracted row against the original document and the approved sample CSV contract.

Default to VALID. This pipeline's job is to TRANSFORM messy source text into clean contract
values, so transformation, mapping, inheritance, and synthesis are EXPECTED and correct — not
suspicious. Return tag=INVALID only when you can point to a CONCRETE, demonstrable violation
(quote the offending value and say exactly which rule it breaks). Do NOT reject on speculation,
hedging, or vague doubt ("possible", "may have", "risks", "insufficient clarity", "lacks
explicit evidence" are NOT valid reasons). If you are not sure a row is wrong, it is VALID.{citation_guidance_block}

Reject ONLY for these concrete problems:
{_citation_reject_bullets(include_citations)}
- A field value is clearly copied from the wrong place (neighboring-row contamination) or is
  document noise (headers, footers, page numbers, N.B. notes, nav links, layout labels).
- grade_level is not exactly one of: Elementary School, Middle School, High School.
- A value plainly contradicts the sample CSV contract's stated rules.

{_citation_do_not_reject_clause(include_citations)}

Sample CSV transformation contract:
{sample_contract_json}

Extracted row:
{row_json}

Relevant source excerpts (the regions of the document where this row's content appears):
{doc_slice}
""".strip()

        return [
            {
                "role": "system",
                "content": (
                    "You are a data extraction validator. Your goal is to pass every row that is a "
                    "faithful, contract-consistent transformation of the source, and to reject only "
                    "rows with a concrete, demonstrable defect you can point to. Transformation of "
                    "source text into contract values is expected and correct, not a defect. When in "
                    "doubt, return VALID. Return your response as a single JSON object."
                ),
            },
            {"role": "user", "content": prompt},
        ]

    def _build_batch_audit_messages(
        self, rows: list[BaseModel], parsed_document: ParsedDocument
    ) -> list[dict[str, str]]:
        schema_config = load_schema_config(settings=self.settings)
        include_citations = self.settings.extraction_citations_enabled
        row_views = [
            critic_row_view(row, schema_config, include_citations=include_citations)
            for row in rows
        ]
        doc_slice = build_review_doc_slice(parsed_document.markdown, row_views)
        rows_json = json.dumps(
            [
                {"row_index": index, "row": view}
                for index, view in enumerate(row_views)
            ],
            indent=2,
        )
        sample_contract_json = (
            json.dumps(schema_config.sample_contract.model_dump(mode="json"), indent=2)
            if schema_config.sample_contract
            else "null"
        )
        citation_guidance = _citation_audit_guidance(schema_config, include_citations)
        citation_guidance_block = f"\n\n{citation_guidance}" if citation_guidance else ""
        prompt = f"""
Audit each extracted row against the original document and the approved sample CSV contract.
Judge every row INDEPENDENTLY and return exactly one verdict per row, echoing its row_index.

Default to VALID. This pipeline's job is to TRANSFORM messy source text into clean contract
values, so transformation, mapping, inheritance, and synthesis are EXPECTED and correct — not
suspicious. Return tag=INVALID for a row only when you can point to a CONCRETE, demonstrable
violation (quote the offending value and say exactly which rule it breaks). Do NOT reject on
speculation, hedging, or vague doubt ("possible", "may have", "risks", "insufficient clarity",
"lacks explicit evidence" are NOT valid reasons). If you are not sure a row is wrong, it is VALID.{citation_guidance_block}

Reject a row ONLY for these concrete problems:
{_citation_reject_bullets(include_citations)}
- A field value is clearly copied from the wrong place (neighboring-row contamination) or is
  document noise (headers, footers, page numbers, N.B. notes, nav links, layout labels).
- grade_level is not exactly one of: Elementary School, Middle School, High School.
- A value plainly contradicts the sample CSV contract's stated rules.

{_citation_do_not_reject_clause(include_citations)}

Sample CSV transformation contract:
{sample_contract_json}

Extracted rows to audit (each has a row_index):
{rows_json}

Relevant source excerpts (the regions of the document where these rows' content appears):
{doc_slice}
""".strip()

        return [
            {
                "role": "system",
                "content": (
                    "You are a data extraction validator. Your goal is to pass every row that is a "
                    "faithful, contract-consistent transformation of the source, and to reject only "
                    "rows with a concrete, demonstrable defect you can point to. Transformation of "
                    "source text into contract values is expected and correct, not a defect. Judge "
                    "each row independently and return one verdict per row. When in doubt, return "
                    "VALID. Return your response as a single JSON object."
                ),
            },
            {"role": "user", "content": prompt},
        ]

    def _audit_batch_with_model(
        self, rows: list[BaseModel], parsed_document: ParsedDocument
    ) -> SemanticBatchAuditVerdict:
        messages = self._build_batch_audit_messages(rows, parsed_document)
        if self.settings.portkey_api_key:
            return self._call_portkey_batch(messages)
        provider = self.settings.critic_provider
        if provider == "openai":
            return self._call_openai_batch(messages)
        if provider == "anthropic":
            return self._call_anthropic_batch(messages)
        raise CriticValidationError(f"Unsupported critic provider: {provider}")

    def _call_openai_batch(self, messages: list[dict[str, str]]) -> SemanticBatchAuditVerdict:
        import instructor
        from openai import OpenAI

        client = instructor.from_openai(OpenAI(api_key=self.settings.openai_api_key))
        return client.chat.completions.create(
            model=self.settings.critic_model,
            response_model=SemanticBatchAuditVerdict,
            messages=messages,
            max_retries=0,
        )

    def _call_anthropic_batch(self, messages: list[dict[str, str]]) -> SemanticBatchAuditVerdict:
        import instructor
        from anthropic import Anthropic

        client = instructor.from_anthropic(
            Anthropic(api_key=self.settings.anthropic_api_key),
            mode=instructor.Mode.ANTHROPIC_JSON,
        )
        return client.messages.create(
            model=self.settings.critic_model,
            response_model=SemanticBatchAuditVerdict,
            messages=messages,
            max_retries=0,
        )

    def _call_portkey_batch(self, messages: list[dict[str, str]]) -> SemanticBatchAuditVerdict:
        from portkey_client import call_portkey_structured

        provider = self.settings.portkey_critic_provider or self._infer_portkey_critic_provider()
        return call_portkey_structured(
            api_key=self.settings.portkey_api_key,
            provider=provider,
            model=self.settings.critic_model,
            response_model=SemanticBatchAuditVerdict,
            messages=messages,
            max_concurrency=self.settings.llm_max_concurrency,
            fallbacks=self.settings.critic_fallbacks,
            timeout=self.settings.request_timeout_seconds,
        )

    def _run_contract_preflight(self, row: BaseModel) -> None:
        schema_config = load_schema_config(settings=self.settings)
        contract = schema_config.sample_contract
        if contract is None:
            return

        data = row.model_dump()
        issues: list[str] = []
        output_to_internal = {
            (spec.output_column or spec.name): spec.name
            for spec in schema_config.fields
        }
        description = str(data.get("description") or "")
        description_citation = str(data.get("description_source_citation") or "")

        for output_column in contract.required_columns:
            internal_name = output_to_internal.get(output_column)
            if not internal_name:
                continue
            value = data.get(internal_name)
            if value is None or (isinstance(value, str) and not value.strip()):
                issues.append(f"Required field '{output_column}' is blank.")

        for key, value in data.items():
            if not isinstance(value, str) or not value.strip():
                continue
            if self._contains_noise_artifact(value):
                issues.append(f"Field '{key}' appears contaminated by layout noise or extraction artifacts.")

        if description and self.settings.extraction_citations_enabled:
            if self._looks_truncated(description, description_citation):
                issues.append("Description appears truncated relative to its supporting citation.")
            if self._loses_multiline_structure(description, description_citation, contract.description_multiline_style):
                issues.append("Description appears to flatten multiline structure that should be preserved.")
            if self._loses_special_notation(description, description_citation):
                issues.append("Description appears to lose symbols, notation, or multilingual characters present in the source citation.")

        grade_level_field = output_to_internal.get("grade_level")
        if grade_level_field:
            grade_level_value = data.get(grade_level_field)
            if isinstance(grade_level_value, str) and grade_level_value.strip():
                if grade_level_value.strip() not in ALLOWED_GRADE_LEVELS:
                    issues.append(
                        f"grade_level must be one of {', '.join(ALLOWED_GRADE_LEVELS)}; got '{grade_level_value.strip()}'."
                    )

        if issues:
            raise CriticValidationError(" | ".join(dict.fromkeys(issues)))

    def _audit_with_openai(self, row: BaseModel, parsed_document: ParsedDocument) -> SemanticAuditVerdict:
        if not self.settings.openai_api_key:
            raise CriticValidationError("OPENAI_API_KEY is not configured for critic validation.")

        import instructor
        from openai import OpenAI

        client = instructor.from_openai(OpenAI(api_key=self.settings.openai_api_key))
        return client.chat.completions.create(
            model=self.settings.critic_model,
            response_model=SemanticAuditVerdict,
            messages=self._build_audit_messages(row, parsed_document),
            max_retries=0,
        )

    def _audit_with_anthropic(self, row: BaseModel, parsed_document: ParsedDocument) -> SemanticAuditVerdict:
        if not self.settings.anthropic_api_key:
            raise CriticValidationError("ANTHROPIC_API_KEY is not configured for critic validation.")

        import instructor
        from anthropic import Anthropic

        client = instructor.from_anthropic(
            Anthropic(api_key=self.settings.anthropic_api_key),
            mode=instructor.Mode.ANTHROPIC_JSON,
        )
        return client.messages.create(
            model=self.settings.critic_model,
            response_model=SemanticAuditVerdict,
            messages=self._build_audit_messages(row, parsed_document),
            max_retries=0,
        )

    def _audit_with_portkey(self, row: BaseModel, parsed_document: ParsedDocument) -> SemanticAuditVerdict:
        if not self.settings.portkey_api_key:
            raise CriticValidationError("PORTKEY_API_KEY is not configured for critic validation.")

        from portkey_client import call_portkey_structured

        provider = self.settings.portkey_critic_provider or self._infer_portkey_critic_provider()
        try:
            return call_portkey_structured(
                api_key=self.settings.portkey_api_key,
                provider=provider,
                model=self.settings.critic_model,
                response_model=SemanticAuditVerdict,
                messages=self._build_audit_messages(row, parsed_document),
                max_concurrency=self.settings.llm_max_concurrency,
                fallbacks=self.settings.critic_fallbacks,
                timeout=self.settings.request_timeout_seconds,
            )
        except Exception as exc:
            LOGGER.exception(
                "Portkey critic call failed. provider=%s model=%s error_type=%s error=%r",
                provider,
                self.settings.critic_model,
                type(exc).__name__,
                exc,
            )
            raise CriticValidationError(
                f"Portkey critic failed ({type(exc).__name__}): {exc}"
            ) from exc

    def _infer_portkey_critic_provider(self) -> str:
        # Inference: these provider ids mirror the common Portkey provider names used in their examples.
        provider_map = {
            "openai": "@openai",
            "anthropic": "@anthropic",
        }
        return provider_map.get(self.settings.critic_provider, "@openai")

    @staticmethod
    def _contains_noise_artifact(text: str) -> bool:
        normalized = text.strip()
        noise_patterns = [
            r"\bN\.B\.\b",
            r"\bpage\s+\d+\b",
            r"^\s*\d+\s*$",
            r"^\s*continued\b",
            r"^\s*appendix\b",
            r"^\s*footer\b",
            r"^\s*header\b",
        ]
        return any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in noise_patterns)

    @staticmethod
    def _looks_truncated(value: str, citation: str) -> bool:
        stripped = value.rstrip()
        if not stripped:
            return False
        if re.search(r"[,;/(\-\u2013\u2014]$", stripped):
            return True
        if citation:
            compact_value = " ".join(stripped.split())
            compact_citation = " ".join(citation.strip().split())
            if compact_citation.startswith(compact_value) and len(compact_citation) > len(compact_value) + 24:
                return True
        return False

    @staticmethod
    def _loses_multiline_structure(value: str, citation: str, multiline_style: str) -> bool:
        if "preserves multiline" not in multiline_style.lower() and "split-line" not in multiline_style.lower():
            return False
        return "\n" in citation and "\n" not in value

    @staticmethod
    def _loses_special_notation(value: str, citation: str) -> bool:
        if not citation:
            return False
        special_chars = {
            char
            for char in citation
            if (
                ord(char) > 127
                or char in "±×÷√∑∫∞≈≠≤≥→←↔αβγδεζηθικλμνξοπρστυφχψωΑΒΓΔΘΛΞΠΣΦΨΩ"
            )
        }
        return bool(special_chars) and not special_chars.issubset(set(value))
