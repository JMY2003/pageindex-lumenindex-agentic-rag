# Feature Coverage

This file maps the requested PageIndex web app capabilities to implementation areas.

## Implemented

- FastAPI backend: `run_web.py`, `pageindex_web/main.py`.
- Apple liquid-glass style SPA: `pageindex_web/static/`.
- Isolated dependency set: project-level `environment.yml` creates the `lumenindex` Conda environment, then `requirements.txt` installs the Python web/runtime dependencies.
- User accounts: local registration, login, logout, HTTP-only session cookie, and per-user document/conversation visibility.
- Upload management: multi-file upload, suffix validation, size limit, SHA-256 duplicate detection.
- Browser-side SHA-256 preflight: the SPA computes a file hash with SubtleCrypto and checks `/api/cache/{hash}` before uploading bytes.
- Persistent document state: JSON manifest plus SQLite mirror.
- Persistent task state: JSON task file plus SQLite mirror.
- Cooperative cancellation: cancel endpoint and cancellation checks in indexing.
- Cache reuse: fingerprint-based duplicate detection and in-memory search index signature cache.
- Cache API: `/api/cache/{hash}` checks reusable indexes and `/api/cache/load` returns a cached document without re-uploading.
- Cache version validation: fingerprint reuse requires current schema, index version, PDF local index version, metadata, and gzip page content.
- Legacy cache migration: single-file JSON caches are promoted to split metadata plus gzip pages and the old file is renamed with `.migrated`.
- PDF indexing: page extraction, repeated header/footer filtering, TOC-page skip, bookmark destination top ratio, normalized bookmark levels, heading detection, merged heading lines, nested section tree, and deterministic large-node refinement from layout headings.
- DOCX indexing: XML extraction with `lastRenderedPageBreak`/page break support, heading detection, fallback paragraph paging, optional Windows Word COM page extraction with image placeholders.
- DOCX LLM TOC fallback: when heading styles are insufficient and an API key is configured, the indexer asks the model for a structured TOC before page chunk fallback.
- DOC indexing: LibreOffice conversion path.
- Markdown indexing: heading-aware section tree, fenced-code heading protection, and token-aware thinning for highly fragmented tiny leaf sections.
- Search: hybrid lexical ranking, phrase boost, CJK query cleanup, coverage, minimal-window proximity, title weight, and Reciprocal Rank Fusion.
- Standard QA: LLM node selection over the directory tree, page collection, answer synthesis, and deterministic lexical fallback.
- ReAct QA: real tool-loop for OpenAI/Anthropic tool calling, offline deterministic loop without API key, step budget, budget warning, structured observations.
- Context compaction: token estimate, archive to `logs/context_compactions`, directory-observation slimming, LLM summary with deterministic fallback, recursive split fallback, and final-answer pre-compaction.
- LLM shim: OpenAI-compatible and Anthropic-compatible HTTP clients without requiring LiteLLM, token counter, retry env, and async wrapper.
- Settings: local settings file with priority over macOS zshrc and system environment variables, Qwen/DashScope OpenAI-compatible aliases, SQLite mirror, secret masking in API, max output tokens, context window K, deep-thinking default, and conversation-history toggle.
- Conversations: standard and ReAct sessions saved to SQLite and scoped to the signed-in user.
- Prompt management: all model-facing prompt templates and ReAct tool descriptions are centralized in `pageindex_web/prompts.yaml`; code reads them through `pageindex_web/prompts.py`.
- Structured error responses: API details and SSE recoverability hints.
- Worker isolation: indexing is executed in a child Python process, while the FastAPI process supervises progress, cancellation, and final atomic cache promotion.
- Logs: rotating app and error logs.
- Delivery artifacts: Dockerfile, Docker Compose, API docs, and deployment docs.

## External Conditions

- Model-backed ReAct requires a valid OpenAI-compatible or Anthropic API key.
- Windows Word COM true pagination requires Windows plus Microsoft Word installed.
- `.doc` conversion requires LibreOffice in `PATH`; the Docker image includes it.
- PDF layout fidelity depends on the source PDF having extractable text. Scanned image-only PDFs need OCR, which is outside this implementation.

## Verification Targets

- Python compile check.
- Unit tests for indexing, search, persistence, context compaction, and offline ReAct.
- JavaScript syntax check.
- Local HTTP smoke: health, upload, progress, structure, standard QA, streaming QA, reindex, delete.
