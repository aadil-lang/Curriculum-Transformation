from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from config import DEFAULT_SCHEMA_CONFIG_PATH, Settings, get_settings


FieldType = Literal["string", "integer", "number", "boolean"]
SOURCE_CITATION_SUFFIX = "_source_citation"
# Trailing marker columns appended after the schema columns. Unfixed rows are
# kept in the CSV (not dropped) and flagged here so a human can review or delete.
REVIEW_STATUS_COLUMN = "review_status"
REVIEW_ISSUES_COLUMN = "review_issues"
REVIEW_STATUS_OK = ""
REVIEW_STATUS_NEEDS_REVIEW = "NEEDS_REVIEW"
ALLOWED_GRADE_LEVELS = ("Elementary School", "Middle School", "High School")
GRADE_LEVEL_NAMING_RULE = (
    "Use exactly one of these grade_level values: Elementary School, Middle School, High School. "
    "Map source banding terminology to the closest allowed label. Do not use other labels such as "
    "Primary, Middle Years, Senior Years, Secondary, Senior, or similar variants."
)


# Program/course labels that name a course, not a learner grade band — must never
# occupy display_grade / grade_number.
PROGRAM_GRADE_LABELS = frozenset(
    {
        "ap", "advanced placement", "ib", "international baccalaureate",
        "honors", "honours", "gcse", "igcse", "a-level", "a level",
        "as-level", "as level",
    }
)
_GRADE_LEVEL_BUCKETS_LOWER = frozenset(b.lower() for b in ALLOWED_GRADE_LEVELS)
_STRUCTURAL_PREFIX = __import__("re").compile(
    r"^\s*(?:unit|topic|module|cluster|lesson|section|chapter)\s+[0-9]+[.:)\-]?[0-9]*\s*[:.\-]?\s*",
    __import__("re").IGNORECASE,
)


def _sanitize_grade_cell(value: str, *, expand_ranges: bool) -> str:
    """display_grade/grade_number must hold a real band, never a program label or bucket.

    Blanks program/course labels ('AP') and grade buckets ('High School'); expands a
    numeric range ('9-12' -> '9,10,11,12') for grade_number. Returns the cleaned value.
    """
    text = (value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered in PROGRAM_GRADE_LABELS or lowered in _GRADE_LEVEL_BUCKETS_LOWER:
        return ""
    if expand_ranges:
        import re as _re

        m = _re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", text)
        if m:
            low, high = int(m.group(1)), int(m.group(2))
            if low <= high and high - low <= 20:
                return ",".join(str(n) for n in range(low, high + 1))
    return text


def _strip_structural_prefix(value: str) -> str:
    """Remove a leading 'Unit N:'/'Topic N.N:'/etc. marker, keeping the real heading."""
    text = (value or "").strip()
    if not text:
        return ""
    stripped = _STRUCTURAL_PREFIX.sub("", text).strip()
    return stripped or text  # never blank a heading to nothing


def apply_row_guards(values: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Deterministic quality guards on a row's internal-field values (fix + check).

    Returns (fixed_values, unfixable_issues). Fixes what it safely can:
    - display_grade / grade_number: drop program labels & buckets; expand grade_number ranges
    - domain never empty: promote topic into domain (never the reverse)
    - strip 'Unit N:'/'Topic N.N:' structural prefixes from domain/topic
    Flags what it cannot fix:
    - grade_level not mappable to Elementary/Middle/High School -> issue (caller flags NEEDS_REVIEW)
    """
    fixed = dict(values)
    issues: list[str] = []

    if "display_grade" in fixed:
        fixed["display_grade"] = _sanitize_grade_cell(str(fixed.get("display_grade") or ""), expand_ranges=False)
    if "grade_number" in fixed:
        fixed["grade_number"] = _sanitize_grade_cell(str(fixed.get("grade_number") or ""), expand_ranges=True)

    for key in ("domain", "topic"):
        if key in fixed and isinstance(fixed.get(key), str):
            fixed[key] = _strip_structural_prefix(fixed[key])

    # domain must never be empty; topic may be. Promote topic -> domain (never reverse).
    if "domain" in fixed and "topic" in fixed:
        domain_val = str(fixed.get("domain") or "").strip()
        topic_val = str(fixed.get("topic") or "").strip()
        if not domain_val and topic_val:
            fixed["domain"] = topic_val
            fixed["topic"] = ""

    # grade_level: coerce to an allowed bucket; flag if unmappable.
    if "grade_level" in fixed:
        raw = str(fixed.get("grade_level") or "").strip()
        if raw:
            mapped = normalize_grade_level(raw)
            if mapped:
                fixed["grade_level"] = mapped
            else:
                issues.append(
                    f"grade_level '{raw}' is not one of Elementary School / Middle School / High School "
                    "and could not be mapped."
                )

    return fixed, issues


def normalize_grade_level(value: str | None) -> str | None:
    if not value or not isinstance(value, str):
        return None
    normalized = value.strip()
    if normalized in ALLOWED_GRADE_LEVELS:
        return normalized

    lowered = normalized.casefold()
    elementary_aliases = {"elementary school", "elementary", "primary school", "primary", "k-5", "k-6"}
    middle_aliases = {"middle school", "middle", "junior high", "junior high school", "middle years", "junior"}
    high_aliases = {"high school", "high", "senior school", "senior years", "senior", "secondary school", "secondary"}
    if lowered in elementary_aliases or lowered.startswith(("elementary", "primary")):
        return "Elementary School"
    if lowered in middle_aliases or lowered.startswith("middle"):
        return "Middle School"
    if lowered in high_aliases or lowered.startswith(("high", "senior", "secondary")):
        return "High School"
    return None


# Fields whose value is derived/transformed/inherited rather than copied verbatim
# from the source (e.g. a canonical URL, a mapped grade band, a topic inferred from
# a section heading, a synthetic display code). These cannot always carry a verbatim
# citation, so citation enforcement is relaxed for them.
DERIVED_FIELD_NAMES = frozenset(
    {
        "source",
        "grade_level",
        "display_grade",
        "grade_number",
        "grade_string",
        "subject",
        "domain",
        "topic",
        "l3",
        "l4",
        "l5",
        "display_standard_code",
        "czi_standard_code",
    }
)


def default_requires_citation(field_name: str) -> bool:
    return field_name not in DERIVED_FIELD_NAMES


class SchemaFieldSpec(BaseModel):
    name: str = Field(..., pattern=r"^[a-z][a-z0-9_]*$")
    description: str
    field_type: FieldType
    required: bool = False
    output_column: str | None = None
    example_value: str | None = None
    requires_citation: bool = True


class SampleTransformationContract(BaseModel):
    column_order: list[str] = Field(default_factory=list)
    required_columns: list[str] = Field(default_factory=list)
    subject_naming: str = ""
    grade_level_naming: str = ""
    display_grade_logic: str = ""
    grade_string_logic: str = ""
    display_standard_code_logic: str = ""
    source_link_format: str = ""
    description_style: str = ""
    description_integrity_rules: list[str] = Field(default_factory=list)
    description_multiline_style: str = ""
    description_merge_split_style: str = ""
    bullet_and_punctuation_style: str = ""
    symbol_preservation_rules: list[str] = Field(default_factory=list)
    field_placement_rules: dict[str, str] = Field(default_factory=dict)
    disallowed_output_content: list[str] = Field(default_factory=list)
    noise_rejection_rules: list[str] = Field(default_factory=list)
    output_quality_rules: list[str] = Field(default_factory=list)
    sample_rows: list[dict[str, str]] = Field(default_factory=list)
    # Output columns that are blank in every sample row. Enforced empty in output
    # so the pipeline does not populate a column the approved sample never uses
    # (e.g. an l4/l5 hierarchy level the sample flattens away).
    always_empty_columns: list[str] = Field(default_factory=list)


class TargetSchemaConfig(BaseModel):
    schema_name: str
    description: str
    fields: list[SchemaFieldSpec]
    sample_contract: SampleTransformationContract | None = None


class CitationPayloadBase(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    __schema_field_names__: tuple[str, ...] = ()
    __citation_required_fields__: frozenset[str] = frozenset()

    @model_validator(mode="after")
    def validate_field_citations(self) -> "CitationPayloadBase":
        for field_name in self.__class__.__schema_field_names__:
            citation_name = f"{field_name}{SOURCE_CITATION_SUFFIX}"
            # Citation fields are absent when citation generation is disabled;
            # there is nothing to enforce in that mode.
            if not hasattr(self, citation_name):
                continue
            value = getattr(self, field_name)
            citation = getattr(self, citation_name)

            has_value = value is not None and not (isinstance(value, str) and not value.strip())
            has_citation = bool(citation and citation.strip())

            citation_required = field_name in self.__class__.__citation_required_fields__
            if has_value and not has_citation and citation_required:
                raise ValueError(f"Field '{field_name}' requires a non-empty source citation.")
            if not has_value and has_citation and citation_required:
                raise ValueError(
                    f"Field '{citation_name}' must be empty when '{field_name}' is null or blank."
                )
        return self


PIPELINE_METADATA_FIELDS = ("source_document", "source_type", "source_identifier", "processed_at_utc")


class CsvRowBase(CitationPayloadBase):
    source_document: str
    source_type: Literal["pdf", "docx", "website"]
    source_identifier: str
    processed_at_utc: str


def schema_only_row_view(row: BaseModel) -> dict[str, Any]:
    """Row content minus pipeline-managed metadata fields.

    The critic and reviewer must judge rows against the sample contract only.
    Exposing pipeline metadata (source_document, processed_at_utc, ...) makes the
    LLM flag those fields as contract violations, so hide them from review.
    """
    data = row.model_dump(mode="json")
    return {key: value for key, value in data.items() if key not in PIPELINE_METADATA_FIELDS}


def critic_row_view(
    row: BaseModel,
    schema_config: "TargetSchemaConfig",
    *,
    include_citations: bool = True,
) -> dict[str, Any]:
    """Row view for the critic that hides citation keys when appropriate.

    When citations are disabled, strip every citation key so the critic cannot
    demand snippets the pipeline no longer collects. When enabled, hide citation
    keys for derived fields only so transformed columns are not audited on quotes.
    """
    data = schema_only_row_view(row)
    if not include_citations:
        return {key: value for key, value in data.items() if not key.endswith(SOURCE_CITATION_SUFFIX)}
    hidden_citations = {
        f"{spec.name}{SOURCE_CITATION_SUFFIX}"
        for spec in schema_config.fields
        if not spec.requires_citation
    }
    return {key: value for key, value in data.items() if key not in hidden_citations}


def _map_field_type(field_type: FieldType) -> type[Any]:
    return {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
    }[field_type]


def load_schema_config(path: str | None = None, settings: Settings | None = None) -> TargetSchemaConfig:
    active_settings = settings or get_settings()
    if path is None:
        schema_path = active_settings.schema_config_path
    else:
        candidate = Path(path)
        schema_path = candidate if candidate.is_absolute() else DEFAULT_SCHEMA_CONFIG_PATH.parent / candidate
    raw_data = json.loads(schema_path.read_text(encoding="utf-8"))
    return TargetSchemaConfig.model_validate(raw_data)


def _build_dynamic_fields(
    schema_config: TargetSchemaConfig, include_citations: bool = True
) -> dict[str, tuple[Any, Field]]:
    field_map: dict[str, tuple[Any, Field]] = {}
    for spec in schema_config.fields:
        python_type = _map_field_type(spec.field_type)
        annotation = python_type if spec.required else python_type | None
        default = ... if spec.required else None
        field_map[spec.name] = (
            annotation,
            Field(default=default, description=spec.description),
        )
        if include_citations:
            field_map[f"{spec.name}{SOURCE_CITATION_SUFFIX}"] = (
                str,
                Field(
                    default="",
                    description=f"Verbatim supporting quote for '{spec.name}' from the source document.",
                ),
            )
    return field_map


def get_schema_fields(schema_path: str | None = None, settings: Settings | None = None) -> list[SchemaFieldSpec]:
    return load_schema_config(schema_path, settings=settings).fields


def get_output_column_name(spec: SchemaFieldSpec) -> str:
    return spec.output_column or spec.name


def _citation_required_fields(schema_config: TargetSchemaConfig) -> frozenset[str]:
    return frozenset(spec.name for spec in schema_config.fields if spec.requires_citation)


def get_extraction_payload_model(
    schema_path: str | None = None, include_citations: bool = True
) -> type[BaseModel]:
    schema_config = load_schema_config(schema_path)
    model = create_model(
        "ExtractionPayload",
        __base__=CitationPayloadBase,
        __module__=__name__,
        **_build_dynamic_fields(schema_config, include_citations=include_citations),
    )
    model.__schema_field_names__ = tuple(spec.name for spec in schema_config.fields)
    model.__citation_required_fields__ = (
        _citation_required_fields(schema_config) if include_citations else frozenset()
    )
    return model


def get_target_row_model(
    schema_path: str | None = None, include_citations: bool = True
) -> type[BaseModel]:
    schema_config = load_schema_config(schema_path)
    # Citation columns are omitted entirely when citation generation is disabled.
    model = create_model(
        "TargetCsvRow",
        __base__=CsvRowBase,
        __module__=__name__,
        **_build_dynamic_fields(schema_config, include_citations=include_citations),
    )
    model.__schema_field_names__ = tuple(spec.name for spec in schema_config.fields)
    model.__citation_required_fields__ = (
        _citation_required_fields(schema_config) if include_citations else frozenset()
    )
    return model


def csv_headers(schema_path: str | None = None, *, include_citations: bool | None = None) -> list[str]:
    if include_citations is None:
        include_citations = get_settings().extraction_citations_enabled
    schema_config = load_schema_config(schema_path)
    headers = ["source_document", "source_type", "source_identifier", "processed_at_utc"]
    for spec in schema_config.fields:
        output_column = get_output_column_name(spec)
        headers.append(output_column)
        if include_citations:
            headers.append(f"{output_column}{SOURCE_CITATION_SUFFIX}")
    headers.append(REVIEW_STATUS_COLUMN)
    headers.append(REVIEW_ISSUES_COLUMN)
    return headers


def flatten_row(
    row: BaseModel,
    schema_path: str | None = None,
    review_status: str = REVIEW_STATUS_OK,
    review_issues: str = "",
    *,
    include_citations: bool | None = None,
) -> dict[str, Any]:
    if include_citations is None:
        include_citations = get_settings().extraction_citations_enabled
    data = row.model_dump()
    ordered: dict[str, Any] = {}
    schema_fields = get_schema_fields(schema_path)
    ordered["source_document"] = data.get("source_document")
    ordered["source_type"] = data.get("source_type")
    ordered["source_identifier"] = data.get("source_identifier")
    ordered["processed_at_utc"] = data.get("processed_at_utc")
    for spec in schema_fields:
        output_column = get_output_column_name(spec)
        ordered[output_column] = data.get(spec.name)
        if include_citations:
            ordered[f"{output_column}{SOURCE_CITATION_SUFFIX}"] = data.get(
                f"{spec.name}{SOURCE_CITATION_SUFFIX}"
            )
    ordered[REVIEW_STATUS_COLUMN] = review_status
    ordered[REVIEW_ISSUES_COLUMN] = review_issues
    return ordered
