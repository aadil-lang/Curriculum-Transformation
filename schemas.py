from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from config import DEFAULT_SCHEMA_CONFIG_PATH, Settings, get_settings


FieldType = Literal["string", "integer", "number", "boolean"]
SOURCE_CITATION_SUFFIX = "_source_citation"


class SchemaFieldSpec(BaseModel):
    name: str = Field(..., pattern=r"^[a-z][a-z0-9_]*$")
    description: str
    field_type: FieldType
    required: bool = False
    output_column: str | None = None
    example_value: str | None = None


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
    sample_rows: list[dict[str, str]] = Field(default_factory=list)


class TargetSchemaConfig(BaseModel):
    schema_name: str
    description: str
    fields: list[SchemaFieldSpec]
    sample_contract: SampleTransformationContract | None = None


class CitationPayloadBase(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    __schema_field_names__: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_field_citations(self) -> "CitationPayloadBase":
        for field_name in self.__class__.__schema_field_names__:
            citation_name = f"{field_name}{SOURCE_CITATION_SUFFIX}"
            value = getattr(self, field_name)
            citation = getattr(self, citation_name)

            has_value = value is not None and not (isinstance(value, str) and not value.strip())
            has_citation = bool(citation and citation.strip())

            if has_value and not has_citation:
                raise ValueError(f"Field '{field_name}' requires a non-empty source citation.")
            if not has_value and has_citation:
                raise ValueError(
                    f"Field '{citation_name}' must be empty when '{field_name}' is null or blank."
                )
        return self


class CsvRowBase(CitationPayloadBase):
    source_document: str
    source_type: Literal["pdf", "docx", "website"]
    source_identifier: str
    processed_at_utc: str


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


def _build_dynamic_fields(schema_config: TargetSchemaConfig) -> dict[str, tuple[Any, Field]]:
    field_map: dict[str, tuple[Any, Field]] = {}
    for spec in schema_config.fields:
        python_type = _map_field_type(spec.field_type)
        annotation = python_type if spec.required else python_type | None
        default = ... if spec.required else None
        field_map[spec.name] = (
            annotation,
            Field(default=default, description=spec.description),
        )
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


def get_extraction_payload_model(schema_path: str | None = None) -> type[BaseModel]:
    schema_config = load_schema_config(schema_path)
    model = create_model(
        "ExtractionPayload",
        __base__=CitationPayloadBase,
        __module__=__name__,
        **_build_dynamic_fields(schema_config),
    )
    model.__schema_field_names__ = tuple(spec.name for spec in schema_config.fields)
    return model


def get_target_row_model(schema_path: str | None = None) -> type[BaseModel]:
    schema_config = load_schema_config(schema_path)
    model = create_model(
        "TargetCsvRow",
        __base__=CsvRowBase,
        __module__=__name__,
        **_build_dynamic_fields(schema_config),
    )
    model.__schema_field_names__ = tuple(spec.name for spec in schema_config.fields)
    return model


def csv_headers(schema_path: str | None = None) -> list[str]:
    schema_config = load_schema_config(schema_path)
    headers = ["source_document", "source_type", "source_identifier", "processed_at_utc"]
    for spec in schema_config.fields:
        output_column = get_output_column_name(spec)
        headers.append(output_column)
        headers.append(f"{output_column}{SOURCE_CITATION_SUFFIX}")
    return headers


def flatten_row(row: BaseModel, schema_path: str | None = None) -> dict[str, Any]:
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
        ordered[f"{output_column}{SOURCE_CITATION_SUFFIX}"] = data.get(
            f"{spec.name}{SOURCE_CITATION_SUFFIX}"
        )
    return ordered
