import json
import os
import asyncio
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .prompts import get_prompt
from .schemas import parse_tool_arguments


class LLMError(RuntimeError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    stop_reason: str = "stop"
    raw: Dict[str, Any] = field(default_factory=dict)


def estimate_tokens_text(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u3400" <= ch <= "\u9fff")
    if len(text) and cjk / len(text) > 0.2:
        return cjk + int((len(text) - cjk) / 4)
    return max(1, int(len(text) / 4))


def token_counter(model: str = "", messages: Optional[List[Dict[str, Any]]] = None, text: str = "") -> int:
    payload = text or json.dumps(messages or [], ensure_ascii=False)
    return estimate_tokens_text(payload)


def estimate_messages_tokens(messages: List[Dict[str, Any]]) -> int:
    return estimate_tokens_text(json.dumps(messages, ensure_ascii=False))


def _json_request(url: str, headers: Dict[str, str], payload: Dict[str, Any], timeout: int) -> Dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise LLMError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise LLMError(str(exc)) from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise LLMError(f"Invalid JSON response: {body[:500]}") from exc


def _settings_value(settings: Dict[str, Any], key: str, default: str = "") -> str:
    return str(settings.get(key) or default).strip()


def _api_key(settings: Dict[str, Any]) -> str:
    protocol = _settings_value(settings, "api_protocol", "openai")
    if settings.get("api_key"):
        return str(settings["api_key"])
    if protocol == "anthropic":
        return os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN") or ""
    return os.getenv("OPENAI_API_KEY") or os.getenv("CHATGPT_API_KEY") or ""


def has_api_key(settings: Dict[str, Any]) -> bool:
    return bool(_api_key(settings))


def _base_url(settings: Dict[str, Any]) -> str:
    protocol = _settings_value(settings, "api_protocol", "openai")
    if settings.get("api_url"):
        return str(settings["api_url"]).rstrip("/")
    if protocol == "anthropic":
        return (os.getenv("ANTHROPIC_API_BASE") or os.getenv("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").rstrip("/")
    return (os.getenv("OPENAI_API_BASE") or "https://api.openai.com/v1").rstrip("/")


def _model(settings: Dict[str, Any]) -> str:
    return _settings_value(settings, "model", "gpt-4o-mini")


def _timeout(settings: Dict[str, Any]) -> int:
    return int(settings.get("timeout") or 60)


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    chunks.append(str(block.get("text", "")))
                elif block.get("type") == "tool_result":
                    chunks.append(str(block.get("content", "")))
            else:
                chunks.append(str(block))
        return "\n".join(chunks)
    return str(content or "")


def _openai_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    output = []
    for message in messages:
        role = message.get("role", "user")
        if role == "tool":
            output.append({
                "role": "tool",
                "tool_call_id": message.get("tool_call_id") or message.get("id") or "tool",
                "content": _content_to_text(message.get("content")),
            })
        else:
            item = {"role": role, "content": _content_to_text(message.get("content"))}
            if message.get("tool_calls"):
                item["tool_calls"] = message["tool_calls"]
            output.append(item)
    return output


def _anthropic_messages(messages: List[Dict[str, Any]]) -> tuple[str, List[Dict[str, Any]]]:
    system_parts = []
    output: List[Dict[str, Any]] = []
    pending_tool_results: List[Dict[str, Any]] = []
    for message in messages:
        role = message.get("role", "user")
        if role == "system":
            system_parts.append(_content_to_text(message.get("content")))
            continue
        if role == "tool":
            pending_tool_results.append({
                "type": "tool_result",
                "tool_use_id": message.get("tool_call_id") or message.get("id") or "tool",
                "content": _content_to_text(message.get("content")),
            })
            continue
        if pending_tool_results:
            output.append({"role": "user", "content": pending_tool_results})
            pending_tool_results = []
        if role not in {"user", "assistant"}:
            role = "user"
        if role == "assistant" and isinstance(message.get("content"), list):
            output.append({"role": role, "content": message["content"]})
        else:
            output.append({"role": role, "content": _content_to_text(message.get("content"))})
    if pending_tool_results:
        output.append({"role": "user", "content": pending_tool_results})
    return "\n\n".join(system_parts), output


def _openai_tools(tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    if not tools:
        return None
    return [{"type": "function", "function": tool} for tool in tools]


def _anthropic_tools(tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    if not tools:
        return None
    converted = []
    for tool in tools:
        converted.append({
            "name": tool["name"],
            "description": tool.get("description", ""),
            "input_schema": tool.get("parameters", {"type": "object", "properties": {}}),
        })
    return converted


def completion(
    settings: Dict[str, Any],
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    max_tokens: int = 1800,
    temperature: float = 0.2,
    retries: int = 2,
) -> LLMResponse:
    if not has_api_key(settings):
        raise LLMError("API key is not configured")
    protocol = _settings_value(settings, "api_protocol", "openai")
    retries = int(os.getenv("LLM_MAX_RETRIES") or retries)
    last_error: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            if protocol == "anthropic":
                return _anthropic_completion(settings, messages, tools, max_tokens, temperature)
            return _openai_completion(settings, messages, tools, max_tokens, temperature)
        except LLMError as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(0.6 * (attempt + 1))
    raise last_error or LLMError("LLM call failed")


async def acompletion(
    settings: Dict[str, Any],
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    max_tokens: int = 1800,
    temperature: float = 0.2,
    retries: int = 2,
) -> LLMResponse:
    return await asyncio.to_thread(completion, settings, messages, tools, max_tokens, temperature, retries)


def _openai_completion(
    settings: Dict[str, Any],
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]],
    max_tokens: int,
    temperature: float,
) -> LLMResponse:
    url = _base_url(settings).rstrip("/") + "/chat/completions"
    payload: Dict[str, Any] = {
        "model": _model(settings).removeprefix("openai/").removeprefix("litellm/"),
        "messages": _openai_messages(messages),
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    converted_tools = _openai_tools(tools)
    if converted_tools:
        payload["tools"] = converted_tools
        payload["tool_choice"] = "auto"
    raw = _json_request(
        url,
        {"Authorization": f"Bearer {_api_key(settings)}", "Content-Type": "application/json"},
        payload,
        _timeout(settings),
    )
    choice = (raw.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    calls = []
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        args = parse_tool_arguments(fn.get("arguments") or "{}")
        calls.append(ToolCall(id=call.get("id") or fn.get("name") or "tool", name=fn.get("name", ""), arguments=args))
    return LLMResponse(text=msg.get("content") or "", tool_calls=calls, stop_reason=choice.get("finish_reason") or "stop", raw=raw)


def _anthropic_completion(
    settings: Dict[str, Any],
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]],
    max_tokens: int,
    temperature: float,
) -> LLMResponse:
    url = _base_url(settings).rstrip("/") + "/v1/messages"
    system, anthropic_messages = _anthropic_messages(messages)
    payload: Dict[str, Any] = {
        "model": _model(settings).removeprefix("anthropic/").removeprefix("litellm/"),
        "messages": anthropic_messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if system:
        payload["system"] = system
    converted_tools = _anthropic_tools(tools)
    if converted_tools:
        payload["tools"] = converted_tools
    raw = _json_request(
        url,
        {
            "x-api-key": _api_key(settings),
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        payload,
        _timeout(settings),
    )
    text_parts = []
    calls = []
    for block in raw.get("content") or []:
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            calls.append(ToolCall(id=block.get("id") or block.get("name") or "tool", name=block.get("name", ""), arguments=block.get("input") or {}))
    return LLMResponse(text="\n".join(text_parts), tool_calls=calls, stop_reason=raw.get("stop_reason") or "stop", raw=raw)


def complete(settings: Dict[str, Any], messages: List[Dict[str, str]], fallback: str) -> str:
    if not has_api_key(settings):
        return fallback
    try:
        response = completion(settings, messages, tools=None, max_tokens=2200, temperature=0.2)
        return response.text or fallback
    except Exception as exc:
        return fallback + f"\n\n> The LLM call did not complete. Returning the local retrieval summary instead. Error: {exc}"


def synthesize_answer(question: str, results: List[Dict[str, Any]], pages: List[Dict[str, Any]]) -> str:
    if not results and not pages:
        return "No sufficiently relevant content was found in the indexed documents. Confirm that documents are selected or try a more specific question."
    lines = [f"Based on the current index, I found the most relevant material for \"{question}\":\n"]
    for item in results[:5]:
        page_range = item.get("pages", [])
        page_text = f"Page {page_range[0]}-{page_range[1]}" if len(page_range) == 2 else "Page ?"
        lines.append(f"- **{item.get('document_name')} / {item.get('title')}** ({page_text}): {item.get('snippet')}")
    if pages:
        lines.append("\nPage excerpts available for direct citation:")
        for page in pages[:4]:
            text = (page.get("content") or "").strip().replace("\n", " ")
            if len(text) > 320:
                text = text[:320] + "..."
            lines.append(f"- **{page.get('document_name')} Page {page.get('page')}**: {text}")
    lines.append("\nIf an API key is configured, the system will generate a fuller natural-language answer from this evidence.")
    return "\n".join(lines)


def build_prompt(question: str, results: List[Dict[str, Any]], pages: List[Dict[str, Any]], conversation_context: str = "") -> List[Dict[str, str]]:
    evidence = []
    for item in results[:8]:
        evidence.append(
            f"[Search] {item.get('document_name')} | {item.get('title')} | pages={item.get('pages')} | {item.get('snippet')}"
        )
    for page in pages[:8]:
        evidence.append(
            f"[Page] {page.get('document_name')} Page {page.get('page')}\n{(page.get('content') or '')[:1800]}"
        )
    messages = [
        {"role": "system", "content": get_prompt("standard_answer.system")},
    ]
    if conversation_context:
        messages.append({"role": "user", "content": f"Conversation context from this chat session:\n{conversation_context}"})
    messages.append({"role": "user", "content": get_prompt("standard_answer.user", question=question, evidence="\n\n".join(evidence))})
    return messages
