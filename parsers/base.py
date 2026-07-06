from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ParsedDocument(BaseModel):
    document_id: str
    source_path: str
    source_name: str
    source_type: Literal["pdf", "docx", "website"]
    markdown: str
    metadata: dict[str, Any] = Field(default_factory=dict)
