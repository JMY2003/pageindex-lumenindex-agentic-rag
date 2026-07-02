import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import AgentTools, react_event_stream
from .context_compact import compact_messages, needs_compaction
from .llm import LLMError, build_prompt, complete, completion, has_api_key, synthesize_answer
from .prompts import get_prompt
from .schemas import NodeSelectionRow, extract_json_array, validate_model_list
from .search import SearchIndexCache, flatten_nodes
from .settings import SettingsStore
from .storage import DocumentStore, allowed_suffix, now_iso, read_gzip_json, read_json, safe_filename, sha256_file

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "pageindex_web" / "static"
MAX_UPLOAD_BYTES = int(os.getenv("PAGEINDEX_MAX_UPLOAD_MB", "200")) * 1024 * 1024
INDEX_WORKERS = max(1, min(8, int(os.getenv("PAGEINDEX_INDEX_WORKERS", "5"))))

logger = logging.getLogger("pageindex_web")
SESSION_COOKIE = "lumenindex_session"
MAX_ADMIN_USERS = 3


def api_error(message: str, code: str = "bad_request", recoverable: bool = True, **extra: Any) -> Dict[str, Any]:
    return {"error": message, "code": code, "recoverable": recoverable, **extra}


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)
    document_ids: List[str] = Field(default_factory=list, max_length=20)
    mode: str = "standard"
    conversation_id: Optional[str] = None
    request_id: Optional[str] = Field(default=None, max_length=100)


class AuthRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=80)
    password: str = Field(..., min_length=6, max_length=200)


class RegisterRequest(AuthRequest):
    password: str = Field(..., min_length=8, max_length=200)
    confirm_password: str = Field(..., min_length=8, max_length=200)


class AdminCreateUserRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=80)


class AskCancelRequest(BaseModel):
    request_id: str = Field(..., min_length=8, max_length=100)


class RenameRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=240)


class SettingsRequest(BaseModel):
    api_protocol: Optional[str] = None
    api_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    timeout: Optional[int] = None
    context_window: Optional[int] = None
    context_window_k: Optional[int] = None
    step_budget: Optional[int] = None
    max_output_tokens: Optional[int] = None
    deep_thinking: Optional[bool] = None
    context_enabled: Optional[bool] = None


class CacheLoadRequest(BaseModel):
    fingerprint: str = Field(..., min_length=8, max_length=100)


class ToolDirectoryRequest(BaseModel):
    document_ids: List[str] = Field(default_factory=list, max_length=20)
    document_names: List[str] = Field(default_factory=list, max_length=20)
    target: str = "ALL"
    node_id: str = ""


class ToolContentRequest(BaseModel):
    document_ids: List[str] = Field(default_factory=list, max_length=20)
    document_names: List[str] = Field(default_factory=list, max_length=20)
    pages: List[int] = Field(default_factory=list, max_length=12)
    node_id: str = ""
    query: str = ""


def sse(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def normalize_fingerprint(value: str) -> str:
    text = (value or "").strip()
    if text.startswith("sha256:"):
        return text
    return "sha256:" + text


def usable_conversation_id(value: Optional[str]) -> Optional[str]:
    text = (value or "").strip()
    if re.fullmatch(r"conv_[A-Za-z0-9_-]{6,80}", text):
        return text
    return None


def password_hash(password: str, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return f"pbkdf2_sha256$200000${salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, digest = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iterations)).hex()
        return hmac.compare_digest(candidate, digest)
    except Exception:
        return False


def public_user(user: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not user:
        return None
    return {"id": user["id"], "username": user["username"], "is_admin": bool(user.get("is_admin"))}


def validate_username(username: str) -> str:
    normalized = username.strip().lower()
    if not re.fullmatch(r"[a-z0-9_.@-]{3,80}", normalized):
        raise HTTPException(400, api_error("Use 3-80 letters, numbers, dots, underscores, hyphens, or @.", "invalid_username"))
    return normalized


def managed_user_password(username: str) -> str:
    return f"{username}@123"


def create_app() -> FastAPI:
    store = DocumentStore(ROOT)
    settings = SettingsStore(ROOT / "app_settings.json")
    search_cache = SearchIndexCache()
    index_pool = ThreadPoolExecutor(max_workers=INDEX_WORKERS, thread_name_prefix="pageindex-index")
    ask_cancellations: set[str] = set()
    app = FastAPI(title="LumenIndex", version="1.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.on_event("shutdown")
    def shutdown_index_pool() -> None:
        index_pool.shutdown(wait=False, cancel_futures=True)

    def session_response(response: Response, user_id: str) -> Dict[str, Any]:
        token = secrets.token_urlsafe(32)
        store.db.create_session(user_id, token, now_iso())
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            secure=False,
            samesite="lax",
            max_age=60 * 60 * 24 * 30,
        )
        user = store.db.get_user(user_id)
        return {"user": public_user(user)}

    def require_user(request: Request) -> Dict[str, Any]:
        token = request.cookies.get(SESSION_COOKIE, "")
        user = store.db.get_session_user(token) if token else None
        if not user:
            raise HTTPException(401, api_error("Sign in first", "not_authenticated", recoverable=True))
        return user

    def require_admin(user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
        if not user.get("is_admin"):
            raise HTTPException(403, api_error("Admin privileges are required", "admin_required", recoverable=False))
        return user

    def delete_user_assets(user_id: str) -> Dict[str, int]:
        documents = list(store.documents(user_id))
        deleted_documents = 0
        for doc in documents:
            if store.delete(doc["id"], user_id):
                deleted_documents += 1
        deleted_conversations = store.db.delete_conversations_for_user(user_id)
        if deleted_documents:
            search_cache.clear()
        return {"documents": deleted_documents, "conversations": deleted_conversations}

    def assert_not_admin_owned_asset(owner_user_id: str) -> None:
        owner = store.db.get_user(owner_user_id) if owner_user_id else None
        if owner and owner.get("is_admin"):
            raise HTTPException(403, api_error("Admin-owned assets cannot be managed from this panel.", "admin_asset_protected", recoverable=False))

    def selected_documents(user_id: str, document_ids: List[str]) -> List[Dict[str, Any]]:
        docs = []
        for doc_id in document_ids:
            doc = store.get(doc_id, user_id)
            if not doc or doc.get("status") != "indexed":
                continue
            metadata = store.load_metadata(doc_id)
            pages = store.load_pages(doc_id)
            if metadata and metadata.get("cache_schema_version"):
                docs.append({**doc, "structure": metadata.get("structure", []), "summary": metadata.get("summary", ""), "pages": pages})
        return docs

    def ask_request_id(value: Optional[str]) -> str:
        text = (value or "").strip()
        if re.fullmatch(r"ask_[A-Za-z0-9_-]{6,90}", text):
            return text
        return "ask_" + uuid.uuid4().hex[:16]

    def ensure_not_cancelled(request_id: str) -> None:
        if request_id in ask_cancellations:
            ask_cancellations.discard(request_id)
            raise HTTPException(499, api_error("Question answering was cancelled", "ask_cancelled", recoverable=True))

    def document_detail_payload(user_id: str, document_id: str) -> Dict[str, Any]:
        doc = store.get(document_id, user_id)
        metadata = store.load_metadata(document_id)
        if not doc or not metadata:
            raise HTTPException(404, "Document or index not found")
        cache_path = Path(doc.get("cache_path") or store.cache_dir / document_id)
        metadata_path = cache_path / "metadata.json"
        pages_path = cache_path / "pages.json.gz"
        cache_info = {
            "cache_path": str(cache_path),
            "metadata_path": str(metadata_path),
            "pages_path": str(pages_path),
            "metadata_bytes": metadata_path.stat().st_size if metadata_path.exists() else 0,
            "pages_bytes": pages_path.stat().st_size if pages_path.exists() else 0,
            "cache_schema_version": metadata.get("cache_schema_version"),
            "index_version": metadata.get("version"),
            "pdf_local_index_version": metadata.get("pdf_local_index_version"),
            "fingerprint": doc.get("fingerprint"),
            "is_current": store.is_cache_current(doc),
        }
        return {"document": doc, "metadata": metadata, "structure": metadata.get("structure", []), "cache": cache_info}

    def tool_runner(docs: List[Dict[str, Any]]) -> AgentTools:
        return AgentTools(docs, search_cache.get(docs), collect_pages)

    def submit_index(document_id: str, user_id: Optional[str] = None) -> str:
        doc = store.get(document_id, user_id)
        if not doc:
            raise HTTPException(404, "Document not found")
        if not Path(doc["upload_path"]).exists():
            raise HTTPException(409, "Original file not found; cannot reindex")
        task = store.create_task(document_id)
        store.update(document_id, status="pending", error="")
        index_pool.submit(run_index, task.id, document_id)
        return task.id

    def run_index(task_id: str, document_id: str) -> None:
        doc = store.get(document_id)
        if not doc:
            return
        def progress(value: int, message: str) -> None:
            if store.is_cancelled(task_id):
                raise RuntimeError("Indexing task was cancelled")
            store.set_task(task_id, status="indexing", progress=max(0, min(99, value)), message=message)
            store.update(document_id, status="indexing")
        try:
            store.set_task(task_id, status="indexing", progress=1, message="Starting indexing task")
            metadata, pages = run_worker_index(task_id, doc, progress)
            if store.is_cancelled(task_id):
                raise RuntimeError("Indexing task was cancelled")
            metadata.update({
                "document_id": document_id,
                "created_time": now_iso(),
                "original_name": doc.get("original_name"),
            })
            store.save_index(document_id, metadata, pages)
            search_cache.clear()
            store.update(
                document_id,
                status="indexed",
                page_count=metadata.get("page_count", len(pages)),
                index_strategy=metadata.get("index_strategy"),
                error="",
            )
            store.set_task(task_id, status="completed", progress=100, message="Indexing completed")
        except Exception as exc:
            if store.is_cancelled(task_id):
                store.update(document_id, status="cancelled", error="Indexing task was cancelled")
                store.set_task(task_id, status="cancelled", progress=100, message="Indexing cancelled", error="")
            else:
                logger.exception("Indexing failed for %s", document_id)
                store.update(document_id, status="failed", error=str(exc))
                store.set_task(task_id, status="failed", progress=100, message="Indexing failed", error=str(exc))

    def run_worker_index(task_id: str, doc: Dict[str, Any], progress: Any) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
        worker_root = store.cache_dir / doc["id"] / "_workers" / task_id
        request_path = worker_root / "request.json"
        progress_path = worker_root / "progress.json"
        result_path = worker_root / "result.json"
        output_dir = worker_root / "output"
        request = {
            "document_id": doc["id"],
            "upload_path": doc["upload_path"],
            "output_dir": str(output_dir),
        }
        from .storage import atomic_json

        atomic_json(request_path, request)
        process = subprocess.Popen(
            [sys.executable, "-m", "pageindex_web.worker", str(request_path), str(progress_path), str(result_path)],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        last_progress = -1
        stderr_tail = ""
        try:
            while process.poll() is None:
                if store.is_cancelled(task_id):
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise RuntimeError("Indexing task was cancelled")
                state = read_json(progress_path, {})
                if state and int(state.get("progress", -1)) != last_progress:
                    last_progress = int(state.get("progress", 0))
                    progress(last_progress, str(state.get("message") or "Indexing"))
                time.sleep(0.2)
            _, stderr = process.communicate(timeout=2)
            stderr_tail = (stderr or "")[-2000:]
        finally:
            if process.poll() is None:
                process.kill()
        result = read_json(result_path, {})
        if process.returncode != 0 or result.get("status") != "completed":
            detail = result.get("error") or stderr_tail or f"worker exited with {process.returncode}"
            raise RuntimeError(detail)
        metadata = read_json(Path(result["metadata_path"]), {})
        pages = read_gzip_json(Path(result["pages_path"]), [])
        if not metadata or not pages:
            raise RuntimeError("Worker did not produce a valid index result")
        return metadata, pages

    @app.get("/", response_class=HTMLResponse)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.head("/")
    def index_head() -> Dict[str, Any]:
        return {}

    @app.get("/api/health")
    def health() -> Dict[str, Any]:
        return {
            "ok": True,
            "documents": len(store.documents()),
            "cache": store.cache_stats(),
            "index_workers": INDEX_WORKERS,
            "max_upload_mb": MAX_UPLOAD_BYTES // 1024 // 1024,
        }

    @app.get("/api/auth/me")
    def auth_me(request: Request) -> Dict[str, Any]:
        token = request.cookies.get(SESSION_COOKIE, "")
        user = store.db.get_session_user(token) if token else None
        return {"authenticated": bool(user), "user": public_user(user)}

    @app.post("/api/auth/register")
    def auth_register(body: RegisterRequest, response: Response) -> Dict[str, Any]:
        username = validate_username(body.username)
        if body.password != body.confirm_password:
            raise HTTPException(400, api_error("Passwords do not match", "password_mismatch"))
        if username == "admin":
            raise HTTPException(400, api_error("Use Create admin account for admin users.", "admin_requires_admin_creation"))
        try:
            user = store.db.create_user(username, password_hash(body.password), now_iso(), is_admin=False)
        except sqlite3.IntegrityError:
            raise HTTPException(409, api_error("Username already exists", "username_exists"))
        return {"ok": True, **session_response(response, user["id"])}

    @app.post("/api/auth/register-admin")
    def auth_register_admin(body: RegisterRequest, response: Response) -> Dict[str, Any]:
        username = validate_username(body.username)
        if body.password != body.confirm_password:
            raise HTTPException(400, api_error("Passwords do not match", "password_mismatch"))
        if store.db.admin_count() >= MAX_ADMIN_USERS:
            raise HTTPException(403, api_error("The system already has the maximum number of admin accounts.", "admin_limit_reached", recoverable=False, max_admins=MAX_ADMIN_USERS))
        try:
            user = store.db.create_user(username, password_hash(body.password), now_iso(), is_admin=True)
        except sqlite3.IntegrityError:
            raise HTTPException(409, api_error("Username already exists", "username_exists"))
        return {"ok": True, **session_response(response, user["id"]), "max_admins": MAX_ADMIN_USERS}

    @app.post("/api/auth/login")
    def auth_login(body: AuthRequest, response: Response) -> Dict[str, Any]:
        user = store.db.get_user_by_username(body.username.strip().lower())
        if not user or not verify_password(body.password, user["password_hash"]):
            raise HTTPException(401, api_error("Invalid username or password", "invalid_credentials"))
        return {"ok": True, **session_response(response, user["id"])}

    @app.post("/api/auth/logout")
    def auth_logout(request: Request, response: Response) -> Dict[str, Any]:
        token = request.cookies.get(SESSION_COOKIE, "")
        if token:
            store.db.delete_session(token)
        response.delete_cookie(SESSION_COOKIE)
        return {"ok": True}

    @app.get("/api/admin/overview")
    def admin_overview(user: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
        users = store.db.list_users()
        owners = {item["id"]: item for item in users}
        documents = []
        for doc in store.documents():
            owner_id = doc.get("owner_user_id") or ""
            documents.append({
                "id": doc.get("id"),
                "name": doc.get("name"),
                "status": doc.get("status"),
                "size": doc.get("size"),
                "page_count": doc.get("page_count"),
                "upload_time": doc.get("upload_time"),
                "owner_user_id": owner_id,
                "owner_username": owners.get(owner_id, {}).get("username", "legacy"),
                "owner_is_admin": bool(owners.get(owner_id, {}).get("is_admin")),
            })
        conversations = []
        for conversation in store.db.list_conversations(200):
            owner_id = conversation.get("owner_user_id") or ""
            first_user_message = next((item.get("content") for item in conversation.get("messages", []) if item.get("role") == "user" and item.get("content")), "")
            conversations.append({
                "id": conversation.get("id"),
                "mode": conversation.get("mode"),
                "document_ids": conversation.get("document_ids", []),
                "message_count": len(conversation.get("messages", [])),
                "title": str(first_user_message or "Untitled conversation")[:180],
                "created_time": conversation.get("created_time"),
                "updated_time": conversation.get("updated_time"),
                "owner_user_id": owner_id,
                "owner_username": owners.get(owner_id, {}).get("username", "legacy"),
                "owner_is_admin": bool(owners.get(owner_id, {}).get("is_admin")),
            })
        return {"users": users, "documents": documents, "conversations": conversations}

    @app.post("/api/admin/users")
    def admin_create_user(body: AdminCreateUserRequest, user: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
        username = validate_username(body.username)
        if username == "admin":
            raise HTTPException(400, api_error("Use Create admin account for admin users.", "admin_requires_admin_creation"))
        temporary_password = managed_user_password(username)
        try:
            created = store.db.create_user(username, password_hash(temporary_password), now_iso(), is_admin=False)
        except sqlite3.IntegrityError:
            raise HTTPException(409, api_error("Username already exists", "username_exists"))
        return {"ok": True, "user": public_user(created), "temporary_password": temporary_password}

    @app.post("/api/admin/users/{user_id}/reset-password")
    def admin_reset_user_password(user_id: str, user: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
        target = store.db.get_user(user_id)
        if not target:
            raise HTTPException(404, api_error("User not found", "user_not_found", recoverable=False))
        if target.get("is_admin"):
            raise HTTPException(403, api_error("Admin accounts cannot be managed from this panel.", "admin_user_protected", recoverable=False))
        temporary_password = managed_user_password(target["username"])
        if not store.db.update_user_password(user_id, password_hash(temporary_password)):
            raise HTTPException(404, api_error("User not found", "user_not_found", recoverable=False))
        deleted_sessions = store.db.delete_sessions_for_user(user_id)
        return {"ok": True, "user": public_user(target), "temporary_password": temporary_password, "deleted_sessions": deleted_sessions}

    @app.delete("/api/admin/users/{user_id}")
    def admin_delete_user(user_id: str, user: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
        target = store.db.get_user(user_id)
        if not target:
            raise HTTPException(404, api_error("User not found", "user_not_found", recoverable=False))
        if target.get("is_admin"):
            raise HTTPException(403, api_error("Admin accounts cannot be managed from this panel.", "admin_user_protected", recoverable=False))
        deleted_assets = delete_user_assets(user_id)
        deleted_sessions = store.db.delete_sessions_for_user(user_id)
        if not store.db.delete_user(user_id):
            raise HTTPException(404, api_error("User not found", "user_not_found", recoverable=False))
        return {"ok": True, "deleted_assets": deleted_assets, "deleted_sessions": deleted_sessions}

    @app.delete("/api/admin/documents/{document_id}")
    def admin_delete_document(document_id: str, user: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
        doc = store.get(document_id)
        if not doc:
            raise HTTPException(404, "Document not found")
        assert_not_admin_owned_asset(doc.get("owner_user_id") or "")
        if not store.delete(document_id):
            raise HTTPException(404, "Document not found")
        search_cache.clear()
        return {"ok": True}

    @app.delete("/api/admin/conversations/{conversation_id}")
    def admin_delete_conversation(conversation_id: str, user: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
        conversation = store.db.get_conversation(conversation_id)
        if not conversation:
            raise HTTPException(404, "Conversation not found")
        assert_not_admin_owned_asset(conversation.get("owner_user_id") or "")
        if not store.db.delete_conversation(conversation_id):
            raise HTTPException(404, "Conversation not found")
        return {"ok": True}

    @app.post("/api/upload")
    async def upload(files: List[UploadFile] = File(...), user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
        if len(files) > 10:
            raise HTTPException(400, "You can upload at most 10 files at a time")
        items = []
        for upload_file in files:
            filename = safe_filename(upload_file.filename or "document")
            if not allowed_suffix(filename):
                items.append({"name": filename, "status": "failed", "error": "Only PDF, DOCX, DOC, and Markdown are supported"})
                continue
            suffix = Path(filename).suffix
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                shutil.copyfileobj(upload_file.file, tmp)
                tmp_path = Path(tmp.name)
            if tmp_path.stat().st_size > MAX_UPLOAD_BYTES:
                tmp_path.unlink(missing_ok=True)
                items.append({"name": filename, "status": "failed", "error": f"File exceeds the {MAX_UPLOAD_BYTES // 1024 // 1024} MB limit"})
                continue
            fingerprint = sha256_file(tmp_path)
            existing = store.find_by_fingerprint(fingerprint, user["id"])
            if existing:
                tmp_path.unlink(missing_ok=True)
                items.append({"name": filename, "status": "cached", "document": existing})
                continue
            doc = store.new_document(filename, tmp_path.stat().st_size, upload_file.content_type or "", fingerprint, suffix, user["id"])
            target = Path(doc["upload_path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tmp_path), str(target))
            task_id = submit_index(doc["id"], user["id"])
            items.append({"name": filename, "status": "accepted", "document": doc, "task_id": task_id})
        return {"items": items}

    @app.get("/api/documents")
    def documents(user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
        return {"documents": store.documents(user["id"]), "cache": store.cache_stats()}

    @app.get("/api/cache/{fingerprint}")
    def cache_check(fingerprint: str, user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
        doc = store.find_by_fingerprint(normalize_fingerprint(fingerprint), user["id"])
        return {"hit": bool(doc), "document": doc}

    @app.post("/api/cache/load")
    def cache_load(body: CacheLoadRequest, user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
        doc = store.find_by_fingerprint(normalize_fingerprint(body.fingerprint), user["id"])
        if not doc:
            raise HTTPException(404, api_error("Cache entry does not exist or is out of date", "cache_miss"))
        return {"hit": True, "document": doc}

    @app.delete("/api/documents/{document_id}")
    def delete_document(document_id: str, user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
        if not store.delete(document_id, user["id"]):
            raise HTTPException(404, "Document not found")
        search_cache.clear()
        return {"ok": True}

    @app.put("/api/documents/{document_id}")
    def rename_document(document_id: str, body: RenameRequest, user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
        doc = store.update(document_id, owner_user_id=user["id"], name=safe_filename(body.name))
        if not doc:
            raise HTTPException(404, "Document not found")
        return {"document": doc}

    @app.post("/api/documents/{document_id}/reindex")
    def reindex_document(document_id: str, user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
        task_id = submit_index(document_id, user["id"])
        return {"ok": True, "task_id": task_id}

    @app.get("/api/documents/{document_id}/structure")
    def document_structure(document_id: str, user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
        return document_detail_payload(user["id"], document_id)

    @app.get("/api/documents/{document_id}/detail")
    def document_detail(document_id: str, user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
        return document_detail_payload(user["id"], document_id)

    @app.get("/api/index/progress/{task_id}")
    def task_progress(task_id: str, user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
        task = store.get_task(task_id)
        if not task or not store.get(task.get("document_id", ""), user["id"]):
            raise HTTPException(404, "Task not found")
        return task

    @app.post("/api/index/cancel/{task_id}")
    def cancel_task(task_id: str, user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
        task = store.get_task(task_id)
        if not task or not store.get(task.get("document_id", ""), user["id"]):
            raise HTTPException(404, "Task not found")
        if not store.request_cancel(task_id):
            raise HTTPException(404, "Task not found")
        return {"ok": True}

    @app.post("/api/ask/standard")
    def ask_standard(body: AskRequest, user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
        request_id = ask_request_id(body.request_id)
        previous = store.db.get_conversation(body.conversation_id, user["id"]) if body.conversation_id else None
        document_ids = body.document_ids or (previous.get("document_ids", []) if previous else [])
        docs = selected_documents(user["id"], document_ids)
        if not docs:
            raise HTTPException(400, api_error("Select at least one indexed document first", "no_indexed_documents"))
        ensure_not_cancelled(request_id)
        cfg = settings.load(include_secret=True)
        searcher = search_cache.get(docs)
        session_messages, context_messages, compaction_info = prepare_session_context(
            cfg,
            store.log_dir,
            previous.get("messages", []) if previous else [],
        )
        conversation_context = conversation_context_from_messages(context_messages)
        retrieval_question = contextual_retrieval_question(body.question, conversation_context)
        ensure_not_cancelled(request_id)
        selected_nodes = choose_nodes_with_llm(cfg, retrieval_question, docs)
        if selected_nodes:
            results = node_results_from_selection(docs, selected_nodes)
        else:
            results = searcher.search(retrieval_question, top_k=8)
        pages = collect_pages(docs, results)
        fallback = synthesize_answer(body.question, results, pages)
        ensure_not_cancelled(request_id)
        answer = complete(cfg, build_prompt(body.question, results, pages, conversation_context), fallback)
        ensure_not_cancelled(request_id)
        conversation_id = previous["id"] if previous else usable_conversation_id(body.conversation_id) or "conv_" + uuid.uuid4().hex[:12]
        created = previous["created_time"] if previous else now_iso()
        messages = list(session_messages)
        messages.extend([
            {"role": "user", "content": body.question},
            {"role": "assistant", "content": answer, "results": results, "pages": [p.get("page") for p in pages]},
        ])
        store.db.save_conversation(
            conversation_id,
            [doc["id"] for doc in docs],
            "standard",
            messages,
            created,
            now_iso(),
            user["id"],
        )
        return {"answer": answer, "results": results, "pages": [p.get("page") for p in pages], "mode": "standard", "selection_strategy": "llm_nodes" if selected_nodes else "lexical_fallback", "conversation_id": conversation_id, "request_id": request_id, "context_compaction": compaction_info}

    @app.post("/api/ask/stream")
    async def ask_stream(body: AskRequest, user: Dict[str, Any] = Depends(require_user)) -> StreamingResponse:
        async def gen():
            request_id = ask_request_id(body.request_id)
            previous = store.db.get_conversation(body.conversation_id, user["id"]) if body.conversation_id else None
            document_ids = body.document_ids or (previous.get("document_ids", []) if previous else [])
            docs = selected_documents(user["id"], document_ids)
            if not docs:
                yield sse("error", api_error("Select at least one indexed document first", "no_indexed_documents"))
                return
            if request_id in ask_cancellations:
                ask_cancellations.discard(request_id)
                yield sse("error", api_error("Question answering was cancelled", "ask_cancelled", recoverable=True))
                return
            cfg = settings.load(include_secret=True)
            searcher = search_cache.get(docs)
            conversation_id = previous["id"] if previous else usable_conversation_id(body.conversation_id) or "conv_" + uuid.uuid4().hex[:12]
            created = previous["created_time"] if previous else now_iso()
            session_messages, context_messages, compaction_info = prepare_session_context(
                cfg,
                store.log_dir,
                previous.get("messages", []) if previous else [],
            )
            if compaction_info.get("compacted"):
                yield sse("context_compaction", {"title": "Compacting saved session context", **compaction_info})
            transcript: List[Dict[str, Any]] = list(session_messages)
            transcript.append({"role": "user", "content": body.question})
            conversation_context = conversation_context_from_messages(context_messages)
            async for event in react_event_stream(body.question, docs, searcher, cfg, store.log_dir, collect_pages, conversation_context):
                if request_id in ask_cancellations:
                    ask_cancellations.discard(request_id)
                    event = {"event": "error", "data": api_error("Question answering was cancelled", "ask_cancelled", recoverable=True)}
                    transcript.append({"type": "error", **event["data"]})
                    store.db.save_conversation(conversation_id, [doc["id"] for doc in docs], "react", transcript, created, now_iso(), user["id"])
                    event["data"]["conversation_id"] = conversation_id
                    yield sse(event["event"], event["data"])
                    return
                if event["event"] in {"tool_call", "observation", "context_compaction", "error", "final"}:
                    transcript.append({"type": event["event"], **event["data"]})
                if event["event"] == "final":
                    store.db.save_conversation(conversation_id, [doc["id"] for doc in docs], "react", transcript, created, now_iso(), user["id"])
                    event["data"]["conversation_id"] = conversation_id
                if event["event"] == "error":
                    store.db.save_conversation(conversation_id, [doc["id"] for doc in docs], "react", transcript, created, now_iso(), user["id"])
                    event["data"]["conversation_id"] = conversation_id
                yield sse(event["event"], event["data"])
        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.post("/api/ask/cancel")
    def cancel_ask(body: AskCancelRequest, user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
        ask_cancellations.add(body.request_id)
        return {"ok": True, "request_id": body.request_id}

    @app.get("/api/settings")
    def get_settings(user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
        return settings.load(include_secret=False)

    @app.post("/api/settings")
    def save_settings(body: SettingsRequest, user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
        saved = settings.save(body.dict(exclude_unset=True))
        store.db.set_setting("runtime", saved, now_iso())
        return saved

    @app.post("/api/settings/test")
    def test_settings(body: SettingsRequest, user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
        cfg = settings.load(include_secret=True)
        patch = body.dict(exclude_unset=True)
        if not patch.get("api_key"):
            patch.pop("api_key", None)
        cfg.update({key: value for key, value in patch.items() if value is not None})
        if not has_api_key(cfg):
            raise HTTPException(400, api_error("API key is not configured", "api_key_missing"))
        started = time.perf_counter()
        try:
            result = completion(
                cfg,
                [
                    {"role": "system", "content": "You are testing an API connection. Reply with exactly: OK"},
                    {"role": "user", "content": "Connection test"},
                ],
                tools=None,
                max_tokens=16,
                temperature=0,
                retries=0,
            )
        except LLMError as exc:
            raise HTTPException(502, api_error(str(exc), "api_test_failed", recoverable=True))
        latency_ms = int((time.perf_counter() - started) * 1000)
        return {"ok": True, "latency_ms": latency_ms, "model": cfg.get("model"), "message": (result.text or "").strip(), "stop_reason": result.stop_reason}

    @app.get("/api/conversations")
    def list_conversations(limit: int = 50, user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
        return {"conversations": store.db.list_conversations(limit, user["id"])}

    @app.get("/api/conversations/{conversation_id}")
    def get_conversation(conversation_id: str, user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
        conversation = store.db.get_conversation(conversation_id, user["id"])
        if not conversation:
            raise HTTPException(404, "Conversation not found")
        return {"conversation": conversation}

    @app.delete("/api/conversations/{conversation_id}")
    def delete_conversation(conversation_id: str, user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
        if not store.db.delete_conversation(conversation_id, user["id"]):
            raise HTTPException(404, "Conversation not found")
        return {"ok": True}

    @app.post("/api/cache/clear")
    def clear_cache(user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
        store.clear_cache(user["id"])
        search_cache.clear()
        return {"ok": True, "cache": store.cache_stats()}

    @app.post("/api/tools/search")
    def tool_search(body: AskRequest, user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
        docs = selected_documents(user["id"], body.document_ids)
        return {"results": search_cache.get(docs).search(body.question, top_k=12)}

    @app.post("/api/tools/directory")
    def tool_directory(body: ToolDirectoryRequest, user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
        docs = selected_documents(user["id"], body.document_ids)
        if not docs:
            raise HTTPException(400, api_error("Select at least one indexed document first", "no_indexed_documents"))
        return {"directory": tool_runner(docs).get_directory_structure(body.document_names, body.target, body.node_id)}

    @app.post("/api/tools/content")
    def tool_content(body: ToolContentRequest, user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
        docs = selected_documents(user["id"], body.document_ids)
        if not docs:
            raise HTTPException(400, api_error("Select at least one indexed document first", "no_indexed_documents"))
        tools = tool_runner(docs)
        pages = list(body.pages or [])
        if body.node_id:
            for doc in docs:
                for node in flatten_nodes(doc.get("structure", [])):
                    if str(node.get("node_id")) == body.node_id:
                        node_pages = node.get("pages") or [node.get("start_index", 1), node.get("end_index", 1)]
                        pages.extend(range(int(node_pages[0]), int(node_pages[1]) + 1))
                        break
        if not pages and body.query:
            results = tools.search(body.document_names, body.query, 4)
            pages = sorted({page for item in results for page in range(int(item.get("pages", [1, 1])[0]), int(item.get("pages", [1, 1])[1]) + 1)})[:12]
        return {"pages": tools.get_page_content(body.document_names, pages)}

    return app


def collect_pages(docs: List[Dict[str, Any]], results: List[Dict[str, Any]], limit: int = 12) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    by_id = {d["id"]: d for d in docs}
    for result in results:
        doc = by_id.get(result["document_id"])
        if not doc:
            continue
        page_map = {p["page"]: p for p in doc.get("pages", [])}
        start, end = result.get("pages", [1, 1])
        for page_num in range(int(start), int(end) + 1):
            key = (doc["id"], page_num)
            if key in seen or page_num not in page_map:
                continue
            seen.add(key)
            out.append({"document_id": doc["id"], "document_name": doc["name"], "page": page_num, "content": page_map[page_num].get("content", ""), "section": result.get("title", "")})
            if len(out) >= limit:
                return out
    return out


def visible_session_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    visible: List[Dict[str, str]] = []
    for item in messages:
        if item.get("type") == "context_summary":
            continue
        if item.get("role") == "user" and item.get("content"):
            visible.append({"role": "user", "content": str(item.get("content")).strip()})
        elif item.get("role") == "assistant" and item.get("content"):
            visible.append({"role": "assistant", "content": str(item.get("content")).strip()})
        elif item.get("type") == "final" and item.get("answer"):
            visible.append({"role": "assistant", "content": str(item.get("answer")).strip()})
        elif item.get("type") == "error" and item.get("error"):
            visible.append({"role": "user", "content": f"System error: {str(item.get('error')).strip()}"})
    return [item for item in visible if item.get("content")]


def latest_context_summary(messages: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for item in reversed(messages):
        if item.get("type") == "context_summary":
            return item
    return None


def session_context_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    visible = visible_session_messages(messages)
    summary = latest_context_summary(messages)
    if not summary:
        return visible
    covered = max(0, min(len(visible), int(summary.get("covered_visible_count") or 0)))
    context: List[Dict[str, str]] = []
    content = str(summary.get("content") or summary.get("summary") or "").strip()
    if content:
        context.append({"role": "user", "content": f"Compressed session context:\n{content}"})
    context.extend(visible[covered:])
    return context


def prepare_session_context(settings: Dict[str, Any], log_dir: Path, messages: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]], Dict[str, Any]]:
    session_messages = list(messages or [])
    if not bool(settings.get("context_enabled", True)):
        return session_messages, [], {"compacted": False, "reason": "session_memory_disabled"}
    context = session_context_messages(session_messages)
    if not context:
        return session_messages, [], {"compacted": False, "reason": "empty_session"}
    trigger_window = max(1000, int(int(settings.get("context_window", 8000)) * 0.8))
    if not needs_compaction(context, trigger_window):
        return session_messages, context, {"compacted": False, "reason": "within_context_window"}
    try:
        compacted, info = compact_messages(settings, log_dir, context, keep_recent=4)
    except Exception as exc:
        return session_messages, context, {"compacted": False, "reason": "compaction_failed", "error": str(exc)}
    if not info.get("compacted") or not compacted:
        return session_messages, context, info
    visible = visible_session_messages(session_messages)
    recent_visible_count = len([item for item in compacted[1:] if item.get("role") in {"user", "assistant"} and item.get("content")])
    covered_visible_count = max(0, len(visible) - recent_visible_count)
    summary_content = str(compacted[0].get("content") or info.get("summary") or "").strip()
    summary_item = {
        "type": "context_summary",
        "content": summary_content,
        "summary": info.get("summary", ""),
        "archived_path": info.get("archived_path", ""),
        "covered_visible_count": covered_visible_count,
        "created_time": now_iso(),
    }
    session_messages = [summary_item] + [item for item in session_messages if item.get("type") != "context_summary"]
    context = session_context_messages(session_messages)
    info = {**info, "covered_visible_count": covered_visible_count, "trigger_window": trigger_window}
    return session_messages, context, info


def conversation_context_from_messages(messages: List[Dict[str, Any]], limit: int = 40, max_chars: int = 24000) -> str:
    lines: List[str] = []
    for item in messages:
        if item.get("type") == "context_summary" and (item.get("content") or item.get("summary")):
            lines.append(f"Compressed session context: {str(item.get('content') or item.get('summary')).strip()}")
        if item.get("role") == "user" and item.get("content"):
            lines.append(f"User: {str(item.get('content')).strip()}")
        elif item.get("role") == "assistant" and item.get("content"):
            lines.append(f"Assistant: {str(item.get('content')).strip()}")
        elif item.get("type") == "final" and item.get("answer"):
            lines.append(f"Assistant: {str(item.get('answer')).strip()}")
        elif item.get("type") == "error" and item.get("error"):
            lines.append(f"System error: {str(item.get('error')).strip()}")
    context = "\n\n".join(lines[-limit:])
    if len(context) > max_chars:
        context = context[-max_chars:]
    return context


def contextual_retrieval_question(question: str, conversation_context: str, max_context_chars: int = 3000) -> str:
    current = str(question or "").strip()
    context = str(conversation_context or "").strip()
    if not context:
        return current
    if len(context) > max_context_chars:
        context = context[-max_context_chars:]
    return (
        "Recent chat context for resolving references in the current question:\n"
        f"{context}\n\n"
        "Current question to answer using the selected documents:\n"
        f"{current}"
    )


def choose_nodes_with_llm(settings: Dict[str, Any], question: str, docs: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    if not has_api_key(settings):
        return []
    tree_lines = []
    node_lookup = set()
    for doc in docs:
        for node in flatten_nodes(doc.get("structure", [])):
            pages = node.get("pages") or [node.get("start_index", 1), node.get("end_index", 1)]
            node_lookup.add((doc["id"], str(node.get("node_id", ""))))
            tree_lines.append(
                f"- document_id={doc['id']} document={doc.get('name')} node_id={node.get('node_id')} pages={pages} title={node.get('section_path', node.get('title', ''))} summary={node.get('summary', '')[:240]}"
            )
    if not tree_lines:
        return []
    prompt = [
        {"role": "system", "content": get_prompt("node_selector.system")},
        {"role": "user", "content": get_prompt("node_selector.user", question=question, directory_tree="\n".join(tree_lines[:900]))},
    ]
    try:
        response = completion(settings, prompt, tools=None, max_tokens=min(1200, int(settings.get("max_output_tokens", 3072))), temperature=0)
        text = response.text.strip()
        rows = validate_model_list(NodeSelectionRow, extract_json_array(text), limit=24)
    except Exception:
        return []
    selected = []
    seen = set()
    for row in rows:
        doc_id = str(row.document_id)
        node_id = str(row.node_id)
        key = (doc_id, node_id)
        if key in node_lookup and key not in seen:
            selected.append({"document_id": doc_id, "node_id": node_id, "reason": row.reason})
            seen.add(key)
        if len(selected) >= 6:
            break
    return selected


def node_results_from_selection(docs: List[Dict[str, Any]], selected: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    by_doc = {doc["id"]: doc for doc in docs}
    results: List[Dict[str, Any]] = []
    rank = 1
    for item in selected:
        doc = by_doc.get(item["document_id"])
        if not doc:
            continue
        nodes = {str(node.get("node_id", "")): node for node in flatten_nodes(doc.get("structure", []))}
        node = nodes.get(str(item["node_id"]))
        if not node:
            continue
        pages = node.get("pages") or [node.get("start_index", 1), node.get("end_index", 1)]
        results.append({
            "rank": rank,
            "document_id": doc["id"],
            "document_name": doc["name"],
            "node_id": node.get("node_id", ""),
            "title": node.get("title", ""),
            "section_path": node.get("section_path", node.get("title", "")),
            "pages": [int(pages[0]), int(pages[1])],
            "score": 1000 - rank,
            "matched_terms": [],
            "coverage": 1.0,
            "proximity": 1.0,
            "snippet": node.get("summary") or item.get("reason") or node.get("title", ""),
        })
        rank += 1
    return results


def trim_pages(pages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for page in pages[:12]:
        text = (page.get("content") or "").strip()
        rows.append({**page, "content": text[:900] + ("..." if len(text) > 900 else "")})
    return rows


def estimate_tokens(question: str, results: List[Dict[str, Any]], pages: List[Dict[str, Any]]) -> int:
    text = question + json.dumps(results, ensure_ascii=False) + json.dumps(trim_pages(pages), ensure_ascii=False)
    cjk = sum(1 for ch in text if "\u3400" <= ch <= "\u9fff")
    return cjk + int((len(text) - cjk) / 4)
