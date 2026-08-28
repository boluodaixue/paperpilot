"""
ChatStore：会话对话消息的 SQLite 持久化（Web UI 侧边栏 + 历史消息）。

研究正文只写入 Markdown Memory Store；聊天记录中的报告消息仅保存
manifest 路径引用。重启服务器后仍可回溯会话。使用独立的锁、连接与建表逻辑。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 消息类型
KIND_CHAT = "chat"        # 普通对话（澄清问答）
KIND_PROPOSAL = "proposal"  # 研究方案确认卡
KIND_REPORT = "report"    # Markdown Memory Store manifest 路径引用


class ChatStore:
    """chat_messages 表 CRUD + 会话列表（含首条消息预览）。"""

    def __init__(self, db_path: str = "data/chat.db") -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._ensure_tables()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _ensure_tables(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chat_messages (
                        session_id TEXT NOT NULL,
                        message_id TEXT NOT NULL,
                        role TEXT NOT NULL,
                        kind TEXT NOT NULL DEFAULT 'chat',
                        content TEXT NOT NULL,
                        timestamp REAL NOT NULL,
                        PRIMARY KEY (session_id, message_id)
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_messages_session ON chat_messages(session_id)"
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS session_meta (
                        session_id TEXT PRIMARY KEY,
                        title TEXT NOT NULL DEFAULT '',
                        pinned INTEGER NOT NULL DEFAULT 0,
                        sort_order REAL NOT NULL DEFAULT 0,
                        memory_id TEXT,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                columns = {
                    str(row[1])
                    for row in conn.execute("PRAGMA table_info(session_meta)").fetchall()
                }
                if "memory_id" not in columns:
                    conn.execute("ALTER TABLE session_meta ADD COLUMN memory_id TEXT")
                conn.commit()
            finally:
                conn.close()

    def set_meta(self, session_id: str, title: str | None = None, pinned: bool | None = None) -> None:
        """更新会话元数据（重命名/置顶），UPSERT 语义。"""
        with self._lock:
            conn = self._connect()
            try:
                if title is not None:
                    conn.execute(
                        """
                        INSERT INTO session_meta (session_id, title, pinned, sort_order, updated_at)
                        VALUES (?, ?, 0, 0, ?)
                        ON CONFLICT(session_id) DO UPDATE SET title = excluded.title, updated_at = excluded.updated_at
                        """,
                        (session_id, title, time.time()),
                    )
                if pinned is not None:
                    conn.execute(
                        """
                        INSERT INTO session_meta (session_id, title, pinned, sort_order, updated_at)
                        VALUES (?, '', ?, 0, ?)
                        ON CONFLICT(session_id) DO UPDATE SET pinned = excluded.pinned, updated_at = excluded.updated_at
                        """,
                        (session_id, 1 if pinned else 0, time.time()),
                    )
                conn.commit()
            finally:
                conn.close()

    def set_sort_order(self, session_ids: list[str]) -> None:
        """批量设置排序位置（拖拽后的新顺序）。"""
        with self._lock:
            conn = self._connect()
            try:
                for i, sid in enumerate(session_ids):
                    conn.execute(
                        """
                        INSERT INTO session_meta (session_id, title, pinned, sort_order, updated_at)
                        VALUES (?, '', 0, ?, ?)
                        ON CONFLICT(session_id) DO UPDATE SET sort_order = excluded.sort_order, updated_at = excluded.updated_at
                        """,
                        (sid, float(i), time.time()),
                    )
                conn.commit()
            finally:
                conn.close()

    def bind_memory(self, session_id: str, memory_id: str) -> str:
        """Bind one session to exactly one explicit Memory ID.

        Repeating the same binding is idempotent.  A different later binding is
        rejected so UI state cannot silently move an existing conversation.
        """
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        if not isinstance(memory_id, str) or not memory_id.strip():
            raise ValueError("memory_id must be a non-empty string")
        normalized_session_id = session_id.strip()
        normalized_memory_id = memory_id.strip()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT memory_id FROM session_meta "
                    "WHERE session_id = ?",
                    (normalized_session_id,),
                ).fetchone()
                if row is not None and row["memory_id"] is not None:
                    existing = row["memory_id"]
                    if existing != normalized_memory_id:
                        raise ValueError(
                            "session is already bound to a different Memory"
                        )
                    conn.commit()
                    return existing
                conn.execute(
                    """
                    INSERT INTO session_meta (
                        session_id, title, pinned, sort_order,
                        memory_id, updated_at
                    )
                    VALUES (?, '', 0, 0, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        memory_id = excluded.memory_id,
                        updated_at = excluded.updated_at
                    """,
                    (normalized_session_id, normalized_memory_id, time.time()),
                )
                conn.commit()
                return normalized_memory_id
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def get_memory_binding(self, session_id: str) -> str | None:
        """Return the explicit binding; legacy NULL rows remain unbound."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT memory_id FROM session_meta "
                    "WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                return None if row is None else row["memory_id"]
            finally:
                conn.close()

    def delete_session(self, session_id: str) -> int:
        """删除会话的聊天记录与元数据，返回删除的消息条数。"""
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM chat_messages WHERE session_id = ?", (session_id,)
                )
                n = int(cur.fetchone()[0])
                conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM session_meta WHERE session_id = ?", (session_id,))
                conn.commit()
                return n
            finally:
                conn.close()

    def delete_session_with_workflow_leases(
        self,
        session_id: str,
        lease_tokens: Mapping[str, str],
        *,
        now: float | None = None,
    ) -> tuple[int, tuple[str, ...]]:
        """Atomically delete chat state and the exactly leased workflow locators.

        ``session_meta`` owns the foreign-key cascade for runtime locators and
        outbox rows.  Requiring an exact set of live lease tokens gives session
        deletion one SQLite linearization point without copying workflow state
        into Chat Store.
        """
        current = time.time() if now is None else float(now)
        tokens = dict(lease_tokens)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    "SELECT task_id, thread_id, lease_owner, lease_until "
                    "FROM runtime_workflows WHERE session_id = ? "
                    "ORDER BY task_id",
                    (session_id,),
                ).fetchall()
                task_ids = {str(row["task_id"]) for row in rows}
                if task_ids != set(tokens):
                    raise RuntimeError(
                        "session workflow set changed during deletion"
                    )
                for row in rows:
                    task_id = str(row["task_id"])
                    if (
                        row["lease_owner"] != tokens[task_id]
                        or row["lease_until"] is None
                        or float(row["lease_until"]) < current
                    ):
                        raise RuntimeError(
                            "session workflow lease was lost during deletion"
                        )
                n = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM chat_messages WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()[0]
                )
                thread_ids = tuple(str(row["thread_id"]) for row in rows)
                conn.execute(
                    "DELETE FROM chat_messages WHERE session_id = ?", (session_id,)
                )
                conn.execute(
                    "DELETE FROM session_meta WHERE session_id = ?", (session_id,)
                )
                conn.commit()
                return n, thread_ids
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _next_message_id(self, session_id: str) -> str:
        """生成当前 session 内递增的消息 ID：M-1, M-2, ..."""
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT message_id FROM chat_messages WHERE session_id = ?",
                    (session_id,),
                )
                max_seq = 0
                for (mid,) in cur.fetchall():
                    try:
                        seq = int(str(mid).split("-", 1)[1])
                        max_seq = max(max_seq, seq)
                    except (ValueError, IndexError):
                        continue
                return f"M-{max_seq + 1}"
            finally:
                conn.close()

    def add(self, session_id: str, role: str, kind: str, content: str) -> str:
        """追加一条消息，返回 message_id。"""
        message_id = self._next_message_id(session_id)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO chat_messages (session_id, message_id, role, kind, content, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (session_id, message_id, role, kind, content, time.time()),
                )
                conn.commit()
            finally:
                conn.close()
        return message_id

    def get_messages(self, session_id: str) -> list[dict]:
        """按 message_id 的数字序号返回该 session 全部消息。"""
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT message_id, role, kind, content, timestamp "
                    "FROM chat_messages WHERE session_id = ? "
                    "ORDER BY CAST(SUBSTR(message_id, 3) AS INTEGER), timestamp",
                    (session_id,),
                )
                return [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()

    def list_sessions(self) -> list[dict]:
        """返回侧边栏会话列表：{session_id, title, pinned, last_update, count}。

        title 优先级：session_meta.title（自定义重命名）> 首条 user 消息前 15 字
        > session_id。排序：pinned 置顶优先，组内按 sort_order（拖拽顺序），
        最后按 last_update 兜底。
        """
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT cm.session_id, cm.role, cm.content, cm.timestamp, "
                    "sm.title AS meta_title, sm.pinned, sm.sort_order, "
                    "sm.memory_id "
                    "FROM chat_messages cm "
                    "LEFT JOIN session_meta sm ON sm.session_id = cm.session_id "
                    "ORDER BY cm.session_id, "
                    "CAST(SUBSTR(cm.message_id, 3) AS INTEGER), cm.timestamp"
                )
                rows = cur.fetchall()
                meta_rows = conn.execute(
                    "SELECT session_id, title, pinned, sort_order, updated_at, "
                    "memory_id FROM session_meta"
                ).fetchall()
            finally:
                conn.close()

        by_session: dict[str, dict] = {}
        for r in rows:
            sid = r["session_id"]
            group = by_session.setdefault(
                sid,
                {
                    "session_id": sid, "title": sid, "last_update": 0.0,
                    "count": 0, "_first_user": None,
                    "pinned": bool(r["pinned"]), "sort_order": r["sort_order"] or 0.0,
                    "_meta_title": r["meta_title"] or "",
                    "memory_id": r["memory_id"],
                },
            )
            group["count"] += 1
            group["last_update"] = max(group["last_update"], r["timestamp"])
            if r["role"] == "user" and group["_first_user"] is None:
                group["_first_user"] = (r["content"] or "").strip()

        for r in meta_rows:
            group = by_session.setdefault(
                r["session_id"],
                {
                    "session_id": r["session_id"],
                    "title": r["session_id"],
                    "last_update": float(r["updated_at"]),
                    "count": 0,
                    "_first_user": None,
                    "pinned": bool(r["pinned"]),
                    "sort_order": r["sort_order"] or 0.0,
                    "_meta_title": r["title"] or "",
                    "memory_id": r["memory_id"],
                },
            )
            if group["count"] == 0:
                group["last_update"] = float(r["updated_at"])
            group["pinned"] = bool(r["pinned"])
            group["sort_order"] = r["sort_order"] or 0.0
            group["_meta_title"] = r["title"] or ""
            group["memory_id"] = r["memory_id"]

        result = []
        for g in by_session.values():
            first_user = g.pop("_first_user", None)
            title = g["_meta_title"] or (first_user[:15] if first_user else g["session_id"])
            result.append({
                "session_id": g["session_id"],
                "title": title,
                "pinned": bool(g["pinned"]),
                "sort_order": g["sort_order"],
                "last_update": g["last_update"],
                "count": g["count"],
                "memory_id": g["memory_id"],
            })
        result.sort(key=lambda s: (not s["pinned"], s["sort_order"], -s["last_update"]))
        return result
