import asyncio
import json
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Dict, List

from .context_compact import compact_messages, needs_compaction
from .llm import completion, has_api_key
from .prompts import get_prompt, get_react_tools
from .schemas import DirectoryToolArgs, PageContentToolArgs, SearchToolArgs, validate_model

Event = Dict[str, Any]


REACT_TOOLS: List[Dict[str, Any]] = get_react_tools()


class AgentTools:
    def __init__(self, docs: List[Dict[str, Any]], searcher: Any, page_collector: Callable[[List[Dict[str, Any]], List[Dict[str, Any]], int], List[Dict[str, Any]]]):
        self.docs = docs
        self.searcher = searcher
        self.page_collector = page_collector

    def _selected_docs(self, names: List[str]) -> List[Dict[str, Any]]:
        if not names:
            return self.docs
        wanted = set(names)
        selected = [doc for doc in self.docs if doc.get("name") in wanted or doc.get("original_name") in wanted]
        return selected or self.docs

    def _find_node(self, nodes: List[Dict[str, Any]], node_id: str) -> Dict[str, Any] | None:
        for node in nodes:
            if node.get("node_id") == node_id:
                return node
            found = self._find_node(node.get("nodes", []), node_id)
            if found:
                return found
        return None

    def get_directory_structure(self, document_names: List[str], target: str = "ALL", node_id: str = "") -> List[Dict[str, Any]]:
        output = []
        for doc in self._selected_docs(document_names):
            structure = doc.get("structure", [])
            selected: Any = structure
            if node_id:
                selected = self._find_node(structure, node_id) or []
            elif target and target != "ALL":
                try:
                    idx = int(target) - 1
                    selected = [structure[idx]] if 0 <= idx < len(structure) else []
                except ValueError:
                    selected = structure
            output.append({"document_name": doc.get("name"), "summary": doc.get("summary", ""), "structure": selected})
        return output

    def search(self, document_names: List[str], query: str, top_k: int = 8) -> List[Dict[str, Any]]:
        selected_ids = {doc["id"] for doc in self._selected_docs(document_names)}
        results = self.searcher.search(query, top_k=max(1, min(12, int(top_k or 8))))
        return [item for item in results if item.get("document_id") in selected_ids]

    def get_page_content(self, document_names: List[str], pages: List[int]) -> List[Dict[str, Any]]:
        selected = self._selected_docs(document_names)
        wanted = set(int(p) for p in (pages or [])[:12] if int(p) > 0)
        output = []
        for doc in selected:
            for page in doc.get("pages", []):
                if page.get("page") in wanted:
                    text = (page.get("content") or "").strip()
                    output.append({
                        "document_id": doc["id"],
                        "document_name": doc["name"],
                        "page": page["page"],
                        "content": text[:2400] + ("..." if len(text) > 2400 else ""),
                    })
        return output

    def call(self, name: str, args: Dict[str, Any]) -> Any:
        if name == "get_directory_structure":
            payload = validate_model(DirectoryToolArgs, args)
            return self.get_directory_structure(payload.document_names, payload.target, payload.node_id)
        if name == "search":
            payload = validate_model(SearchToolArgs, args)
            return self.search(payload.document_names, payload.query, payload.top_k)
        if name == "get_page_content":
            payload = validate_model(PageContentToolArgs, args)
            return self.get_page_content(payload.document_names, payload.pages)
        return {"error": f"Unknown tool: {name}", "recoverable": True, "next_steps": ["Use a declared tool name."]}


def _assistant_tool_message(text: str, tool_calls: List[Any], protocol: str) -> Dict[str, Any]:
    if protocol == "anthropic":
        blocks = []
        if text:
            blocks.append({"type": "text", "text": text})
        for call in tool_calls:
            blocks.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments})
        return {"role": "assistant", "content": blocks}
    return {
        "role": "assistant",
        "content": text or "",
        "tool_calls": [
            {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)}}
            for call in tool_calls
        ],
    }


def _tool_result_message(call_id: str, content: Any) -> Dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": json.dumps(content, ensure_ascii=False)}


def _clean_reason(text: str, fallback: str) -> str:
    reason = " ".join(str(text or "").strip().split())
    if reason:
        return reason[:280]
    return fallback


def _tool_reason(name: str) -> str:
    if name == "get_directory_structure":
        return "Inspecting the outline to understand the document structure before narrowing evidence."
    if name == "search":
        return "Searching with focused keywords to find the most relevant sections."
    if name == "get_page_content":
        return "Reading a tight page range to verify the evidence before answering."
    return "Using a retrieval tool to gather document evidence."


def _tool_call_event(step: int, remaining_steps: int, tool_id: str, name: str, args: Dict[str, Any], reason: str) -> Event:
    clean = _clean_reason(reason, _tool_reason(name))
    return {
        "event": "tool_call",
        "data": {
            "step": step,
            "remaining_steps": remaining_steps,
            "title": clean,
            "reason": clean,
            "tool": {"id": tool_id, "name": name, "input": args, "reason": clean},
        },
    }


async def _offline_stream(question: str, docs: List[Dict[str, Any]], tools: AgentTools, max_steps: int, conversation_context: str = "") -> AsyncGenerator[Event, None]:
    yield {"event": "status", "data": {"message": "Agent loop started.", "mode": "react", "step_budget": max_steps, "offline": True}}
    names = [doc["name"] for doc in docs]
    directory = tools.get_directory_structure(names, "ALL")
    yield _tool_call_event(1, max_steps - 1, "offline_directory", "get_directory_structure", {"document_names": names, "target": "ALL"}, _tool_reason("get_directory_structure"))
    yield {"event": "observation", "data": {"step": 1, "title": "Observation 1: directory", "observations": directory}}
    await asyncio.sleep(0.02)
    retrieval_query = question
    if conversation_context:
        retrieval_query = f"Recent chat context:\n{conversation_context[-3000:]}\n\nCurrent question:\n{question}"
    results = tools.search(names, retrieval_query, 8)
    yield _tool_call_event(2, max_steps - 2, "offline_search", "search", {"document_names": names, "query": retrieval_query, "top_k": 8}, _tool_reason("search"))
    yield {"event": "observation", "data": {"step": 2, "title": "Observation 2: search", "observations": results}}
    pages = sorted({p for item in results for p in range(item.get("pages", [1, 1])[0], item.get("pages", [1, 1])[1] + 1)})[:12]
    page_content = tools.get_page_content(names, pages)
    yield _tool_call_event(3, max_steps - 3, "offline_pages", "get_page_content", {"document_names": names, "pages": pages}, _tool_reason("get_page_content"))
    yield {"event": "observation", "data": {"step": 3, "title": "Observation 3: page content", "observations": page_content}}
    answer = _local_final_answer(question, results, page_content)
    yield {"event": "final", "data": {"answer": answer, "sections": [r.get("section_path") for r in results[:5]], "pages": pages, "trace": [{"type": "offline_react", "count": 3}]}}


def _local_final_answer(question: str, results: List[Dict[str, Any]], pages: List[Dict[str, Any]]) -> str:
    if not results:
        return f"No directly relevant evidence was found for \"{question}\". Try a more specific query."
    lines = [f"Based on the available document evidence, here is what I found for \"{question}\":\n"]
    for result in results[:5]:
        pr = result.get("pages", [None, None])
        lines.append(f"- **{result.get('document_name')} Page {pr[0]}-{pr[1]} / {result.get('section_path')}**: {result.get('snippet')}")
    if pages:
        lines.append("\nReferenced pages:")
        for page in pages[:4]:
            text = (page.get("content") or "").replace("\n", " ")
            lines.append(f"- {page.get('document_name')} Page {page.get('page')}: {text[:280]}")
    return "\n".join(lines)


async def react_event_stream(
    question: str,
    docs: List[Dict[str, Any]],
    searcher: Any,
    settings: Dict[str, Any],
    log_dir: Path,
    page_collector: Callable[[List[Dict[str, Any]], List[Dict[str, Any]], int], List[Dict[str, Any]]],
    conversation_context: str = "",
) -> AsyncGenerator[Event, None]:
    max_steps = max(10, min(100, int(settings.get("step_budget", 50))))
    max_output_tokens = max(512, min(40000, int(settings.get("max_output_tokens", 3072))))
    tools = AgentTools(docs, searcher, page_collector)
    if not has_api_key(settings):
        async for event in _offline_stream(question, docs, tools, max_steps, conversation_context):
            yield event
        return

    messages: List[Dict[str, Any]] = [{"role": "system", "content": get_prompt("react.system")}]
    if conversation_context:
        messages.append({"role": "user", "content": f"Conversation context from this chat session:\n{conversation_context}"})
    messages.append({"role": "user", "content": get_prompt("react.user_question", question=question, documents=", ".join(doc["name"] for doc in docs))})
    yield {"event": "status", "data": {"message": "Agent loop started.", "mode": "react", "step_budget": max_steps}}
    protocol = str(settings.get("api_protocol") or "openai")
    consecutive_compaction_failures = 0
    directory_checked = False
    outline_corrections = 0
    for step in range(1, max_steps + 1):
        if max_steps - step <= 10:
            messages.append({"role": "system", "content": get_prompt("react.budget_warning", completed_steps=step - 1, remaining_steps=max_steps - step + 1)})
        if needs_compaction(messages, int(settings.get("context_window", 8000))):
            try:
                messages, info = await asyncio.to_thread(compact_messages, settings, log_dir, messages)
                consecutive_compaction_failures = 0
                if info.get("compacted"):
                    yield {"event": "context_compaction", "data": {"title": "Compacting conversation context", **info}}
            except Exception as exc:
                consecutive_compaction_failures += 1
                yield {"event": "context_compaction", "data": {"title": "Context compaction failed", "error": str(exc), "recoverable": consecutive_compaction_failures <= 3}}
                if consecutive_compaction_failures > 3:
                    break
        try:
            response = await asyncio.to_thread(completion, settings, messages, tools=REACT_TOOLS, max_tokens=max_output_tokens, temperature=0.1)
        except Exception as exc:
            if "context" in str(exc).lower() or "token" in str(exc).lower():
                try:
                    messages, info = await asyncio.to_thread(compact_messages, settings, log_dir, messages)
                    yield {"event": "context_compaction", "data": {"title": "Model context exceeded; retrying after compaction", **info}}
                    response = await asyncio.to_thread(completion, settings, messages, tools=REACT_TOOLS, max_tokens=max_output_tokens, temperature=0.1)
                except Exception as retry_exc:
                    yield {"event": "error", "data": {"error": str(retry_exc), "recoverable": True, "next_steps": ["Lower the context window, select fewer documents, or use a model with a larger context window."]}}
                    return
            else:
                yield {"event": "error", "data": {"error": str(exc), "recoverable": True, "next_steps": ["Check the API key, model name, API URL, and network connection."]}}
                return

        if response.tool_calls:
            messages.append(_assistant_tool_message(response.text, response.tool_calls, protocol))
            if not directory_checked and not any(call.name == "get_directory_structure" for call in response.tool_calls):
                outline_corrections += 1
                if outline_corrections > 3:
                    yield {"event": "error", "data": {"error": "The model repeatedly skipped mandatory outline inspection before retrieval.", "recoverable": True, "next_steps": ["Retry the question or switch to standard mode."]}}
                    return
                reminder = "Before search or page reads, inspect the outline with get_directory_structure for the selected documents. Then continue with focused retrieval."
                messages.append({"role": "system", "content": reminder})
                yield {"event": "status", "data": {"message": "Outline inspection required before other retrieval tools.", "title": "Inspecting outline first"}}
                continue
            for call in response.tool_calls:
                reason = _clean_reason(response.text, _tool_reason(call.name))
                yield _tool_call_event(step, max_steps - step, call.id, call.name, call.arguments, reason)
                try:
                    observation = tools.call(call.name, call.arguments)
                except Exception as exc:
                    observation = {"error": str(exc), "recoverable": True, "next_steps": ["Adjust the tool arguments and retry."]}
                yield {"event": "observation", "data": {"step": step, "title": f"Observation {step}: {call.name}", "observations": observation}}
                messages.append(_tool_result_message(call.id, observation))
                if call.name == "get_directory_structure":
                    directory_checked = True
            await asyncio.sleep(0)
            continue

        answer = response.text.strip()
        if answer:
            yield {"event": "final", "data": {"answer": answer, "sections": [], "pages": [], "trace": [{"type": "react_steps", "count": step}]}}
            return
        messages.append({"role": "user", "content": get_prompt("react.finalize")})

    messages.append({"role": "user", "content": get_prompt("react.step_budget_exhausted")})
    if needs_compaction(messages, int(settings.get("context_window", 8000))):
        try:
            messages, info = await asyncio.to_thread(compact_messages, settings, log_dir, messages)
            if info.get("compacted"):
                yield {"event": "context_compaction", "data": {"title": "Compacting context before final answer", **info}}
        except Exception as exc:
            yield {"event": "context_compaction", "data": {"title": "Final-answer context compaction failed", "error": str(exc), "recoverable": True}}
    try:
        response = await asyncio.to_thread(completion, settings, messages, tools=None, max_tokens=max_output_tokens, temperature=0.1)
        yield {"event": "final", "data": {"answer": response.text or "The step budget was reached and the model did not return an answer.", "sections": [], "pages": [], "trace": [{"type": "step_budget_exhausted", "count": max_steps}]}}
    except Exception as exc:
        yield {"event": "error", "data": {"error": str(exc), "recoverable": False}}
