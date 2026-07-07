from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from extractor import _normalize_to_string_list
from config import Settings, get_settings
from parsers.base import ParsedDocument
from schemas import ALLOWED_GRADE_LEVELS, critic_row_view, get_output_column_name, load_schema_config


class CriticValidationError(RuntimeError):
    pass


LOGGER = logging.getLogger(__name__)


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
        row_json = json.dumps(critic_row_view(row, schema_config), indent=2)
        sample_contract_json = (
            json.dumps(schema_config.sample_contract.model_dump(mode="json"), indent=2)
            if schema_config.sample_contract
            else "null"
        )
        verbatim_columns = [
            get_output_column_name(spec) for spec in schema_config.fields if spec.requires_citation
        ]
        derived_columns = [
            get_output_column_name(spec) for spec in schema_config.fields if not spec.requires_citation
        ]
        verbatim_list = ", ".join(verbatim_columns) or "(none)"
        derived_list = ", ".join(derived_columns) or "(none)"
        prompt = f"""
Audit this extracted row against the original document and the approved sample CSV contract.

Default to VALID. This pipeline's job is to TRANSFORM messy source text into clean contract
values, so transformation, mapping, inheritance, and synthesis are EXPECTED and correct — not
suspicious. Return tag=INVALID only when you can point to a CONCRETE, demonstrable violation
(quote the offending value and say exactly which rule it breaks). Do NOT reject on speculation,
hedging, or vague doubt ("possible", "may have", "risks", "insufficient clarity", "lacks
explicit evidence" are NOT valid reasons). If you are not sure a row is wrong, it is VALID.

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

Reject ONLY for these concrete problems:
- A verbatim-cited field's cited text is not present in the source at all.
- A field value is clearly copied from the wrong place (neighboring-row contamination) or is
  document noise (headers, footers, page numbers, N.B. notes, nav links, layout labels).
- A description is truncated mid-sentence or drops required sub-parts that the citation shows.
- Loss/garbling of mathematical, chemistry, Greek, notation, or multilingual characters that the
  source clearly contains.
- grade_level is not exactly one of: Elementary School, Middle School, High School.
- A value plainly contradicts the sample CSV contract's stated rules.

Do NOT reject for: an empty citation on a derived field, a formatting-only citation difference,
a prefixed/synthesized display code, a canonical source URL not printed in the body, a null
optional field, or duplicate display codes (uniqueness is resolved later by the pipeline).

Sample CSV transformation contract:
{sample_contract_json}

Extracted row:
{row_json}

Original document markdown:
{parsed_document.markdown}
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

        if description:
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
