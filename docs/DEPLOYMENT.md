# Deployment Guide

## Local Conda Environment

```bash
conda env create -f environment.yml
conda activate lumenindex
pip install -r requirements.txt
python run_web.py
```

Open `http://127.0.0.1:8765`.

For machines that already have the `lumenindex` Conda environment, update it with:

```bash
conda activate lumenindex
pip install --upgrade -r requirements.txt
```

The first registered user claims any legacy local documents and conversations. After that, uploaded documents and chat histories are scoped to the signed-in user.

## Docker

```bash
docker build -t lumenindex .
docker run --rm -p 8765:8765 \
  -v "$PWD/uploads:/app/uploads" \
  -v "$PWD/cache:/app/cache" \
  -v "$PWD/logs:/app/logs" \
  -v "$PWD/pageindex_web.sqlite3:/app/pageindex_web.sqlite3" \
  lumenindex
```

The image includes LibreOffice Writer and Noto CJK fonts for `.doc` conversion and Chinese text rendering.

## Docker Compose

```bash
touch app_settings.json pageindex_web.sqlite3
docker compose up -d --build
```

Compose mounts `uploads/`, `cache/`, `logs/`, `app_settings.json`, and `pageindex_web.sqlite3` for persistence. On macOS it also mounts `${HOME}/.zshrc` read-only into the container so Qwen/OpenAI-compatible fallback variables can be discovered when the app settings fields are empty.

## Configuration

Use the in-app settings panel as the primary configuration source. Values saved there are stored in `app_settings.json` and take priority over shell or system environment variables.

Fallback variables, used only when no app setting has been saved for that field:

- `PAGEINDEX_MAX_UPLOAD_MB`: per-file upload limit.
- `PAGEINDEX_INDEX_WORKERS`: indexing thread pool size.
- `PAGEINDEX_API_PROTOCOL`: `openai` or `anthropic`.
- `PAGEINDEX_MODEL`: model name.
- `PAGEINDEX_CONTEXT_WINDOW`: context compaction threshold.
- `PAGEINDEX_MAX_OUTPUT_TOKENS`: max LLM output tokens per call.
- `PAGEINDEX_STEP_BUDGET`: max ReAct tool-loop steps.
- `PAGEINDEX_DEEP_THINKING`: default browser QA mode.
- `PAGEINDEX_CONTEXT_ENABLED`: conversation-history persistence switch.
- `LLM_MAX_RETRIES`: retry count for LLM calls.
- `QWEN_API_KEY`, `QWEN_BASE_URL`, `QWEN_MODEL`: Qwen OpenAI-compatible API fallback.
- `DASHSCOPE_API_KEY`, `DASHSCOPE_API_BASE`, `DASHSCOPE_MODEL`: DashScope alias fallback.

On macOS, the app also reads matching `export ...=...` entries from `~/.zshrc`. On Windows and Linux, it reads system environment variables. The settings file is ignored by git and written with `0600` permissions when possible.

## Runtime Data

- `uploads/`: original uploaded files.
- `cache/`: derived metadata, pages, task JSON, and per-document caches.
- `logs/server8765.out.log`: application log.
- `logs/server8765.err.log`: warning and error log.
- `logs/context_compactions/`: archived pre-compaction conversations.
- `pageindex_web.sqlite3`: queryable runtime state for users, sessions, documents, tasks, conversations, and settings.

Back up all runtime data directories and the SQLite database together.

## Document Conversion Notes

- PDF uses PyMuPDF extraction and layout heuristics.
- DOCX uses native XML extraction on all platforms.
- DOC uses LibreOffice conversion when available.
- On Windows with Microsoft Word installed, DOCX true pagination can use Word COM through PowerShell. Non-Windows systems fall back to deterministic XML paragraph paging.

## Production Checklist

1. Set upload limit and index worker count for machine capacity.
2. Mount `uploads`, `cache`, `logs`, and `pageindex_web.sqlite3` on persistent storage.
3. Configure reverse proxy TLS and request body limits.
4. Set API key through the settings panel, or use environment variables only as fallback.
5. Run `GET /api/health` after deployment.
6. Upload a known PDF/DOCX/Markdown file and verify structure extraction.
7. Verify `logs/server8765.err.log` remains quiet after a smoke query.
