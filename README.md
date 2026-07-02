# LumenIndex

**A production-ready PageIndex-inspired document QA system for vectorless RAG, long-document retrieval, and multi-user AI workspaces.**

LumenIndex is a FastAPI web application deeply optimized from the open-source **PageIndex** architecture. It keeps the PageIndex idea of reasoning over document structure instead of relying on vector embeddings, then adds a delivery-oriented backend, persistent ChatGPT-style sessions, multi-document retrieval, user isolation, context compaction, Docker deployment, and a liquid-glass web interface.

If you are searching for **PageIndex web app**, **pageindex FastAPI**, **PageIndex RAG**, **vectorless RAG**, or **reasoning-based document retrieval**, LumenIndex is designed to be a practical, self-hostable implementation for those workflows.

## Why LumenIndex

- **Vectorless RAG over document structure**: uses outlines, page ranges, section paths, lexical evidence, and agentic tool use instead of requiring embeddings or a vector database.
- **Built for real users, not a demo**: local accounts, HTTP-only sessions, per-user documents, per-user chat history, admin management, and asset isolation.
- **ChatGPT-style conversation history**: one conversation is one persistent history, with selected documents and context preserved across turns.
- **Automatic context compaction**: long ReAct sessions are summarized and archived when approaching the configured context window.
- **Multi-document retrieval**: ask over multiple PDFs, DOC/DOCX files, and Markdown documents in one chat.
- **Structured LLM outputs**: model-generated JSON is parsed and validated with Pydantic before use.
- **Robust indexing pipeline**: child-process indexing, cooperative cancellation, reindex progress, cache versioning, duplicate detection, and failure recovery.
- **OpenAI-compatible providers**: configure OpenAI-style APIs such as Qwen/DashScope from the app settings panel, with environment variable fallback.
- **Production deployment path**: Conda environment, Dockerfile, Docker Compose, API docs, deployment docs, tests, and health checks.

## How It Relates To PageIndex

LumenIndex is **based on and deeply optimized from the open-source PageIndex architecture**, but it is not a thin wrapper around the original PageIndex CLI/SDK.

It keeps the core PageIndex principles:

- build a hierarchical document index;
- reason over section structure before reading pages;
- retrieve tight evidence ranges;
- answer from cited document evidence;
- avoid vector database complexity for many document QA workflows.

It then adds the web-system features needed for delivery:

| Area | Open-source PageIndex style | LumenIndex |
| --- | --- | --- |
| Interface | CLI / SDK examples | Full FastAPI + browser app |
| Retrieval | Single-document agent demo | Multi-document standard and ReAct QA |
| Storage | Workspace JSON examples | SQLite + JSON/cache mirror |
| Users | Not the focus | Login, admin, user asset isolation |
| Sessions | Not ChatGPT-style persistent chat | Persistent conversations with context |
| Context | Basic agent flow | Automatic context compaction and archives |
| Operations | Demo scripts | Docker Compose, health check, logs, tests |
| LLM JSON | Loose parsing in examples | Pydantic-validated structured outputs |

## Screens And UX

The app is designed as a document workspace rather than a landing page:

- left side: conversation history and documents;
- center: chat window;
- right side: collapsible document outline;
- drag-and-drop files into the chat or document panel;
- attach document tags to a chat;
- stream ReAct trace above each assistant answer;
- render full Markdown, GitHub-flavored tables, and math formulas.

## Supported Documents

- PDF
- DOCX
- DOC through LibreOffice conversion
- Markdown

PDF indexing uses bookmark extraction, layout heading detection, repeated header/footer filtering, TOC-page detection, and deterministic large-node refinement. Markdown indexing protects fenced code blocks and performs token-aware thinning for fragmented tiny sections.

## Quick Start

```bash
conda env create -f environment.yml
conda activate lumenindex
pip install -r requirements.txt
python run_web.py
```

Open `http://127.0.0.1:8765`.

If the environment already exists:

```bash
conda activate lumenindex
pip install --upgrade -r requirements.txt
python run_web.py
```

## Docker

```bash
touch app_settings.json pageindex_web.sqlite3
docker compose up -d --build
```

The Compose service is named `lumenindex` and exposes the app on port `8765`.

## Configuration

Use the in-app settings panel as the primary configuration source. Saved app settings take priority over macOS `~/.zshrc` or system environment variables.

Supported provider styles:

- OpenAI-compatible APIs
- Qwen / DashScope OpenAI-compatible endpoints
- Anthropic-compatible API mode

Useful fallback environment variables include:

- `OPENAI_API_KEY`, `OPENAI_BASE_URL`
- `QWEN_API_KEY`, `QWEN_BASE_URL`, `QWEN_MODEL`
- `DASHSCOPE_API_KEY`, `DASHSCOPE_API_BASE`, `DASHSCOPE_MODEL`
- `PAGEINDEX_MODEL`, `PAGEINDEX_CONTEXT_WINDOW`, `PAGEINDEX_STEP_BUDGET`

## Project Layout

```text
pageindex_web/              FastAPI backend and static web app
pageindex_web/prompts.yaml  Centralized system prompts and tool definitions
docs/API.md                 HTTP and SSE API reference
docs/DEPLOYMENT.md          Local, Docker, and production deployment notes
docs/FEATURE_COVERAGE.md    Implemented capability map
tests/                      Regression tests
Dockerfile                  Container image
docker-compose.yml          Self-hosted deployment
environment.yml             Conda environment
requirements.txt            Runtime dependencies
```

## Runtime Data

These paths are intentionally ignored by git and should be backed up together in production:

- `uploads/`
- `cache/`
- `logs/`
- `app_settings.json`
- `pageindex_web.sqlite3`

## Verification

```bash
node --check pageindex_web/static/app.js
conda run -n lumenindex python -m py_compile pageindex_web/main.py pageindex_web/agent.py pageindex_web/storage.py pageindex_web/db.py
conda run -n lumenindex python -m pytest -q tests/test_pageindex_web_core.py
```

## Suggested GitHub Description

```text
Production-ready PageIndex-inspired FastAPI web app for vectorless RAG, multi-document document QA, ReAct retrieval, context compaction, and self-hosted AI workspaces.
```

## Suggested GitHub Topics

Use these repository topics so users searching for PageIndex and related RAG terms can find the project:

```text
pageindex
pageindex-web
vectorless-rag
rag
document-qa
document-retrieval
fastapi
openai-compatible
qwen
react-agent
long-context
pdf-qa
self-hosted
```

## Attribution

LumenIndex is inspired by and deeply optimized from the open-source PageIndex architecture. See `NOTICE.md` for attribution details. PageIndex is an open-source project by Vectify AI.

## License

This project is distributed under the MIT License. See `LICENSE`.
