# LumenIndex API

Base URL: `http://127.0.0.1:8765`

## Health

`GET /api/health`

Returns service status, document count, cache size, upload limit, and indexing worker count.

## Authentication

`GET /api/auth/me`

Returns the currently signed-in user, or `null` when no valid session exists.

`POST /api/auth/register`

Creates a regular user. The body must include `username`, `password`, and `confirm_password`.

`POST /api/auth/register-admin`

Creates an admin user. The system allows at most three admin users.

`POST /api/auth/login`

Creates an HTTP-only session cookie for a valid username/password pair.

`POST /api/auth/logout`

Revokes the current session and clears the session cookie.

## Admin

Admin endpoints require an admin session. Admin users can manage regular users and regular-user assets, but cannot operate on other admin accounts or admin-owned assets.

`GET /api/admin/overview`

Returns users, documents, conversations, and pagination metadata for the management page.

`POST /api/admin/users`

Creates a regular user with the default password `{username}@123`.

`POST /api/admin/users/{user_id}/reset-password`

Resets a regular user's password to `{username}@123` without deleting history or documents.

`DELETE /api/admin/users/{user_id}`

Deletes a regular user and that user's assets.

`DELETE /api/admin/documents/{document_id}`

Deletes a regular-user document and related cache/source files.

`DELETE /api/admin/conversations/{conversation_id}`

Deletes a regular-user conversation.

## Documents

`POST /api/upload`

Multipart field: `files`. Supports `.pdf`, `.docx`, `.doc`, `.md`, `.markdown`.

`GET /api/documents`

Lists uploaded documents and cache statistics.

`GET /api/documents/{document_id}/structure`

Returns cached metadata, cache health details, and hierarchical structure.

`GET /api/documents/{document_id}/detail`

Returns document metadata, fingerprint, cache file paths and sizes, index/schema versions, cache freshness, and hierarchical structure.

`PUT /api/documents/{document_id}`

Body:

```json
{"name": "new-name.pdf"}
```

`POST /api/documents/{document_id}/reindex`

Starts a fresh indexing task for an existing source file.

`DELETE /api/documents/{document_id}`

Deletes source file, cache entries, and SQLite document metadata.

## Index Tasks

`GET /api/index/progress/{task_id}`

Returns task status, progress, message, error, and cancellation state.

`POST /api/index/cancel/{task_id}`

Requests cooperative task cancellation. In-flight indexers check cancellation between work units.

## Question Answering

`POST /api/ask/standard`

Body:

```json
{
  "question": "Question",
  "document_ids": ["doc_x"]
}
```

Runs hybrid retrieval and a single answer synthesis call. If no API key is configured, returns deterministic evidence-based fallback text.

`POST /api/ask/stream`

Server-Sent Events ReAct loop. Events:

- `status`: loop started and budget information.
- `tool_call`: model or offline agent selected a tool.
- `observation`: tool result.
- `context_compaction`: archived and summarized old context.
- `final`: final answer and `conversation_id`.
- `error`: structured recoverable or terminal error.

`POST /api/ask/cancel`

Body:

```json
{"request_id": "ask_..."}
```

Requests cancellation for an in-flight standard or streaming answer. Streaming runs stop at the next agent step; standard runs suppress persistence if cancellation arrives while a blocking provider call is still returning.

## Settings

`GET /api/settings`

Returns runtime settings without secrets.

`POST /api/settings`

Stores local runtime settings and mirrors them into SQLite.

`POST /api/settings/test`

Runs a small provider call using the supplied settings and reports whether the configured OpenAI-compatible or Anthropic endpoint is reachable.

## Conversations

`GET /api/conversations?limit=50`

Returns latest saved standard and ReAct conversations, including selected document IDs and messages.

`GET /api/conversations/{conversation_id}`

Returns one saved conversation with its full message transcript and selected document IDs.

`DELETE /api/conversations/{conversation_id}`

Deletes one saved conversation from SQLite.

## Cache

`GET /api/cache/{hash}`

Checks whether a SHA-256 fingerprint has a current reusable split cache.

`POST /api/cache/load`

Body:

```json
{"fingerprint": "sha256:..."}
```

Returns the cached document when available.

`POST /api/cache/clear`

Clears derived index files, cancels in-flight tasks, and marks documents pending.

## Tools

`POST /api/tools/search`

Runs the public lexical/hybrid search tool against selected indexed documents.

`POST /api/tools/directory`

Body:

```json
{"document_ids": ["doc_x"], "document_names": [], "target": "ALL", "node_id": ""}
```

Returns the same outline/directory data used by the ReAct agent.

`POST /api/tools/content`

Body:

```json
{"document_ids": ["doc_x"], "document_names": [], "pages": [1, 2], "node_id": "", "query": ""}
```

Returns page content by explicit pages, selected outline node, or a query-derived fallback page set.
