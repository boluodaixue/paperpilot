"""
ChatStore：会话对话消息的 SQLite 持久化（Web UI 侧边栏 + 历史消息 + 研究报告留档）。

独立于 M4 记忆存储：Web 端维护"用户-澄清-研究方案-报告"的完整对话流，
重启服务器后仍可回溯。仿 EvidenceStore 的锁/连接/建表模式。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 消息类型
KIND_CHAT = "chat"        # 普通对话（澄清问答）
KIND_PROPOSAL = "proposal"  # 研究方案确认卡
KIND_REPORT = "report"    # 研究报告（Markdown，持久化留档）


class ChatStore:
    """chat_messages 表 CRUD + 会话列表（含首条消息预览）。"""

    def __init__(self, db_path: str = "data/memory.db") -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._ensure_tables()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
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
                conn.commit()
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
        """按 message_id 升序返回该 session 全部消息。"""
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT message_id, role, kind, content, timestamp "
                    "FROM chat_messages WHERE session_id = ? ORDER BY message_id",
                    (session_id,),
                )
                return [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()

    def list_sessions(self) -> list[dict]:
        """返回侧边栏会话列表：{session_id, title, last_update, count}。

        title = 该 session 第一条 user 消息的前 15 字；无 user 消息则用 session_id。
        按 last_update 倒序。
        """
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT session_id, role, content, timestamp FROM chat_messages "
                    "ORDER BY session_id, message_id"
                )
                rows = cur.fetchall()
            finally:
                conn.close()

        by_session: dict[str, dict] = {}
        for r in rows:
            sid = r["session_id"]
            group = by_session.setdefault(
                sid, {"session_id": sid, "title": sid, "last_update": 0.0, "count": 0, "_first_user": None}
            )
            group["count"] += 1
            group["last_update"] = max(group["last_update"], r["timestamp"])
            if r["role"] == "user" and group["_first_user"] is None:
                group["_first_user"] = (r["content"] or "").strip()

        result = []
        for g in by_session.values():
            first_user = g.pop("_first_user", None)
            title = (first_user[:15] if first_user else g["session_id"])
            result.append({
                "session_id": g["session_id"],
                "title": title,
                "last_update": g["last_update"],
                "count": g["count"],
            })
        result.sort(key=lambda s: s["last_update"], reverse=True)
        return result