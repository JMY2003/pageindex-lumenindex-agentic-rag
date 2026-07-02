import math
import re
import threading
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Sequence, Tuple

CJK_RE = re.compile(r"[\u3400-\u9fff]+|[A-Za-z0-9][A-Za-z0-9_-]*")
STOP_WORDS = {
    "\u4ec0\u4e48",
    "\u5982\u4f55",
    "\u600e\u4e48",
    "\u662f\u5426",
    "\u53ef\u4ee5",
    "\u8fdb\u884c",
    "\u4ee5\u53ca",
    "\u610f\u601d",
    "\u662f\u4ec0\u4e48",
    "\u662f\u4ec0\u4e48\u610f\u601d",
    "\u5305\u542b\u54ea\u4e9b",
    "\u4e3a\u4ec0\u4e48",
    "\u54ea\u4e9b",
    "\u76f8\u5173",
    "\u8bf7\u95ee",
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
}
QUESTION_PARTS = (
    "\u662f\u4ec0\u4e48\u610f\u601d",
    "\u662f\u4ec0\u4e48",
    "\u5305\u542b\u54ea\u4e9b",
    "\u6709\u54ea\u4e9b",
    "\u4e3a\u4ec0\u4e48",
    "\u662f\u4ec0\u4e48",
    "\u4ec0\u4e48",
    "\u5982\u4f55",
    "\u600e\u4e48",
    "\u662f\u5426",
    "\u8bf7\u95ee",
    "\u76f8\u5173",
)


def terms(text: str) -> List[str]:
    raw = [m.group(0).lower() for m in CJK_RE.finditer(text or "")]
    expanded: List[str] = []
    for token in raw:
        if token in STOP_WORDS:
            continue
        if re.fullmatch(r"[\u3400-\u9fff]+", token):
            for part in QUESTION_PARTS:
                token = token.replace(part, "")
            if not token or token in STOP_WORDS:
                continue
        expanded.append(token)
        if re.fullmatch(r"[\u3400-\u9fff]+", token) and len(token) >= 4:
            for n in (2, 3, 4):
                if len(token) >= n:
                    expanded.extend(token[i : i + n] for i in range(len(token) - n + 1))
    return expanded


def flatten_nodes(nodes: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    def walk(items: Sequence[Dict[str, Any]], trail: List[str]) -> None:
        for item in items:
            path = trail + [item.get("title", "")]
            copy = dict(item)
            copy["section_path"] = " / ".join([p for p in path if p])
            out.append(copy)
            walk(item.get("nodes") or item.get("children") or [], path)
    walk(nodes, [])
    return out


class LexicalSearch:
    def __init__(self, documents: List[Dict[str, Any]]):
        self.rows: List[Dict[str, Any]] = []
        for doc in documents:
            pages_by_num = {p["page"]: p.get("content", "") for p in doc.get("pages", [])}
            for node in flatten_nodes(doc.get("structure", [])):
                start, end = (node.get("pages") or [node.get("start_index", 1), node.get("end_index", 1)])[:2]
                page_text = "\n".join(pages_by_num.get(page, "") for page in range(int(start), int(end) + 1))
                content = f"{node.get('title', '')}\n{node.get('summary', '')}\n{page_text}"
                row_terms = terms(content)
                self.rows.append({
                    "document_id": doc["id"],
                    "document_name": doc["name"],
                    "node_id": node.get("node_id", ""),
                    "title": node.get("title", ""),
                    "section_path": node.get("section_path", node.get("title", "")),
                    "pages": [int(start), int(end)],
                    "content": page_text,
                    "search_text": content,
                    "lower_text": content.lower(),
                    "term_counts": Counter(row_terms),
                    "length": max(1, len(row_terms)),
                })
        self.avg_len = sum(r["length"] for r in self.rows) / max(1, len(self.rows))
        self.df = defaultdict(int)
        for row in self.rows:
            for token in row["term_counts"]:
                self.df[token] += 1

    def search(self, query: str, top_k: int = 8) -> List[Dict[str, Any]]:
        q_terms = terms(query)
        if not q_terms:
            return []
        q_primary = list(dict.fromkeys(q_terms[:20]))
        phrase = (query or "").lower().strip()
        candidates = []
        n_docs = max(1, len(self.rows))
        for row in self.rows:
            counts = row["term_counts"]
            matched = [t for t in q_primary if counts.get(t)]
            lower_text = row["lower_text"]
            if not matched and phrase not in lower_text:
                continue
            title_terms = Counter(terms(row["title"]))
            tf_score = sum((counts[t] + title_terms[t]) * (8 if len(t) >= 5 else 3) + (5 if "-" in t or "_" in t else 0) for t in matched)
            bm25 = 0.0
            for t in q_primary:
                if not counts.get(t):
                    continue
                df = self.df.get(t, 0)
                idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
                freq = counts[t]
                bm25 += idf * (freq * 2.2) / (freq + 1.2 * (1 - 0.75 + 0.75 * row["length"] / self.avg_len))
            title_lower = row["title"].lower()
            phrase_score = 0
            if phrase and phrase == title_lower:
                phrase_score += 800
            elif phrase and phrase in title_lower:
                phrase_score += 300
            if phrase and phrase in lower_text:
                phrase_score += 500
            if _ordered_match(lower_text, q_primary):
                phrase_score += 200
            coverage = len(set(matched)) / max(1, len(set(q_primary)))
            title_score = 80 if any(t in title_lower for t in matched) else 0
            proximity = _proximity_quality(lower_text, q_primary)
            blended = {
                "tf_bm25": tf_score + bm25 * 30,
                "bm25_tf": bm25 * 45 + tf_score * 0.7,
                "quality": coverage * 120 + phrase_score + title_score + proximity * 160,
            }
            snippet = make_snippet(row["content"], q_primary)
            candidates.append({"row": row, "matched": matched, "snippet": snippet, "scores": blended, "proximity": proximity, "coverage": coverage})
        ranked_lists = []
        for key in ("tf_bm25", "bm25_tf", "quality"):
            ranked_lists.append(sorted(candidates, key=lambda item: item["scores"][key], reverse=True))
        rrf_scores = defaultdict(float)
        for ranked in ranked_lists:
            for rank, item in enumerate(ranked, 1):
                rrf_scores[id(item)] += 1.0 / (60 + rank)
        candidates.sort(key=lambda item: (rrf_scores[id(item)], item["scores"]["quality"], item["scores"]["tf_bm25"]), reverse=True)
        per_node = defaultdict(int)
        results = []
        for item in candidates:
            row = item["row"]
            matched = item["matched"]
            snippet = item["snippet"]
            key = (row["document_id"], row["node_id"])
            if per_node[key] >= 2:
                continue
            per_node[key] += 1
            results.append({
                "rank": len(results) + 1,
                "document_id": row["document_id"],
                "document_name": row["document_name"],
                "node_id": row["node_id"],
                "title": row["title"],
                "section_path": row["section_path"],
                "pages": row["pages"],
                "score": round(float(rrf_scores[id(item)] * 10000), 3),
                "matched_terms": matched[:8],
                "coverage": round(float(item["coverage"]), 3),
                "proximity": round(float(item["proximity"]), 3),
                "snippet": snippet,
            })
            if len(results) >= top_k:
                break
        return results


def _ordered_match(text: str, query_terms: List[str]) -> bool:
    pos = -1
    matched = 0
    for term in query_terms:
        idx = text.find(term.lower(), pos + 1)
        if idx >= 0:
            matched += 1
            pos = idx
    return matched >= min(3, max(1, len(set(query_terms)) // 2))


def _proximity_quality(text: str, query_terms: List[str]) -> float:
    postings = []
    for term_index, term in enumerate(list(dict.fromkeys(query_terms))[:10]):
        starts = [m.start() for m in re.finditer(re.escape(term.lower()), text)]
        for pos in starts[:20]:
            postings.append((pos, term_index))
    if len(postings) <= 1:
        return 0.0
    required = len(set(idx for _, idx in postings))
    if required <= 1:
        return 0.0
    postings.sort()
    counts = defaultdict(int)
    have = 0
    left = 0
    best = None
    for right, (pos, idx) in enumerate(postings):
        if counts[idx] == 0:
            have += 1
        counts[idx] += 1
        while have == required and left <= right:
            width = postings[right][0] - postings[left][0]
            best = width if best is None else min(best, width)
            left_idx = postings[left][1]
            counts[left_idx] -= 1
            if counts[left_idx] == 0:
                have -= 1
            left += 1
    if best is None:
        return 0.0
    return 1.0 / (1.0 + max(0, best) / 20.0)


class SearchIndexCache:
    def __init__(self, max_entries: int = 12):
        self.max_entries = max_entries
        self.lock = threading.RLock()
        self._cache: Dict[Tuple[Tuple[str, str, int], ...], Tuple[float, LexicalSearch]] = {}

    @staticmethod
    def signature(documents: List[Dict[str, Any]]) -> Tuple[Tuple[str, str, int], ...]:
        return tuple(
            sorted(
                (
                    doc.get("id", ""),
                    str(doc.get("fingerprint") or doc.get("index_version") or ""),
                    int(doc.get("page_count") or 0),
                )
                for doc in documents
            )
        )

    def get(self, documents: List[Dict[str, Any]]) -> LexicalSearch:
        import time

        key = self.signature(documents)
        with self.lock:
            item = self._cache.get(key)
            if item:
                self._cache[key] = (time.time(), item[1])
                return item[1]
        searcher = LexicalSearch(documents)
        with self.lock:
            self._cache[key] = (time.time(), searcher)
            if len(self._cache) > self.max_entries:
                oldest = min(self._cache.items(), key=lambda kv: kv[1][0])[0]
                self._cache.pop(oldest, None)
        return searcher

    def clear(self) -> None:
        with self.lock:
            self._cache.clear()


def make_snippet(text: str, query_terms: Iterable[str], limit: int = 360) -> str:
    clean = re.sub(r"\s+", " ", text or "").strip()
    if len(clean) <= limit:
        return clean
    lower = clean.lower()
    positions = [lower.find(t.lower()) for t in query_terms if lower.find(t.lower()) >= 0]
    start = max(0, min(positions) - 80) if positions else 0
    snippet = clean[start : start + limit]
    return ("..." if start else "") + snippet + ("..." if start + limit < len(clean) else "")
