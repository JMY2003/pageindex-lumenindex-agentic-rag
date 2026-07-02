import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .llm import completion, estimate_messages_tokens
from .prompts import get_prompt
from .storage import now_iso

SUMMARY_SECTIONS = [
    "User Request",
    "Active Documents",
    "Retrieval Path",
    "Evidence",
    "Conclusions",
    "Errors",
    "Next Actions",
    "Current Work",
]


def archive_messages(log_dir: Path, messages: List[Dict[str, Any]]) -> Path:
    target_dir = log_dir / "context_compactions"
    target_dir.mkdir(parents=True, exist_ok=True)
    name = now_iso().replace(":", "").replace("-", "") + ".json"
    path = target_dir / name
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump({"created_time": now_iso(), "messages": messages}, f, ensure_ascii=False, indent=2)
    tmp.replace(path)
    return path


def _deterministic_summary(messages: List[Dict[str, Any]], archived_path: Path) -> str:
    text = json.dumps(messages, ensure_ascii=False)
    excerpt = text[:2500] + ("..." if len(text) > 2500 else "")
    lines = [f"## {section}\n" for section in SUMMARY_SECTIONS]
    lines[0] += "Earlier conversation has been compacted while preserving the user goal, tool calls, and evidence path.\n"
    lines[2] += excerpt
    lines[7] += f"Full pre-compaction context archive: {archived_path}"
    return "\n".join(lines)


def _outline_nodes(nodes: List[Dict[str, Any]], level: int = 0, limit: int = 500) -> List[str]:
    lines: List[str] = []
    for node in nodes:
        if len(lines) >= limit:
            break
        pages = node.get("pages") or [node.get("start_index", ""), node.get("end_index", "")]
        indent = "  " * level
        lines.append(f"{indent}- {node.get('node_id', '')} {node.get('title', '')} P{pages[0]}-{pages[1]}")
        lines.extend(_outline_nodes(node.get("nodes") or [], level + 1, limit - len(lines)))
    return lines[:limit]


def _slim_directory_observations(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    slimmed: List[Dict[str, Any]] = []
    for message in messages:
        if message.get("role") != "tool":
            slimmed.append(message)
            continue
        try:
            payload = json.loads(str(message.get("content") or ""))
        except json.JSONDecodeError:
            slimmed.append(message)
            continue
        if not isinstance(payload, list) or not payload or not all(isinstance(item, dict) and "structure" in item for item in payload):
            slimmed.append(message)
            continue
        outlines = []
        for item in payload:
            outlines.append(f"Document: {item.get('document_name')}\nSummary: {item.get('summary', '')}\n" + "\n".join(_outline_nodes(item.get("structure") or [])))
        copy = dict(message)
        copy["content"] = "\n\n".join(outlines)
        slimmed.append(copy)
    return slimmed


def _summary_prompt(messages: List[Dict[str, Any]], archived_path: Path) -> List[Dict[str, str]]:
    transcript = json.dumps(messages, ensure_ascii=False)
    return [
        {
            "role": "system",
            "content": get_prompt("context_compaction.summary_system", summary_sections=", ".join(SUMMARY_SECTIONS)),
        },
        {"role": "user", "content": get_prompt("context_compaction.summary_user", archived_path=archived_path, transcript=transcript)},
    ]


def _message_groups(messages: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    groups: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    for message in messages:
        current.append(message)
        if message.get("role") == "assistant":
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def _split_json_messages(messages: List[Dict[str, Any]], chunk_chars: int) -> List[List[Dict[str, Any]]]:
    chunks: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    current_size = 0
    for group in _message_groups(messages):
        encoded_size = len(json.dumps(group, ensure_ascii=False))
        if current and current_size + encoded_size > chunk_chars:
            chunks.append(current)
            current = []
            current_size = 0
        current.extend(group)
        current_size += encoded_size
    if current:
        chunks.append(current)
    return chunks


def _summarize_chunk(settings: Dict[str, Any], chunk: List[Dict[str, Any]], archived: Path, depth: int = 0) -> str:
    try:
        return completion(settings, _summary_prompt(chunk, archived), max_tokens=1200, temperature=0).text
    except Exception:
        if depth >= 3 or len(chunk) <= 1:
            return _deterministic_summary(chunk, archived)
        mid = max(1, len(chunk) // 2)
        left = _summarize_chunk(settings, chunk[:mid], archived, depth + 1)
        right = _summarize_chunk(settings, chunk[mid:], archived, depth + 1)
        return left + "\n\n" + right


def _recent_turn_index(messages: List[Dict[str, Any]], keep_turns: int) -> int:
    groups = _message_groups(messages)
    keep_groups = max(1, keep_turns)
    if len(groups) <= keep_groups:
        return max(0, len(messages) - keep_groups)
    return sum(len(group) for group in groups[:-keep_groups])


def compact_messages(
    settings: Dict[str, Any],
    log_dir: Path,
    messages: List[Dict[str, Any]],
    keep_recent: int = 2,
    chunk_chars: int = 60000,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if len(_message_groups(messages)) <= keep_recent + 1:
        return messages, {"compacted": False, "reason": "not_enough_messages"}
    archived = archive_messages(log_dir, messages)
    split_at = _recent_turn_index(messages, keep_recent)
    old_messages = _slim_directory_observations(messages[:split_at])
    recent = messages[split_at:]
    try:
        chunks = _split_json_messages(old_messages, chunk_chars)
        with ThreadPoolExecutor(max_workers=min(4, max(1, len(chunks)))) as pool:
            summaries = list(pool.map(lambda chunk: _summarize_chunk(settings, chunk, archived), chunks))
        if len(summaries) == 1:
            summary = summaries[0]
        else:
            merge_prompt = [
                {"role": "system", "content": get_prompt("context_compaction.merge_system")},
                {"role": "user", "content": "\n\n---\n\n".join(summaries)},
            ]
            summary = completion(settings, merge_prompt, max_tokens=1600, temperature=0).text
    except Exception:
        summary = _deterministic_summary(old_messages, archived)
    continuation = {
        "role": "user",
        "content": get_prompt("context_compaction.continuation_user", archived_path=archived, summary=summary),
    }
    return [continuation] + recent, {"compacted": True, "summary": summary, "archived_path": str(archived)}


def needs_compaction(messages: List[Dict[str, Any]], context_window: int) -> bool:
    return estimate_messages_tokens(messages) >= int(context_window * 0.95)
