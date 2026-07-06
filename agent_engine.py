from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, create_model, field_validator

from config import Settings, get_settings
from parsers.base import ParsedDocument
from schemas import get_extraction_payload_model, load_schema_config


class GeminiClientError(RuntimeError):
    pass


LOGGER = logging.getLogger(__name__)


class PreExtractionUnderstanding(BaseModel):
    layout_analysis: list[str] = Field(
        default_factory=list,
        description="Concise notes describing document structure and likely row boundaries.",
    )
    row_formation_logic: list[str] = Field(
        default_factory=list,
        description="How one complete output row is formed from this source and sample contract.",
    )
    column_derivations: dict[str, str] = Field(
        default_factory=dict,
        description="Per-column explanation of what content belongs there and how it is derived.",
    )
    representative_row: dict[str, str] = Field(
        default_factory=dict,
        description="A row-shaped preview showing what one complete row would contain before final cited extraction.",
    )
    exclusion_rules: list[str] = Field(
        default_factory=list,
        description="Source-specific content that must be excluded from output rows.",
    )
    coverage_expectations: list[str] = Field(
        default_factory=list,
        description="Notes about what domains, topics, and row items must be covered so source content is not missed.",
    )

    @field_validator(
        "layout_analysis",
        "row_formation_logic",
        "exclusion_rules",
        "coverage_expectations",
        mode="before",
    )
    @classmethod
    def _normalize_string_list_fields(cls, value: Any) -> list[str]:
        return _normalize_to_string_list(value)

    @field_validator("column_derivations", mode="before")
    @classmethod
    def _normalize_column_derivations(cls, value: Any) -> dict[str, str]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return {str(key): _stringify_structured_value(item) for key, item in value.items()}
        if isinstance(value, list):
            normalized: dict[str, str] = {}
            for index, item in enumerate(value, start=1):
                normalized[f"field_{index}"] = _stringify_structured_value(item)
            return normalized
        if isinstance(value, str):
            return {"notes": value.strip()}
        return {"notes": _stringify_structured_value(value)}

    @field_validator("representative_row", mode="before")
    @classmethod
    def _normalize_representative_row(cls, value: Any) -> dict[str, str]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return {str(key): _stringify_structured_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return {f"field_{index}": _stringify_structured_value(item) for index, item in enumerate(value, start=1)}
        if isinstance(value, str):
            return {"notes": value.strip()}
        return {"notes": _stringify_structured_value(value)}


def _normalize_to_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, dict):
        normalized_items: list[str] = []
        for key, item in value.items():
            rendered = _stringify_structured_value(item)
            normalized_items.append(f"{key}: {rendered}" if rendered else str(key))
        return [item for item in normalized_items if item.strip()]
    if isinstance(value, list):
        normalized_items: list[str] = []
        for item in value:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    normalized_items.append(text)
                continue
            if isinstance(item, dict):
                normalized_items.extend(_normalize_to_string_list(item))
                continue
            text = _stringify_structured_value(item)
            if text:
                normalized_items.append(text)
        return normalized_items
    text = _stringify_structured_value(value)
    return [text] if text else []


def _stringify_structured_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        parts = [f"{key}: {_stringify_structured_value(item)}" for key, item in value.items()]
        return "; ".join(part for part in parts if part.strip())
    if isinstance(value, list):
        parts = [_stringify_structured_value(item) for item in value]
        return "; ".join(part for part in parts if part.strip())
    return str(value).strip()


def _strip_json_code_fence(content: str) -> str:
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1).strip()
    if text.startswith("{") or text.startswith("["):
        return text
    first_object = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
    if first_object:
        return first_object.group(1).strip()
    return text


def _extract_message_text(response: Any) -> str:
    if isinstance(response, dict):
        choices = response.get("choices") or []
    else:
        choices = getattr(response, "choices", []) or []

    if not choices:
        raise GeminiClientError("Model response did not include any choices.")

    first_choice = choices[0]
    message = first_choice.get("message") if isinstance(first_choice, dict) else getattr(first_choice, "message", None)
    if message is None:
        raise GeminiClientError("Model response did not include a message payload.")

    content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_chunks: list[str] = []
        for item in content:
            if isinstance(item, str):
                text_chunks.append(item)
                continue
            if isinstance(item, dict):
                if item.get("type") == "text" and item.get("text"):
                    text_chunks.append(item["text"])
                elif item.get("text"):
                    text_chunks.append(str(item["text"]))
                continue
            item_text = getattr(item, "text", None)
            if item_text:
                text_chunks.append(str(item_text))
        rendered = "".join(text_chunks).strip()
        if rendered:
            return rendered

    raise GeminiClientError("Model response did not contain parsable text content.")


@dataclass(slots=True)
class ExtractionAttempt:
    payload_rows: list[BaseModel]
    planning: PreExtractionUnderstanding
    layout_analysis: list[str]
    anchoring_plan: dict[str, str]


class ExtractionEngine:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def extract(self, parsed_document: ParsedDocument, prior_error_log: str | None = None) -> ExtractionAttempt:
        schema_path = str(self.settings.schema_config_path)
        payload_model = get_extraction_payload_model(schema_path)
        planning = self._analyze_document(parsed_document, prior_error_log)
        envelope_model = create_model(
            "ExtractionEnvelope",
            __base__=BaseModel,
            __module__=__name__,
            anchoring_plan=(
                dict[str, str],
                Field(description="Field-to-layout anchor map used for this one extraction."),
            ),
            payload_rows=(
                list[payload_model],
                Field(description="All final structured extraction rows with verbatim citations, in source order."),
            ),
        )

        messages = self._build_extraction_messages(parsed_document, planning, prior_error_log)
        response = self._call_extractor(envelope_model, messages)
        return ExtractionAttempt(
            planning=planning,
            payload_rows=response.payload_rows,
            layout_analysis=planning.layout_analysis,
            anchoring_plan=response.anchoring_plan,
        )

    def _analyze_document(
        self,
        parsed_document: ParsedDocument,
        prior_error_log: str | None,
    ) -> PreExtractionUnderstanding:
        messages = self._build_analysis_messages(parsed_document, prior_error_log)
        return self._call_extractor(PreExtractionUnderstanding, messages)

    def _build_analysis_messages(
        self,
        parsed_document: ParsedDocument,
        prior_error_log: str | None,
    ) -> list[dict[str, str]]:
        schema_config = load_schema_config(settings=self.settings)
        schema_json = json.dumps(schema_config.model_dump(mode="json"), indent=2)
        sample_contract_json = (
            json.dumps(schema_config.sample_contract.model_dump(mode="json"), indent=2)
            if schema_config.sample_contract
            else "null"
        )
        correction_block = (
            "No prior validation failures.\n"
            if not prior_error_log
            else f"Previous validation failure log. Correct these exact issues:\n{prior_error_log}\n"
        )

        system_prompt = (
            "You are an adaptive data transformation agent. "
            "Do not use static assumptions about layout. "
            "Before extraction, inspect the unique source structure together with the sample CSV contract. "
            "This step is only for understanding how rows are formed and how each column is derived. "
            "Do not hardcode subject-specific assumptions. "
            "Infer the meaning of each output column for this subject from the approved sample contract first, then from the source structure."
        )

        user_prompt = f"""
Schema to populate:
{schema_json}

Sample CSV transformation contract:
{sample_contract_json}

Document metadata:
- source_name: {parsed_document.source_name}
- source_type: {parsed_document.source_type}
- source_path: {parsed_document.source_path}

Validation feedback:
{correction_block}

Instructions:
1. Analyze this document's unique structure and summarize it in layout_analysis.
2. First infer what each output column means for this subject under the approved sample contract. Do not assume the same subject/domain/topic/grade pattern used by a different subject.
3. Explain in row_formation_logic how one complete output row is formed from this source and how that row pattern repeats across the source.
4. For each schema field, explain in column_derivations what data it should contain and how it is derived from the source.
5. Determine whether values such as source, subject, domain, topic, grade_level, display_grade, and grade_number are document-level, section-level, or row-level for this specific subject and source.
6. If the approved sample implies canonical public source links, merged topic paths, row-specific stage labels, or transformed display codes, note that explicitly when supported by the source.
7. Build representative_row as a row-shaped preview showing what one complete row would contain under this sample contract.
8. List exclusion_rules describing what source content must be rejected from output rows.
9. In coverage_expectations, identify what domains, topics, sections, or repeated row items must be captured so valid source content is not missed.
10. Do not perform final cited extraction yet. This step is only for understanding the source-to-row mapping.

Source markdown:
{parsed_document.markdown}
""".strip()

        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    def _build_extraction_messages(
        self,
        parsed_document: ParsedDocument,
        planning: PreExtractionUnderstanding,
        prior_error_log: str | None,
    ) -> list[dict[str, str]]:
        schema_config = load_schema_config(settings=self.settings)
        schema_json = json.dumps(schema_config.model_dump(mode="json"), indent=2)
        sample_contract_json = (
            json.dumps(schema_config.sample_contract.model_dump(mode="json"), indent=2)
            if schema_config.sample_contract
            else "null"
        )
        planning_json = json.dumps(planning.model_dump(mode="json"), indent=2)
        correction_block = (
            "No prior validation failures.\n"
            if not prior_error_log
            else f"Previous validation failure log. Correct these exact issues:\n{prior_error_log}\n"
        )

        system_prompt = (
            "You are an adaptive data transformation agent. "
            "Do not use static assumptions about layout. "
            "Use the pre-extraction understanding artifact as the required plan before mapping content into the provided schema. "
            "The provided sample CSV contract defines the transformation rules and output style; follow it strictly. "
            "Infer subject-specific column meaning from the sample contract and source rather than hardcoding one hierarchy interpretation across subjects. "
            "Description fields must preserve source meaning completely, without truncation, cross-row merges, "
            "neighbor contamination, or forced flattening when multiline structure is meaningful. "
            "Preserve mathematical symbols, equations, radicals, superscripts, subscripts, chemistry notation, "
            "Greek letters, domain-specific notation, semantic punctuation, and multilingual text faithfully. "
            "If notation looks degraded in native extraction, attempt a localized repair only when supported by the source. "
            "Reject noise such as headers, footers, page numbers, continuation fragments, appendix-only noise, N.B. notes, "
            "layout labels, and extraction artifacts unless the contract explicitly requires them. "
            "Citations must be verbatim snippets copied from the source markdown. "
            "If a value is absent, return null for the value and an empty string for its citation. "
            "Keep anchoring_plan short and field-specific. "
            "Extract every valid row supported by the source and approved sample contract. "
            "If the approved sample supports merged topic cells and one standard genuinely applies to multiple topics, merge those topic names into one topic cell using ' | ' rather than duplicating the row only for topic labels. "
            "If the approved sample supports prefixed or formed display codes and the same raw display code repeats, prefix it with a short domain code or topic code plus a dot when that disambiguation is supported by the source structure. "
            "Do not stop after the first row. "
            "Do not omit valid domains, topics, or descriptions that belong in output. "
            "Return payload_rows in source order with no duplicates and no missing supported rows."
        )

        user_prompt = f"""
Schema to populate:
{schema_json}

Sample CSV transformation contract:
{sample_contract_json}

Pre-extraction understanding artifact:
{planning_json}

Document metadata:
- source_name: {parsed_document.source_name}
- source_type: {parsed_document.source_type}
- source_path: {parsed_document.source_path}

Validation feedback:
{correction_block}

Instructions:
1. Use the pre-extraction understanding artifact before extracting any values.
2. Build a temporary field anchoring plan explaining where each schema field appears in this specific layout.
3. Apply the sample-derived meaning of each column consistently across all rows. Once you infer what source, subject, domain, topic, grade_level, display_grade, and grade_number mean for this subject, do not drift to a different interpretation.
4. Extract every valid output row from the source, not just one row.
5. Ensure complete source coverage for all valid domains, topics, and descriptions that match the sample contract.
6. Return payload_rows in source reading order with one object per output row.
7. If the approved sample supports canonical public source links, use those links when they are identifiable from the source documents or staging context instead of local file names.
8. If the approved sample supports merged topic cells and one standard genuinely spans multiple topics, merge those topic names into one topic field using ` | ` rather than duplicating the row only for topic labels.
9. If the approved sample supports prefixed or formed display codes and the same raw display standard code repeats, disambiguate it with a short domain code or topic code prefix and a dot when that is needed to keep `Display standard code` unique.
10. If the source contradicts the draft representative_row, follow the source while preserving the sample contract.

Source markdown:
{parsed_document.markdown}
""".strip()

        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    def _call_extractor(self, response_model: type[BaseModel], messages: list[dict[str, str]]) -> BaseModel:
        if self.settings.portkey_api_key:
            return self._call_portkey_json(response_model, messages)
        return self._call_gemini_instructor(response_model, messages)

    def _call_portkey_json(self, response_model: type[BaseModel], messages: list[dict[str, str]]) -> BaseModel:
        if not self.settings.portkey_api_key:
            raise GeminiClientError("PORTKEY_API_KEY is not configured.")

        try:
            from portkey_ai import Portkey
        except ImportError as exc:  # pragma: no cover - optional integration
            raise GeminiClientError(
                "Portkey support requires the 'portkey-ai' package. Install it to use PORTKEY_API_KEY mode."
            ) from exc

        client = Portkey(
            api_key=self.settings.portkey_api_key,
            provider=self.settings.portkey_extractor_provider,
        )
        try:
            response = client.chat.completions.create(
                model=self.settings.extractor_model,
                messages=messages,
                temperature=0,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            LOGGER.exception(
                "Portkey extractor call failed. provider=%s model=%s error_type=%s error=%r",
                self.settings.portkey_extractor_provider,
                self.settings.extractor_model,
                type(exc).__name__,
                exc,
            )
            raise GeminiClientError(
                f"Portkey extractor failed ({type(exc).__name__}): {exc}"
            ) from exc
        content = _extract_message_text(response)
        try:
            payload = json.loads(_strip_json_code_fence(content))
        except json.JSONDecodeError as exc:
            raise GeminiClientError(f"Portkey extractor returned invalid JSON: {exc}") from exc
        return response_model.model_validate(payload)

    def _call_gemini_instructor(self, response_model: type[BaseModel], messages: list[dict[str, str]]) -> BaseModel:
        if not self.settings.gemini_api_key:
            raise GeminiClientError("GEMINI_API_KEY is not configured.")

        import instructor
        from google import genai

        raw_client = genai.Client(api_key=self.settings.gemini_api_key)
        wrapper_errors: list[str] = []
        client = None

        wrapper_candidates = [
            lambda: instructor.from_genai(raw_client, mode=instructor.Mode.GEMINI_JSON),
            lambda: instructor.from_gemini(raw_client, mode=instructor.Mode.GEMINI_JSON),
        ]

        for build_client in wrapper_candidates:
            try:
                client = build_client()
                break
            except Exception as exc:  # pragma: no cover - version-compatibility shim
                wrapper_errors.append(repr(exc))

        if client is None:
            raise GeminiClientError(
                "Unable to initialize Instructor Gemini client. "
                + " | ".join(wrapper_errors)
            )

        call_errors: list[str] = []
        call_candidates = [
            lambda: client.chat.completions.create(
                model=self.settings.extractor_model,
                response_model=response_model,
                messages=messages,
                max_retries=0,
            ),
            lambda: client.messages.create(
                model=self.settings.extractor_model,
                response_model=response_model,
                messages=messages,
                max_retries=0,
            ),
            lambda: client.create(
                model=self.settings.extractor_model,
                response_model=response_model,
                messages=messages,
                max_retries=0,
            ),
        ]

        for invoke in call_candidates:
            try:
                return invoke()
            except Exception as exc:  # pragma: no cover - version-compatibility shim
                call_errors.append(repr(exc))

        raise GeminiClientError(
            "Instructor Gemini call failed across all known call styles. "
            + " | ".join(call_errors)
        )
