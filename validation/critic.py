from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from agent_engine import _extract_message_text
from config import Settings, get_settings
from parsers.base import ParsedDocument
from schemas import load_schema_config


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
        row_json = json.dumps(row.model_dump(mode="json"), indent=2)
        schema_config = load_schema_config(settings=self.settings)
        sample_contract_json = (
            json.dumps(schema_config.sample_contract.model_dump(mode="json"), indent=2)
            if schema_config.sample_contract
            else "null"
        )
        prompt = f"""
Audit the extracted row against the original document markdown and the approved sample CSV contract.

Validation rules:
- Reject any field whose citation is not verbatim or does not support the field value.
- Reject fields that are semantically irrelevant to the document.
- Reject rows where important field meanings are mismatched.
- Reject rows whose transformed values or field placement violate the sample CSV contract.
- Reject any row whose `Display standard code` is duplicated within the same CSV context when that duplication is known programmatically.
- Accept `topic` values joined with ` | ` when one standard genuinely spans multiple topics, the source supports that merged topic cell, and the approved sample contract allows that topic style.
- Accept synthetic `Display standard code` prefixes such as `DA.1.1` when needed to keep the code unique and the approved sample contract allows transformed or prefixed display codes.
- Reject truncated descriptions, cross-row sentence merges, missing required sub-parts, neighboring-row contamination, and forced flattening when the sample style preserves multiline structure.
- Reject symbol loss or incorrect normalization for mathematical notation, chemistry notation, Greek letters, multilingual text, or semantic punctuation.
- Reject noise such as appendix-only out-of-scope items, N.B. notes, repeated headers, repeated footers, page numbers, continuation fragments, layout labels, and extraction artifacts.
- Accept null fields when the value truly is absent.
- Return tag=VALID only when the row is safe to append to the final CSV.
- Return tag=INVALID for every rejection.

Return is_valid=false with explicit issues if anything is unsupported.

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
                "content": "You are an adversarial data extraction critic. Be skeptical, precise, and strict about style-preserving transformations.",
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

        try:
            from portkey_ai import Portkey
        except ImportError as exc:  # pragma: no cover - optional integration
            raise CriticValidationError(
                "Portkey support requires the 'portkey-ai' package. Install it to use PORTKEY_API_KEY mode."
            ) from exc

        provider = self.settings.portkey_critic_provider or self._infer_portkey_critic_provider()
        client = Portkey(
            api_key=self.settings.portkey_api_key,
            provider=provider,
        )
        try:
            response = client.chat.completions.create(
                model=self.settings.critic_model,
                messages=self._build_audit_messages(row, parsed_document),
                temperature=0,
                response_format={"type": "json_object"},
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
        content = _extract_message_text(response)
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise CriticValidationError(f"Portkey critic returned invalid JSON: {exc}") from exc
        return SemanticAuditVerdict.model_validate(payload)

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
