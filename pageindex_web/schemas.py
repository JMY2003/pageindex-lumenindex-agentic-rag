import json
import re
from typing import Any, Dict, List

from pydantic import BaseModel, Field, ValidationError, conint


class DocumentStructureRow(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    level: conint(ge=1, le=6) = 1
    start_page: conint(ge=1) = 1
    end_page: conint(ge=1) = 1


class NodeSelectionRow(BaseModel):
    document_id: str = Field(..., min_length=1, max_length=160)
    node_id: str = Field(..., min_length=1, max_length=160)
    reason: str = Field(default="", max_length=1000)


class DirectoryToolArgs(BaseModel):
    document_names: List[str] = Field(default_factory=list, max_length=20)
    target: str = Field(default="ALL", max_length=80)
    node_id: str = Field(default="", max_length=160)


class SearchToolArgs(BaseModel):
    document_names: List[str] = Field(default_factory=list, max_length=20)
    query: str = Field(default="", max_length=4000)
    top_k: conint(ge=1, le=12) = 8


class PageContentToolArgs(BaseModel):
    document_names: List[str] = Field(default_factory=list, max_length=20)
    pages: List[conint(ge=1)] = Field(default_factory=list, max_length=12)


def extract_json_array(text: str) -> List[Any]:
    match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text or "", re.S)
    raw = match.group(1) if match else text
    if not raw:
        return []
    start = raw.find("[")
    end = raw.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def validate_model(model: type[BaseModel], payload: Any) -> BaseModel:
    return model.model_validate(payload)


def validate_model_list(model: type[BaseModel], rows: Any, limit: int = 120) -> List[BaseModel]:
    if not isinstance(rows, list):
        return []
    validated: List[BaseModel] = []
    for row in rows[:limit]:
        try:
            validated.append(validate_model(model, row))
        except ValidationError:
            continue
    return validated


def parse_tool_arguments(raw: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
