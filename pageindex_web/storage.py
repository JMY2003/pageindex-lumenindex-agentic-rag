import gzip
import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .db import AppDatabase, DEFAULT_OWNER_ID

CACHE_SCHEMA_VERSION = 2
INDEX_VERSION = "fastapi-web-2"
PDF_LOCAL_INDEX_VERSION = "pdf-local-2"


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def atomic_gzip_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def read_gzip_json(path: Path, default: Any = None) -> Any:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class TaskState:
    id: str
    document_id: str
    status: str = "pending"
    progress: int = 0
    message: str = "Waiting for indexing"
    error: str = ""
    cancelled: bool = False
    created_time: str = field(default_factory=now_iso)
    updated_time: str = field(default_factory=now_iso)

    def as_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


class DocumentStore:
    def __init__(self, root: Path):
        self.root = root
        self.upload_dir = root / "uploads"
        self.cache_dir = root / "cache"
        self.log_dir = root / "logs"
        self.manifest_path = self.cache_dir / "documents.json"
        self.tasks_path = self.cache_dir / "tasks.json"
        self.db = AppDatabase(root / "pageindex_web.sqlite3")
        self.lock = threading.RLock()
        self.tasks: Dict[str, TaskState] = {}
        for directory in (self.upload_dir, self.cache_dir, self.log_dir / "context_compactions"):
            directory.mkdir(parents=True, exist_ok=True)
        if not self.manifest_path.exists():
            atomic_json(self.manifest_path, {"documents": []})
        self._load_tasks()
        self.recover_interrupted_work()
        self.migrate_legacy_cache()
        self.sync_database()

    def _load_tasks(self) -> None:
        data = read_json(self.tasks_path, {"tasks": []})
        tasks = data.get("tasks", []) if isinstance(data, dict) else []
        for item in tasks:
            if not isinstance(item, dict) or not item.get("id") or not item.get("document_id"):
                continue
            allowed = {field.name for field in TaskState.__dataclass_fields__.values()}
            self.tasks[item["id"]] = TaskState(**{k: v for k, v in item.items() if k in allowed})

    def _save_tasks(self) -> None:
        atomic_json(self.tasks_path, {"tasks": [task.as_dict() for task in self.tasks.values()]})
        for task in self.tasks.values():
            self.db.upsert_task(task.as_dict())

    def recover_interrupted_work(self) -> None:
        with self.lock:
            for task in self.tasks.values():
                if task.status in {"pending", "indexing", "cancelling"}:
                    task.status = "cancelled" if task.cancelled or task.status == "cancelling" else "failed"
                    task.progress = max(task.progress, 100)
                    task.error = "" if task.status == "cancelled" else "Service restarted or the process was interrupted. The task did not finish. Upload again or reindex."
                    task.message = "Indexing cancelled" if task.status == "cancelled" else "Task interrupted"
                    task.updated_time = now_iso()
            data = self._manifest()
            changed = False
            for doc in data["documents"]:
                if doc.get("status") == "indexing":
                    doc["status"] = "failed"
                    doc["error"] = "Service restarted or the process was interrupted. Indexing did not finish."
                    changed = True
            if changed:
                atomic_json(self.manifest_path, data)
            self._save_tasks()

    def _manifest(self) -> Dict[str, Any]:
        data = read_json(self.manifest_path, {"documents": []})
        if not isinstance(data, dict) or not isinstance(data.get("documents"), list):
            return {"documents": []}
        return data

    def documents(self, owner_user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.lock:
            docs = self._manifest()["documents"]
            if owner_user_id:
                docs = [doc for doc in docs if (doc.get("owner_user_id") or DEFAULT_OWNER_ID) == owner_user_id]
            return sorted(docs, key=lambda d: d.get("upload_time", ""), reverse=True)

    def sync_database(self) -> None:
        with self.lock:
            for doc in self._manifest()["documents"]:
                doc.setdefault("owner_user_id", DEFAULT_OWNER_ID)
                self.db.upsert_document(doc)
            for task in self.tasks.values():
                self.db.upsert_task(task.as_dict())

    def migrate_legacy_cache(self) -> None:
        with self.lock:
            changed = False
            data = self._manifest()
            for doc in data["documents"]:
                document_id = doc.get("id")
                if not document_id:
                    continue
                cache_path = self.cache_dir / document_id
                metadata_path = cache_path / "metadata.json"
                pages_path = cache_path / "pages.json.gz"
                if metadata_path.exists() and pages_path.exists():
                    continue
                legacy_path = next((p for p in (cache_path / name for name in ("index.json", "document.json", "cache.json")) if p.exists()), None)
                if not legacy_path:
                    continue
                legacy = read_json(legacy_path, {})
                pages = legacy.get("pages") if isinstance(legacy, dict) else None
                structure = legacy.get("structure") or legacy.get("toc") if isinstance(legacy, dict) else None
                if not isinstance(pages, list) or not isinstance(structure, list):
                    continue
                metadata = {
                    "structure": structure,
                    "summary": legacy.get("summary", "") if isinstance(legacy, dict) else "",
                    "version": INDEX_VERSION,
                    "cache_schema_version": CACHE_SCHEMA_VERSION,
                    "pdf_local_index_version": doc.get("pdf_local_index_version", PDF_LOCAL_INDEX_VERSION),
                    "index_strategy": legacy.get("index_strategy", "legacy_migrated") if isinstance(legacy, dict) else "legacy_migrated",
                    "page_count": len(pages),
                    "migrated_from": legacy_path.name,
                    "migrated_time": now_iso(),
                }
                self.save_index(document_id, metadata, pages)
                legacy_path.replace(legacy_path.with_suffix(legacy_path.suffix + ".migrated"))
                doc.update({
                    "page_count": len(pages),
                    "status": "indexed",
                    "cache_schema_version": CACHE_SCHEMA_VERSION,
                    "index_version": INDEX_VERSION,
                    "pdf_local_index_version": PDF_LOCAL_INDEX_VERSION,
                })
                changed = True
            if changed:
                atomic_json(self.manifest_path, data)

    def get(self, document_id: str, owner_user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self.lock:
            doc = next((d for d in self._manifest()["documents"] if d.get("id") == document_id), None)
            if not doc:
                return None
            if owner_user_id and (doc.get("owner_user_id") or DEFAULT_OWNER_ID) != owner_user_id:
                return None
            return doc

    def find_by_fingerprint(self, fingerprint: str, owner_user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self.lock:
            for doc in self._manifest()["documents"]:
                if owner_user_id and (doc.get("owner_user_id") or DEFAULT_OWNER_ID) != owner_user_id:
                    continue
                if doc.get("fingerprint") != fingerprint:
                    continue
                if self.is_cache_current(doc):
                    return doc
            return None

    def is_cache_current(self, doc: Dict[str, Any]) -> bool:
        metadata = self.load_metadata(doc.get("id", ""))
        if not metadata:
            return False
        return (
            int(metadata.get("cache_schema_version") or 0) == CACHE_SCHEMA_VERSION
            and metadata.get("version") == INDEX_VERSION
            and (doc.get("pdf_local_index_version") == PDF_LOCAL_INDEX_VERSION or metadata.get("pdf_local_index_version") in {None, PDF_LOCAL_INDEX_VERSION})
            and bool(self.load_pages(doc.get("id", "")))
        )

    def upsert(self, doc: Dict[str, Any]) -> None:
        with self.lock:
            data = self._manifest()
            docs = [d for d in data["documents"] if d.get("id") != doc["id"]]
            docs.append(doc)
            data["documents"] = docs
            atomic_json(self.manifest_path, data)
            self.db.upsert_document(doc)

    def update(self, document_id: str, owner_user_id: Optional[str] = None, **patch: Any) -> Optional[Dict[str, Any]]:
        with self.lock:
            doc = self.get(document_id, owner_user_id)
            if not doc:
                return None
            doc.update(patch)
            self.upsert(doc)
            return doc

    def delete(self, document_id: str, owner_user_id: Optional[str] = None) -> bool:
        with self.lock:
            doc = self.get(document_id, owner_user_id)
            if not doc:
                return False
            for task in self.tasks.values():
                if task.document_id == document_id and task.status in {"pending", "indexing"}:
                    task.cancelled = True
                    task.status = "cancelling"
                    task.message = "Cancelling and deleting document"
                    task.updated_time = now_iso()
            data = self._manifest()
            data["documents"] = [d for d in data["documents"] if d.get("id") != document_id]
            atomic_json(self.manifest_path, data)
            self._save_tasks()
            self.db.delete_document(document_id)
        shutil.rmtree(self.upload_dir / document_id, ignore_errors=True)
        shutil.rmtree(self.cache_dir / document_id, ignore_errors=True)
        return True

    def new_document(self, original_name: str, size: int, mime_type: str, fingerprint: str, suffix: str, owner_user_id: str = DEFAULT_OWNER_ID) -> Dict[str, Any]:
        document_id = "doc_" + uuid.uuid4().hex[:12]
        upload_path = self.upload_dir / document_id / ("original" + suffix.lower())
        cache_path = self.cache_dir / document_id
        doc = {
            "id": document_id,
            "owner_user_id": owner_user_id,
            "name": original_name,
            "original_name": original_name,
            "size": size,
            "mime_type": mime_type,
            "upload_time": now_iso(),
            "status": "pending",
            "fingerprint": fingerprint,
            "page_count": 0,
            "index_version": INDEX_VERSION,
            "cache_schema_version": CACHE_SCHEMA_VERSION,
            "pdf_local_index_version": PDF_LOCAL_INDEX_VERSION,
            "upload_path": str(upload_path),
            "cache_path": str(cache_path),
        }
        self.upsert(doc)
        return doc

    def save_index(self, document_id: str, metadata: Dict[str, Any], pages: List[Dict[str, Any]]) -> None:
        cache_path = self.cache_dir / document_id
        atomic_json(cache_path / "metadata.json", metadata)
        atomic_gzip_json(cache_path / "pages.json.gz", pages)

    def load_metadata(self, document_id: str) -> Optional[Dict[str, Any]]:
        return read_json(self.cache_dir / document_id / "metadata.json")

    def load_pages(self, document_id: str) -> List[Dict[str, Any]]:
        pages = read_gzip_json(self.cache_dir / document_id / "pages.json.gz", [])
        return pages if isinstance(pages, list) else []

    def cache_stats(self) -> Dict[str, Any]:
        total = 0
        count = 0
        for path in self.cache_dir.rglob("*"):
            if path.is_file():
                count += 1
                total += path.stat().st_size
        return {"bytes": total, "files": count}

    def clear_cache(self, owner_user_id: Optional[str] = None) -> None:
        target_docs = self.documents(owner_user_id)
        target_ids = {doc["id"] for doc in target_docs}
        with self.lock:
            for task in self.tasks.values():
                if task.document_id in target_ids and task.status in {"pending", "indexing"}:
                    task.cancelled = True
                    task.status = "cancelling"
                    task.message = "Cache is being cleared; cancelling task"
            for doc in target_docs:
                self.update(doc["id"], status="pending", page_count=0)
            self._save_tasks()
        if owner_user_id:
            for doc_id in target_ids:
                shutil.rmtree(self.cache_dir / doc_id, ignore_errors=True)
        else:
            for path in self.cache_dir.iterdir():
                if path.name not in {"documents.json", "tasks.json"}:
                    if path.is_dir():
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        path.unlink(missing_ok=True)

    def claim_legacy_content(self, owner_user_id: str) -> None:
        with self.lock:
            data = self._manifest()
            changed = False
            for doc in data["documents"]:
                if not doc.get("owner_user_id") or doc.get("owner_user_id") == DEFAULT_OWNER_ID:
                    doc["owner_user_id"] = owner_user_id
                    self.db.upsert_document(doc)
                    changed = True
            if changed:
                atomic_json(self.manifest_path, data)
            self.db.claim_legacy_content(owner_user_id)

    def create_task(self, document_id: str) -> TaskState:
        with self.lock:
            task = TaskState(id="task_" + uuid.uuid4().hex[:12], document_id=document_id)
            self.tasks[task.id] = task
            self._save_tasks()
            return task

    def set_task(self, task_id: str, **patch: Any) -> None:
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return
            for key, value in patch.items():
                if hasattr(task, key):
                    setattr(task, key, value)
            task.updated_time = now_iso()
            self._save_tasks()

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            task = self.tasks.get(task_id)
            return task.as_dict() if task else None

    def request_cancel(self, task_id: str) -> bool:
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return False
            if task.status in {"completed", "failed", "cancelled"}:
                return True
            task.cancelled = True
            task.status = "cancelling"
            task.message = "Cancelling"
            task.updated_time = now_iso()
            self._save_tasks()
            return True

    def is_cancelled(self, task_id: str) -> bool:
        with self.lock:
            task = self.tasks.get(task_id)
            return bool(task and task.cancelled)


def safe_filename(name: str) -> str:
    return Path(name).name.replace("/", "_").replace("\\", "_").strip() or "document"


def allowed_suffix(filename: str) -> bool:
    return Path(filename).suffix.lower() in {".pdf", ".docx", ".doc", ".md", ".markdown"}
