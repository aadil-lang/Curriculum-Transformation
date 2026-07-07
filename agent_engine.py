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


class ExtractionOutputTooLargeError(GeminiClientError):
    """The model truncated its response because the output hit the token cap.

    Signals the chunking layer to split the input further and retry.
    """


LOGGER = logging.getLogger(__name__)


class ExtractionRegion(BaseModel):
    start_anchor: str = Field(
        default="",
        description="Verbatim text marking where extractable content STARTS (a '# Page N' marker for PDFs, or a heading line for docs). Empty if the whole document should be used.",
    )
    end_anchor: str = Field(
        default="",
        description="Verbatim text marking where extractable content ENDS. Empty means continue to the end of the document.",
    )
    skip_anchors: list[str] = Field(
        default_factory=list,
        description="Verbatim heading/marker texts between start and end whose sections must be skipped (e.g. an embedded contents table).",
    )
    confidence: str = Field(
        default="low",
        description="Confidence in these boundaries: one of high, medium, low.",
    )
    notes: str = Field(default="", description="Short explanation of the chosen boundaries.")

    @field_validator("skip_anchors", mode="before")
    @classmethod
    def _normalize_skip_anchors(cls, value: Any) -> list[str]:
        return _normalize_to_string_list(value)


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


def _document_outline(markdown: str, max_chars: int = 12000) -> str:
    """The heading lines and page markers of a document, for the locate pass.

    Feeding only the structure (not the full text) keeps the locate call cheap and
    avoids its own overflow on large documents.
    """
    lines = [
        line.rstrip()
        for line in markdown.splitlines()
        if line.lstrip().startswith("#")
    ]
    outline = "\n".join(lines)
    if len(outline) <= max_chars:
        return outline
    return outline[:max_chars]


def _apply_region(markdown: str, region: "ExtractionRegion") -> str | None:
    """Slice markdown to the region's [start_anchor, end_anchor] with skip sections removed.

    Returns None to signal "use the full document" whenever the boundary is missing,
    low-confidence, or the result is implausibly small — coverage is never sacrificed to
    a bad boundary.
    """
    if region.confidence.strip().lower() == "low":
        return None
    start = region.start_anchor.strip()
    if not start:
        return None
    start_idx = markdown.find(start)
    if start_idx == -1:
        return None

    end = region.end_anchor.strip()
    if end:
        end_idx = markdown.find(end, start_idx + len(start))
        bounded = markdown[start_idx:end_idx] if end_idx != -1 else markdown[start_idx:]
    else:
        bounded = markdown[start_idx:]

    # Remove each skip section: from the skip anchor to the next line that starts with
    # '#' (next heading/page) or end of the bounded text.
    for skip in region.skip_anchors:
        skip = skip.strip()
        if not skip:
            continue
        s_idx = bounded.find(skip)
        if s_idx == -1:
            continue
        newline = bounded.find("\n", s_idx)
        next_heading = -1
        search_from = newline if newline != -1 else s_idx + len(skip)
        for candidate_line_start in _iter_line_starts(bounded, search_from):
            if bounded[candidate_line_start:].lstrip().startswith("#"):
                next_heading = candidate_line_start
                break
        cut_end = next_heading if next_heading != -1 else len(bounded)
        bounded = bounded[:s_idx] + bounded[cut_end:]

    # Guard against detection errors that bound to a trivial fragment. A high-confidence
    # region is trusted even when small (outcomes are often a small slice of a large doc,
    # which is exactly the case region targeting exists for); otherwise require a modest floor.
    bounded_len = len(bounded.strip())
    if bounded_len < 1000:
        return None
    if region.confidence.strip().lower() != "high" and bounded_len < int(len(markdown) * 0.05):
        return None
    return bounded


def _iter_line_starts(text: str, from_index: int):
    idx = from_index
    length = len(text)
    while idx < length:
        yield idx
        nl = text.find("\n", idx)
        if nl == -1:
            return
        idx = nl + 1


def _is_output_truncation_error(exc: Exception) -> bool:
    markers = ("incompleteoutput", "max_tokens", "length limit", "finish_reason", "output is incomplete")
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in markers)


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

    def extract(
        self,
        parsed_document: ParsedDocument,
        prior_error_log: str | None = None,
        region_override: ExtractionRegion | None = None,
    ) -> ExtractionAttempt:
        schema_path = str(self.settings.schema_config_path)
        payload_model = get_extraction_payload_model(schema_path)

        parsed_document = self._apply_region_targeting(parsed_document, region_override)

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

        chunks = self._chunk_markdown(parsed_document.markdown)
        payload_rows: list[BaseModel] = []
        anchoring_plan: dict[str, str] = {}
        for chunk in chunks:
            rows, plan = self._extract_chunk_rows(
                parsed_document, planning, prior_error_log, envelope_model, chunk
            )
            payload_rows.extend(rows)
            anchoring_plan.update(plan)

        return ExtractionAttempt(
            planning=planning,
            payload_rows=payload_rows,
            layout_analysis=planning.layout_analysis,
            anchoring_plan=anchoring_plan,
        )

    def _apply_region_targeting(
        self,
        parsed_document: ParsedDocument,
        region_override: ExtractionRegion | None,
    ) -> ParsedDocument:
        if region_override is None and not self.settings.enable_region_targeting:
            return parsed_document

        region = region_override if region_override is not None else self._locate_extraction_region(parsed_document)
        if region is None:
            return parsed_document

        bounded = _apply_region(parsed_document.markdown, region)
        original_len = len(parsed_document.markdown)
        if bounded is None or not bounded.strip():
            LOGGER.info(
                "Region targeting: no reliable boundary for %s; using full document (%d chars).",
                parsed_document.source_name,
                original_len,
            )
            return parsed_document

        LOGGER.info(
            "Region targeting for %s: bounded %d -> %d chars (start=%r end=%r skip=%d, confidence=%s).",
            parsed_document.source_name,
            original_len,
            len(bounded),
            region.start_anchor[:60],
            region.end_anchor[:60],
            len(region.skip_anchors),
            region.confidence,
        )
        return parsed_document.model_copy(update={"markdown": bounded})

    def _locate_extraction_region(self, parsed_document: ParsedDocument) -> ExtractionRegion | None:
        outline = _document_outline(parsed_document.markdown)
        if not outline.strip():
            return None
        schema_config = load_schema_config(settings=self.settings)
        sample_contract_json = (
            json.dumps(schema_config.sample_contract.model_dump(mode="json"), indent=2)
            if schema_config.sample_contract
            else "null"
        )
        system_prompt = (
            "You locate the region of a curriculum document that contains the extractable rows "
            "(typically the syllabus outcomes/standards), so downstream extraction can skip "
            "front matter, rationale, assessment guidance, glossary, appendices, and sample work. "
            "Return anchors as VERBATIM text copied from the provided outline. For paginated "
            "sources use the '# Page N' markers; otherwise use heading lines. "
            "Return your response as a single JSON object."
        )
        user_prompt = f"""
The approved sample CSV contract describes what one extractable row looks like:
{sample_contract_json}

Below is the structural outline (headings and page markers) of the document '{parsed_document.source_name}'.
Identify:
- start_anchor: the verbatim outline line where extractable content begins.
- end_anchor: the verbatim outline line where extractable content ends (empty to run to the end).
- skip_anchors: verbatim outline lines between start and end whose sections must be skipped.
- confidence: high, medium, or low.

If you cannot confidently locate an outcomes region, return empty anchors with confidence=low.

Document outline:
{outline}
""".strip()
        try:
            return self._call_extractor(
                ExtractionRegion,
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
        except Exception as exc:  # noqa: BLE001 - locate is best-effort; fall back to full doc
            LOGGER.info("Region locate pass failed for %s (%s); using full document.", parsed_document.source_name, exc)
            return None

    def _extract_chunk_rows(
        self,
        parsed_document: ParsedDocument,
        planning: PreExtractionUnderstanding,
        prior_error_log: str | None,
        envelope_model: type[BaseModel],
        chunk: str,
    ) -> tuple[list[BaseModel], dict[str, str]]:
        chunk_note = (
            "This is one part of a larger source document. "
            "Extract only rows supported by THIS part; other parts are handled separately."
        )
        messages = self._build_extraction_messages(
            parsed_document, planning, prior_error_log, markdown_override=chunk, chunk_note=chunk_note
        )
        try:
            response = self._call_extractor(envelope_model, messages)
            return list(response.payload_rows), dict(response.anchoring_plan)
        except ExtractionOutputTooLargeError:
            # The chunk yielded more output than the model's token cap allows.
            # Split it in half on a line boundary and extract each part, down to a floor.
            halves = self._split_in_half(chunk)
            if halves is None:
                raise
            LOGGER.info("Chunk output too large; splitting %d chars into 2 sub-chunks.", len(chunk))
            payload_rows: list[BaseModel] = []
            anchoring_plan: dict[str, str] = {}
            for half in halves:
                rows, plan = self._extract_chunk_rows(
                    parsed_document, planning, prior_error_log, envelope_model, half
                )
                payload_rows.extend(rows)
                anchoring_plan.update(plan)
            return payload_rows, anchoring_plan

    def _split_in_half(self, chunk: str) -> list[str] | None:
        lines = chunk.splitlines(keepends=True)
        # Stop shrinking at a floor or when a chunk is a single unsplittable line.
        if len(lines) < 2 or len(chunk) <= 4000:
            return None
        mid = len(lines) // 2
        first = "".join(lines[:mid])
        second = "".join(lines[mid:])
        if not first.strip() or not second.strip():
            return None
        return [first, second]

    def _chunk_markdown(self, markdown: str) -> list[str]:
        limit = self.settings.extraction_max_chars_per_chunk
        if len(markdown) <= limit:
            return [markdown]

        # Split on line boundaries so table rows (one per line in rendered docx/pdf
        # markdown) are never cut mid-row. Pack lines greedily up to the limit.
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for line in markdown.splitlines(keepends=True):
            if current and current_len + len(line) > limit:
                chunks.append("".join(current))
                current = []
                current_len = 0
            current.append(line)
            current_len += len(line)
        if current:
            chunks.append("".join(current))
        return chunks

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
            "Infer the meaning of each output column for this subject from the approved sample contract first, then from the source structure. "
            "grade_level must be exactly one of: Elementary School, Middle School, High School."
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
5. Determine whether values such as source, subject, domain, topic, grade_level, display_grade, and grade_number are document-level, section-level, or row-level for this specific subject and source. grade_level must always normalize to exactly one of Elementary School, Middle School, or High School.
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
        markdown_override: str | None = None,
        chunk_note: str | None = None,
    ) -> list[dict[str, str]]:
        schema_config = load_schema_config(settings=self.settings)
        schema_json = json.dumps(schema_config.model_dump(mode="json"), indent=2)
        sample_contract_json = (
            json.dumps(schema_config.sample_contract.model_dump(mode="json"), indent=2)
            if schema_config.sample_contract
            else "null"
        )
        planning_json = json.dumps(planning.model_dump(mode="json"), indent=2)
        source_markdown = markdown_override if markdown_override is not None else parsed_document.markdown
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
            "grade_level must be exactly one of: Elementary School, Middle School, High School."
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
            "Return payload_rows in source order with no duplicates and no missing supported rows. "
            "grade_level must be exactly one of: Elementary School, Middle School, High School."
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

{("Chunking note:\n" + chunk_note + chr(10)) if chunk_note else ""}Validation feedback:
{correction_block}

Instructions:
1. Use the pre-extraction understanding artifact before extracting any values.
2. Build a temporary field anchoring plan explaining where each schema field appears in this specific layout.
3. Apply the sample-derived meaning of each column consistently across all rows. Once you infer what source, subject, domain, topic, grade_level, display_grade, and grade_number mean for this subject, do not drift to a different interpretation. grade_level must be exactly Elementary School, Middle School, or High School.
4. Extract every valid output row from the source, not just one row.
5. Ensure complete source coverage for all valid domains, topics, and descriptions that match the sample contract.
6. Return payload_rows in source reading order with one object per output row.
7. If the approved sample supports canonical public source links, use those links when they are identifiable from the source documents or staging context instead of local file names.
8. If the approved sample supports merged topic cells and one standard genuinely spans multiple topics, merge those topic names into one topic field using ` | ` rather than duplicating the row only for topic labels.
9. If the approved sample supports prefixed or formed display codes and the same raw display standard code repeats, disambiguate it with a short domain code or topic code prefix and a dot when that is needed to keep `Display standard code` unique.
10. If the source contradicts the draft representative_row, follow the source while preserving the sample contract.

Source markdown:
{source_markdown}
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

        from portkey_client import call_portkey_structured

        try:
            return call_portkey_structured(
                api_key=self.settings.portkey_api_key,
                provider=self.settings.portkey_extractor_provider,
                model=self.settings.extractor_model,
                response_model=response_model,
                messages=messages,
                max_tokens=32000,
                max_concurrency=self.settings.llm_max_concurrency,
            )
        except Exception as exc:
            LOGGER.exception(
                "Portkey extractor call failed. provider=%s model=%s error_type=%s error=%r",
                self.settings.portkey_extractor_provider,
                self.settings.extractor_model,
                type(exc).__name__,
                exc,
            )
            if _is_output_truncation_error(exc):
                raise ExtractionOutputTooLargeError(
                    f"Portkey extractor output truncated ({type(exc).__name__}): {exc}"
                ) from exc
            raise GeminiClientError(
                f"Portkey extractor failed ({type(exc).__name__}): {exc}"
            ) from exc

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
