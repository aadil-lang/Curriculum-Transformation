from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, Field, create_model

from extractor import PreExtractionUnderstanding
from config import Settings, get_settings
from parsers.base import ParsedDocument
from schemas import PIPELINE_METADATA_FIELDS, get_extraction_payload_model, load_schema_config, normalize_grade_level, schema_only_row_view


LOGGER = logging.getLogger(__name__)


class TransformationReviewError(RuntimeError):
    pass


class TransformationReviewMetadata(BaseModel):
    was_modified: bool = Field(description="Whether the evaluator changed any output values.")
    issues_found: list[str] = Field(default_factory=list, description="Problems detected in the extracted transformation.")
    fixes_applied: list[str] = Field(default_factory=list, description="Minimal corrections applied before final validation.")
    confidence_notes: str = Field(default="", description="Short explanation of the review decision.")


class TransformationReviewer:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def review_and_fix(
        self,
        row: BaseModel,
        parsed_document: ParsedDocument,
        planning: PreExtractionUnderstanding,
    ) -> tuple[BaseModel, TransformationReviewMetadata]:
        row_model = row.__class__
        programmatic_row, programmatic_fixes = self._apply_programmatic_fixes(row)
        reviewed_row, metadata = self._review_with_model(programmatic_row, parsed_document, planning)

        merged_fixes = [*programmatic_fixes, *metadata.fixes_applied]
        metadata = TransformationReviewMetadata(
            was_modified=bool(programmatic_fixes) or metadata.was_modified,
            issues_found=metadata.issues_found,
            fixes_applied=merged_fixes,
            confidence_notes=metadata.confidence_notes,
        )
        return row_model.model_validate(reviewed_row.model_dump()), metadata

    def _apply_programmatic_fixes(self, row: BaseModel) -> tuple[BaseModel, list[str]]:
        row_model = row.__class__
        data = row.model_dump()
        fixes: list[str] = []
        schema_config = load_schema_config(settings=self.settings)
        output_to_internal = {
            (spec.output_column or spec.name).strip().lower(): spec.name
            for spec in schema_config.fields
        }

        for key, value in list(data.items()):
            if not isinstance(value, str):
                continue
            normalized = value.strip()
            if normalized != value:
                data[key] = normalized
                fixes.append(f"Trimmed surrounding whitespace in '{key}'.")

        grade_number_field = output_to_internal.get("grade_number")
        if grade_number_field:
            grade_number_value = data.get(grade_number_field)
            normalized_grade_numbers = _normalize_grade_number_value(grade_number_value)
            if normalized_grade_numbers and normalized_grade_numbers != grade_number_value:
                data[grade_number_field] = normalized_grade_numbers
                fixes.append(
                    f"Normalized grade_number '{grade_number_value}' to '{normalized_grade_numbers}'."
                )

        grade_level_field = output_to_internal.get("grade_level")
        if grade_level_field:
            grade_level_value = data.get(grade_level_field)
            normalized_grade_level = normalize_grade_level(grade_level_value)
            if normalized_grade_level and normalized_grade_level != grade_level_value:
                data[grade_level_field] = normalized_grade_level
                fixes.append(
                    f"Normalized grade_level '{grade_level_value}' to '{normalized_grade_level}'."
                )

        topic_field = output_to_internal.get("topic")
        if topic_field:
            topic_value = data.get(topic_field)
            normalized_topic_value = _normalize_pipe_joined_topic(topic_value)
            if normalized_topic_value and normalized_topic_value != topic_value:
                data[topic_field] = normalized_topic_value
                fixes.append(
                    f"Normalized topic '{topic_value}' to '{normalized_topic_value}'."
                )

        return row_model.model_validate(data), fixes

    def _review_with_model(
        self,
        row: BaseModel,
        parsed_document: ParsedDocument,
        planning: PreExtractionUnderstanding,
    ) -> tuple[BaseModel, TransformationReviewMetadata]:
        row_model = row.__class__
        payload_model = get_extraction_payload_model(str(self.settings.schema_config_path))
        response_model = create_model(
            "TransformationReviewEnvelope",
            __base__=BaseModel,
            __module__=__name__,
            review=(TransformationReviewMetadata, Field(description="Review findings and applied fixes.")),
            corrected_row=(payload_model, Field(description="Corrected row after review.")),
        )

        messages = self._build_review_messages(row, parsed_document, planning)
        response = self._call_review_model(response_model, messages)

        # The model only sees and returns schema fields; merge back the pipeline
        # metadata from the original row so the full CsvRow can be reconstructed.
        metadata = {field: getattr(row, field) for field in PIPELINE_METADATA_FIELDS}
        corrected_row = row_model.model_validate({**metadata, **response.corrected_row.model_dump()})
        return corrected_row, response.review

    def _build_review_messages(
        self,
        row: BaseModel,
        parsed_document: ParsedDocument,
        planning: PreExtractionUnderstanding,
    ) -> list[dict[str, str]]:
        schema_config = load_schema_config(settings=self.settings)
        schema_json = json.dumps(schema_config.model_dump(mode="json"), indent=2)
        sample_contract_json = (
            json.dumps(schema_config.sample_contract.model_dump(mode="json"), indent=2)
            if schema_config.sample_contract
            else "null"
        )
        planning_json = json.dumps(planning.model_dump(mode="json"), indent=2)
        row_json = json.dumps(schema_only_row_view(row), indent=2)

        system_prompt = (
            "You are a transformation evaluator and fixer. "
            "Review extracted CSV rows against the source document, the sample CSV contract, and the row-formation plan. "
            "Apply only minimal supported fixes. "
            "Do not invent missing facts. "
            "Preserve citations, symbols, multilingual content, multiline structure, and row boundaries. "
            "Do not hardcode one hierarchy interpretation across subjects; preserve the subject-specific column meanings inferred from the sample contract and source. "
            "grade_level must be exactly one of: Elementary School, Middle School, High School. "
            "Return your response as a single JSON object."
        )

        user_prompt = f"""
Schema:
{schema_json}

Sample CSV transformation contract:
{sample_contract_json}

Pre-extraction understanding artifact:
{planning_json}

Current transformed row:
{row_json}

Review goals:
- check whether the transformation matches the source and the sample contract
- check whether subject, domain, topic, grade_level, display_grade, grade_number, and source follow the sample-derived column semantics for this subject rather than a generic cross-subject assumption
- normalize grade_level to exactly Elementary School, Middle School, or High School when the source supports that mapping
- fix incorrect placement, formatting, merged/split text issues, or minor transformation errors when the source supports a correction
- if the approved sample pattern implies a canonical public source link and the source supports identifying it, prefer that canonical link over a local staged file name
- if the approved sample pattern implies row-specific stage or learner-band values, do not collapse them into one document-wide grade label
- if grade_number is a numeric range such as `9-12`, normalize it to a comma-separated sequence such as `9,10,11,12`, with no spaces after commas
- if the approved sample supports merged topic cells and one standard genuinely applies to multiple topics, merge those topic names into one topic field using ` | ` rather than duplicating the row only for topic labels
- ensure `Display standard code` stays unique within the CSV; if the same raw code repeats across multiple domains or sections, add a short domain code prefix when the source structure supports that disambiguation
- if the approved sample supports prefixed or formed display codes and repeated rows share the same description and raw display code, use a short domain code or topic code prefix with a dot, whichever makes the display code unique
- preserve display logic such as synthetic or source-faithful display standard code when consistent with the sample contract
- preserve description completeness, notation, bullets/punctuation style, and multiline behavior required by the sample
- reject noise, neighboring-row contamination, and unsupported values

Output requirements:
- review.was_modified should be true only if you actually change the row
- review.issues_found should list all detected transformation problems
- review.fixes_applied should describe only the corrections you actually made
- corrected_row must be the final row to send to the final critic

Original document markdown:
{parsed_document.markdown}
""".strip()

        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    def _call_review_model(self, response_model: type[BaseModel], messages: list[dict[str, str]]) -> BaseModel:
        if self.settings.portkey_api_key:
            return self._call_portkey_json(response_model, messages)
        provider = self.settings.critic_provider
        if provider == "openai":
            return self._call_openai_json(response_model, messages)
        if provider == "anthropic":
            return self._call_anthropic_json(response_model, messages)
        raise TransformationReviewError(f"Unsupported review provider: {provider}")

    def _call_openai_json(self, response_model: type[BaseModel], messages: list[dict[str, str]]) -> BaseModel:
        if not self.settings.openai_api_key:
            raise TransformationReviewError("OPENAI_API_KEY is not configured for transformation review.")

        import instructor
        from openai import OpenAI

        client = instructor.from_openai(OpenAI(api_key=self.settings.openai_api_key))
        return client.chat.completions.create(
            model=self.settings.critic_model,
            response_model=response_model,
            messages=messages,
            max_retries=0,
        )

    def _call_anthropic_json(self, response_model: type[BaseModel], messages: list[dict[str, str]]) -> BaseModel:
        if not self.settings.anthropic_api_key:
            raise TransformationReviewError("ANTHROPIC_API_KEY is not configured for transformation review.")

        import instructor
        from anthropic import Anthropic

        client = instructor.from_anthropic(
            Anthropic(api_key=self.settings.anthropic_api_key),
            mode=instructor.Mode.ANTHROPIC_JSON,
        )
        return client.messages.create(
            model=self.settings.critic_model,
            response_model=response_model,
            messages=messages,
            max_retries=0,
        )

    def _call_portkey_json(self, response_model: type[BaseModel], messages: list[dict[str, str]]) -> BaseModel:
        if not self.settings.portkey_api_key:
            raise TransformationReviewError("PORTKEY_API_KEY is not configured for transformation review.")

        from portkey_client import call_portkey_structured

        provider = self.settings.portkey_critic_provider or "@openai"
        try:
            return call_portkey_structured(
                api_key=self.settings.portkey_api_key,
                provider=provider,
                model=self.settings.critic_model,
                response_model=response_model,
                messages=messages,
                max_concurrency=self.settings.llm_max_concurrency,
                fallbacks=self.settings.critic_fallbacks,
            )
        except Exception as exc:
            LOGGER.exception(
                "Portkey transformation review failed. provider=%s model=%s error_type=%s error=%r",
                provider,
                self.settings.critic_model,
                type(exc).__name__,
                exc,
            )
            raise TransformationReviewError(
                f"Portkey transformation review failed ({type(exc).__name__}): {exc}"
            ) from exc


def _normalize_grade_number_value(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    expanded_range = _expand_numeric_grade_range(normalized)
    if expanded_range:
        return expanded_range
    numeric_parts = [part.strip() for part in normalized.split(",")]
    if len(numeric_parts) > 1 and all(re.fullmatch(r"\d{1,2}", part) for part in numeric_parts):
        return ",".join(numeric_parts)
    return None


def _expand_numeric_grade_range(value: str) -> str | None:
    normalized = value.strip()
    match = re.fullmatch(r"(\d{1,2})\s*[-\u2013\u2014]\s*(\d{1,2})", normalized)
    if not match:
        return None
    start = int(match.group(1))
    end = int(match.group(2))
    if start > end or end - start > 20:
        return None
    return ",".join(str(number) for number in range(start, end + 1))


def _normalize_pipe_joined_topic(value: Any) -> str | None:
    if not isinstance(value, str) or "|" not in value:
        return None
    segments = [segment.strip() for segment in value.split("|") if segment.strip()]
    if len(segments) < 2:
        return segments[0] if segments else None
    deduped_segments: list[str] = []
    seen: set[str] = set()
    for segment in segments:
        normalized_key = segment.casefold()
        if normalized_key in seen:
            continue
        seen.add(normalized_key)
        deduped_segments.append(segment)
    return " | ".join(deduped_segments)
