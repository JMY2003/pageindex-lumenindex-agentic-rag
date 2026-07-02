import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable

DEFAULT_SETTINGS: Dict[str, Any] = {
    "api_protocol": "openai",
    "api_url": "https://api.openai.com/v1",
    "api_key": "",
    "model": "gpt-4o-mini",
    "timeout": 60,
    "context_window": 8000,
    "context_window_k": 8,
    "step_budget": 50,
    "max_output_tokens": 3072,
    "deep_thinking": True,
    "context_enabled": True,
}


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"1", "true", "yes", "on"}


def _clean_env_value(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def _zshrc_values() -> Dict[str, str]:
    path = Path.home() / ".zshrc"
    if not path.exists():
        return {}
    values: Dict[str, str] = {}
    pattern = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = pattern.match(line)
            if not match:
                continue
            key, value = match.group(1), match.group(2)
            if key.startswith(("OPENAI_", "CHATGPT_", "ANTHROPIC_", "QWEN_", "DASHSCOPE_", "PAGEINDEX_", "LLM_", "AGENT_")):
                values[key] = _clean_env_value(value.split(" #", 1)[0])
    except OSError:
        return {}
    return values


def _env_values() -> Dict[str, str]:
    env = {key: value for key, value in os.environ.items() if value}
    zshrc = _zshrc_values()
    zshrc.update(env)
    return zshrc


def _first_env(env: Dict[str, str], keys: Iterable[str], default: Any = "") -> Any:
    for key in keys:
        value = env.get(key)
        if value not in {None, ""}:
            return value
    return default


def _stored_value(stored: Dict[str, Any], key: str) -> Any:
    value = stored.get(key)
    return value if value not in {None, ""} else None


class SettingsStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self, include_secret: bool = False) -> Dict[str, Any]:
        data = dict(DEFAULT_SETTINGS)
        stored: Dict[str, Any] = {}
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    stored = json.load(f)
                if isinstance(stored, dict):
                    data.update(stored)
                else:
                    stored = {}
            except (OSError, json.JSONDecodeError):
                pass

        env = _env_values()

        protocol = _stored_value(stored, "api_protocol") or _first_env(env, ["PAGEINDEX_API_PROTOCOL"], data["api_protocol"])
        data["api_protocol"] = protocol
        if protocol == "anthropic":
            data["api_key"] = (
                _stored_value(stored, "api_key")
                or _first_env(env, ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"])
                or data.get("api_key", "")
            )
            data["api_url"] = (
                _stored_value(stored, "api_url")
                or _first_env(env, ["ANTHROPIC_API_BASE", "ANTHROPIC_BASE_URL"])
                or data.get("api_url", "")
            )
        else:
            data["api_key"] = _stored_value(stored, "api_key") or _first_env(env, ["OPENAI_API_KEY", "CHATGPT_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY"], data.get("api_key", ""))
            data["api_url"] = _stored_value(stored, "api_url") or _first_env(env, ["OPENAI_API_BASE", "OPENAI_BASE_URL", "QWEN_BASE_URL", "QWEN_API_BASE", "DASHSCOPE_BASE_URL", "DASHSCOPE_API_BASE"], data.get("api_url", ""))

        data["model"] = _stored_value(stored, "model") or _first_env(env, ["PAGEINDEX_MODEL", "QWEN_MODEL", "DASHSCOPE_MODEL"], data.get("model", DEFAULT_SETTINGS["model"]))
        data["timeout"] = int(_stored_value(stored, "timeout") or _first_env(env, ["PAGEINDEX_TIMEOUT"], data.get("timeout", 60)))
        context_window_setting = _stored_value(stored, "context_window")
        context_window_env = _first_env(env, ["PAGEINDEX_CONTEXT_WINDOW", "AGENT_COMPACT_TRIGGER_TOKENS"])
        if context_window_setting:
            data["context_window"] = int(context_window_setting)
            data["context_window_k"] = max(1, int(data["context_window"]) // 1000)
        elif context_window_env:
            data["context_window"] = int(context_window_env)
            data["context_window_k"] = max(1, int(data["context_window"]) // 1000)
        else:
            if "context_window_k" in data and data.get("context_window_k"):
                data["context_window"] = int(data["context_window_k"]) * 1000
            else:
                data["context_window"] = int(data.get("context_window", 8000))
                data["context_window_k"] = max(1, int(data["context_window"]) // 1000)
        data["step_budget"] = int(_stored_value(stored, "step_budget") or _first_env(env, ["PAGEINDEX_STEP_BUDGET"], data.get("step_budget", 50)))
        data["max_output_tokens"] = int(_stored_value(stored, "max_output_tokens") or _first_env(env, ["LLM_MAX_OUTPUT_TOKENS", "PAGEINDEX_MAX_OUTPUT_TOKENS"], data.get("max_output_tokens", 3072)))
        data["deep_thinking"] = _to_bool(stored["deep_thinking"] if "deep_thinking" in stored else _first_env(env, ["PAGEINDEX_DEEP_THINKING"], data.get("deep_thinking", True)))
        data["context_enabled"] = _to_bool(stored["context_enabled"] if "context_enabled" in stored else _first_env(env, ["PAGEINDEX_CONTEXT_ENABLED"], data.get("context_enabled", False)))
        data["has_api_key"] = bool(data.get("api_key"))
        if not include_secret:
            data["api_key"] = ""
        return data

    def save(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        current = self.load(include_secret=True)
        allowed = set(DEFAULT_SETTINGS)
        for key, value in patch.items():
            if key in allowed and value is not None:
                current[key] = value
        if current.get("context_window_k"):
            current["context_window_k"] = max(1, min(200, int(current.get("context_window_k", 8))))
            current["context_window"] = current["context_window_k"] * 1000
        current["step_budget"] = max(10, min(100, int(current.get("step_budget", 50))))
        current["timeout"] = max(5, min(600, int(current.get("timeout", 60))))
        current["context_window"] = max(1000, min(200000, int(current.get("context_window", 8000))))
        current["context_window_k"] = max(1, min(200, int(current["context_window"] // 1000)))
        current["max_output_tokens"] = max(512, min(40000, int(current.get("max_output_tokens", 3072))))
        current["deep_thinking"] = _to_bool(current.get("deep_thinking"))
        current["context_enabled"] = _to_bool(current.get("context_enabled"))
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump({k: current[k] for k in DEFAULT_SETTINGS}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return self.load(include_secret=False)
