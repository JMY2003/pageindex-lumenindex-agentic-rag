import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_OWNER_ID = "user_local"


class AppDatabase:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    owner_user_id TEXT DEFAULT 'user_local',
                    name TEXT NOT NULL,
                    original_name TEXT,
                    status TEXT,
                    fingerprint TEXT,
                    size INTEGER,
                    page_count INTEGER,
                    index_strategy TEXT,
                    upload_time TEXT,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_documents_fingerprint ON documents(fingerprint);
                CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    status TEXT,
                    progress INTEGER,
                    cancelled INTEGER DEFAULT 0,
                    created_time TEXT,
                    updated_time TEXT,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_document ON tasks(document_id);
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    owner_user_id TEXT DEFAULT 'user_local',
                    document_ids_json TEXT NOT NULL,
                    mode TEXT,
                    messages_json TEXT NOT NULL,
                    created_time TEXT,
                    updated_time TEXT
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_time TEXT
                );
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    is_admin INTEGER DEFAULT 0,
                    created_time TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_time TEXT NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
                """
            )
            self._ensure_column(conn, "documents", "owner_user_id", "TEXT DEFAULT 'user_local'")
            self._ensure_column(conn, "conversations", "owner_user_id", "TEXT DEFAULT 'user_local'")
            self._ensure_column(conn, "users", "is_admin", "INTEGER DEFAULT 0")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_owner ON documents(owner_user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_owner ON conversations(owner_user_id)")

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if column not in {row["name"] for row in rows}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def upsert_document(self, doc: Dict[str, Any]) -> None:
        owner_user_id = doc.get("owner_user_id") or DEFAULT_OWNER_ID
        doc["owner_user_id"] = owner_user_id
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO documents(id, owner_user_id, name, original_name, status, fingerprint, size, page_count, index_strategy, upload_time, payload_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  owner_user_id=excluded.owner_user_id, name=excluded.name, original_name=excluded.original_name, status=excluded.status,
                  fingerprint=excluded.fingerprint, size=excluded.size, page_count=excluded.page_count,
                  index_strategy=excluded.index_strategy, upload_time=excluded.upload_time, payload_json=excluded.payload_json
                """,
                (
                    doc.get("id"),
                    owner_user_id,
                    doc.get("name"),
                    doc.get("original_name"),
                    doc.get("status"),
                    doc.get("fingerprint"),
                    int(doc.get("size") or 0),
                    int(doc.get("page_count") or 0),
                    doc.get("index_strategy"),
                    doc.get("upload_time"),
                    json.dumps(doc, ensure_ascii=False),
                ),
            )

    def delete_document(self, document_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM documents WHERE id=?", (document_id,))

    def list_documents(self, owner_user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            if owner_user_id:
                rows = conn.execute("SELECT payload_json FROM documents WHERE owner_user_id=? ORDER BY upload_time DESC", (owner_user_id,)).fetchall()
            else:
                rows = conn.execute("SELECT payload_json FROM documents ORDER BY upload_time DESC").fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def upsert_task(self, task: Dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks(id, document_id, status, progress, cancelled, created_time, updated_time, payload_json)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  document_id=excluded.document_id, status=excluded.status, progress=excluded.progress,
                  cancelled=excluded.cancelled, updated_time=excluded.updated_time, payload_json=excluded.payload_json
                """,
                (
                    task.get("id"),
                    task.get("document_id"),
                    task.get("status"),
                    int(task.get("progress") or 0),
                    1 if task.get("cancelled") else 0,
                    task.get("created_time"),
                    task.get("updated_time"),
                    json.dumps(task, ensure_ascii=False),
                ),
            )

    def set_setting(self, key: str, value: Any, updated_time: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO settings(key, value_json, updated_time) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_time=excluded.updated_time
                """,
                (key, json.dumps(value, ensure_ascii=False), updated_time),
            )

    def get_setting(self, key: str) -> Optional[Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value_json"]) if row else None

    def save_conversation(
        self,
        conversation_id: str,
        document_ids: List[str],
        mode: str,
        messages: List[Dict[str, Any]],
        created_time: str,
        updated_time: str,
        owner_user_id: str = DEFAULT_OWNER_ID,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO conversations(id, owner_user_id, document_ids_json, mode, messages_json, created_time, updated_time)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  owner_user_id=excluded.owner_user_id, document_ids_json=excluded.document_ids_json, mode=excluded.mode,
                  messages_json=excluded.messages_json, updated_time=excluded.updated_time
                """,
                (
                    conversation_id,
                    owner_user_id,
                    json.dumps(document_ids, ensure_ascii=False),
                    mode,
                    json.dumps(messages, ensure_ascii=False),
                    created_time,
                    updated_time,
                ),
            )

    def list_conversations(self, limit: int = 50, owner_user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            if owner_user_id:
                rows = conn.execute(
                    "SELECT * FROM conversations WHERE owner_user_id=? ORDER BY updated_time DESC LIMIT ?",
                    (owner_user_id, max(1, min(200, int(limit)))),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM conversations ORDER BY updated_time DESC LIMIT ?",
                    (max(1, min(200, int(limit))),),
                ).fetchall()
        return [
            {
                "id": row["id"],
                "owner_user_id": row["owner_user_id"] or DEFAULT_OWNER_ID,
                "document_ids": json.loads(row["document_ids_json"]),
                "mode": row["mode"],
                "messages": json.loads(row["messages_json"]),
                "created_time": row["created_time"],
                "updated_time": row["updated_time"],
            }
            for row in rows
        ]

    def get_conversation(self, conversation_id: str, owner_user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            if owner_user_id:
                row = conn.execute("SELECT * FROM conversations WHERE id=? AND owner_user_id=?", (conversation_id, owner_user_id)).fetchone()
            else:
                row = conn.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "owner_user_id": row["owner_user_id"] or DEFAULT_OWNER_ID,
            "document_ids": json.loads(row["document_ids_json"]),
            "mode": row["mode"],
            "messages": json.loads(row["messages_json"]),
            "created_time": row["created_time"],
            "updated_time": row["updated_time"],
        }

    def delete_conversation(self, conversation_id: str, owner_user_id: Optional[str] = None) -> bool:
        with self._connect() as conn:
            if owner_user_id:
                cursor = conn.execute("DELETE FROM conversations WHERE id=? AND owner_user_id=?", (conversation_id, owner_user_id))
            else:
                cursor = conn.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
            return cursor.rowcount > 0

    def delete_conversations_for_user(self, owner_user_id: str) -> int:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM conversations WHERE owner_user_id=?", (owner_user_id,))
            return int(cursor.rowcount or 0)

    def create_user(self, username: str, password_hash: str, created_time: str, is_admin: bool = False) -> Dict[str, Any]:
        user = {"id": "user_" + secrets.token_hex(8), "username": username, "is_admin": bool(is_admin), "created_time": created_time}
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO users(id, username, password_hash, is_admin, created_time) VALUES(?,?,?,?,?)",
                (user["id"], username, password_hash, 1 if is_admin else 0, created_time),
            )
        return user

    def list_users(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                  users.id,
                  users.username,
                  users.is_admin,
                  users.created_time,
                  COUNT(DISTINCT documents.id) AS document_count,
                  COUNT(DISTINCT conversations.id) AS conversation_count
                FROM users
                LEFT JOIN documents ON documents.owner_user_id = users.id
                LEFT JOIN conversations ON conversations.owner_user_id = users.id
                GROUP BY users.id
                ORDER BY users.created_time ASC
                """
            ).fetchall()
        return [
            {
                "id": row["id"],
                "username": row["username"],
                "is_admin": bool(row["is_admin"]),
                "created_time": row["created_time"],
                "document_count": int(row["document_count"] or 0),
                "conversation_count": int(row["conversation_count"] or 0),
            }
            for row in rows
        ]

    def user_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()
        return int(row["count"] if row else 0)

    def admin_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM users WHERE is_admin=1").fetchone()
        return int(row["count"] if row else 0)

    def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            return None
        user = dict(row)
        user["is_admin"] = bool(user.get("is_admin"))
        return user

    def get_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT id, username, is_admin, created_time FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            return None
        user = dict(row)
        user["is_admin"] = bool(user.get("is_admin"))
        return user

    def update_user_password(self, user_id: str, password_hash: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id))
            return cursor.rowcount > 0

    def delete_user(self, user_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            return cursor.rowcount > 0

    def create_session(self, user_id: str, token: str, created_time: str, ttl_seconds: int = 60 * 60 * 24 * 30) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions(token, user_id, created_time, expires_at) VALUES(?,?,?,?)",
                (token, user_id, created_time, int(time.time()) + ttl_seconds),
            )

    def get_session_user(self, token: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT users.id, users.username, users.is_admin, users.created_time
                FROM sessions JOIN users ON users.id = sessions.user_id
                WHERE sessions.token=? AND sessions.expires_at>?
                """,
                (token, int(time.time())),
            ).fetchone()
        if not row:
            return None
        user = dict(row)
        user["is_admin"] = bool(user.get("is_admin"))
        return user

    def delete_session(self, token: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token=?", (token,))

    def delete_sessions_for_user(self, user_id: str) -> int:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            return int(cursor.rowcount or 0)

    def claim_legacy_content(self, owner_user_id: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE documents SET owner_user_id=? WHERE owner_user_id IS NULL OR owner_user_id=? OR owner_user_id=''", (owner_user_id, DEFAULT_OWNER_ID))
            conn.execute("UPDATE conversations SET owner_user_id=? WHERE owner_user_id IS NULL OR owner_user_id=? OR owner_user_id=''", (owner_user_id, DEFAULT_OWNER_ID))
