from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

import yaml

PROMPTS_PATH = Path(__file__).with_name("prompts.yaml")


class _SafeFormatDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@lru_cache(maxsize=1)
def load_prompts() -> Dict[str, Any]:
    with PROMPTS_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Prompt file must contain a mapping: {PROMPTS_PATH}")
    return data


def get_prompt(path: str, **values: Any) -> str:
    node: Any = load_prompts()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(f"Prompt key not found: {path}")
        node = node[part]
    if not isinstance(node, str):
        raise TypeError(f"Prompt key is not a string: {path}")
    if not values:
        return node
    return node.format_map(_SafeFormatDict({key: str(value) for key, value in values.items()}))


def get_react_tools() -> List[Dict[str, Any]]:
    tools = load_prompts().get("react_tools", [])
    if not isinstance(tools, list):
        raise TypeError("react_tools must be a list")
    return deepcopy(tools)
