"""
Long-term Memory 模块：SQLite 持久化 + 结构化查询

设计决策：
1. SQLite 零运维、文件级持久化、ACID 事务，适合 <100K 条知识库
2. embedding 以 JSON 文本存储，加载时反序列化为 numpy 数组
3. 所有写操作通过 threading.Lock 保证线程安全
4. metadata 使用 JSON 字段，保持 schema 灵活性
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np


logger = logging.getLogger(__name__)


@dataclass
class MemoryEntry:
    """记忆条目数据模型。"""

    entry_id: str
    claim: str              # 核心信息（一句话）
    source: str             # 来源 URL/标题
    confidence: float       # 0-1
    agent_id: str           # 写入的 agent
    timestamp: float
    evidence_type: str      # "primary" | "secondary" | "inference"
    embedding: list[float]  # 语义向量
    topic: str
    metadata: dict[str, Any] = field(default_factory=dict)
    session_id: str = ""    # 所属会话 ID，空字符串表示全局/未分类

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict，embedding 和 metadata 转为 JSON 字符串。"""
        return {
            "entry_id": self.entry_id,
            "claim": self.claim,
            "source": self.source,
            "confidence": self.confidence,
            "agent_id": self.agent_id,
            "timestamp": self.timestamp,
            "evidence_type": self.evidence_type,
            "embedding_json": json.dumps(self.embedding, ensure_ascii=False),
            "topic": self.topic,
            "metadata_json": json.dumps(self.metadata, ensure_ascii=False),
            "session_id": self.session_id,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> MemoryEntry:
        """从 SQLite Row 反序列化。"""
        return cls(
            entry_id=row["entry_id"],
            claim=row["claim"],
            source=row["source"],
            confidence=row["confidence"],
            agent_id=row["agent_id"],
            timestamp=row["timestamp"],
            evidence_type=row["evidence_type"],
            embedding=json.loads(row["embedding_json"]),
            topic=row["topic"],
            metadata=json.loads(row["metadata_json"]),
            session_id=row["session_id"] if "session_id" in row.keys() else "",
        )


@dataclass
class ConflictRecord:
    """矛盾记录数据模型。"""

    conflict_id: str
    entry_id_1: str
    entry_id_2: str
    claim_1: str
    claim_2: str
    similarity: float       # 两个 entry 的 cosine similarity
    status: str             # "open" | "resolved" | "dismissed"
    resolution: Optional[str] = None  # 消解结果 entry_id
    detected_at: float = field(default_factory=time.time)


class LongTermMemory:
    """
    长期记忆 SQLite 封装。

    提供 entries 和 conflicts 两张表的 CRUD，以及基础过滤查询。
    上层 SharedMemoryStore 负责向量相似度计算和高级语义操作。
    """

    def __init__(self, db_path: str = "memory.db") -> None:
        """
        初始化并建表。

        Args:
            db_path: SQLite 数据库文件路径
        """
        self.db_path = db_path
        self._lock = threading.RLock()
        # 自动创建父目录（SQLite 不会在目录不存在时自动创建）
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._ensure_tables()
        self._migrate_add_session_id()

    def _connect(self) -> sqlite3.Connection:
        """创建新连接（SQLite 连接不是线程安全的，每次操作新建连接）。"""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_tables(self) -> None:
        """初始化表结构（如果不存在）。"""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS entries (
                        entry_id TEXT PRIMARY KEY,
                        claim TEXT NOT NULL,
                        source TEXT NOT NULL,
                        confidence REAL NOT NULL,
                        agent_id TEXT NOT NULL,
                        timestamp REAL NOT NULL,
                        evidence_type TEXT NOT NULL,
                        embedding_json TEXT NOT NULL,
                        topic TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        session_id TEXT NOT NULL DEFAULT ''
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_entries_topic ON entries(topic)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_entries_agent ON entries(agent_id)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_entries_timestamp ON entries(timestamp)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS conflicts (
                        conflict_id TEXT PRIMARY KEY,
                        entry_id_1 TEXT NOT NULL,
                        entry_id_2 TEXT NOT NULL,
                        claim_1 TEXT NOT NULL,
                        claim_2 TEXT NOT NULL,
                        similarity REAL NOT NULL,
                        status TEXT NOT NULL DEFAULT 'open',
                        resolution TEXT,
                        detected_at REAL NOT NULL,
                        FOREIGN KEY (entry_id_1) REFERENCES entries(entry_id),
                        FOREIGN KEY (entry_id_2) REFERENCES entries(entry_id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_conflicts_status ON conflicts(status)
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def _migrate_add_session_id(self) -> None:
        """为已存在的旧表添加 session_id 列（向后兼容）。"""
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("PRAGMA table_info(entries)")
                columns = [row["name"] for row in cur.fetchall()]
                if "session_id" not in columns:
                    conn.execute("ALTER TABLE entries ADD COLUMN session_id TEXT NOT NULL DEFAULT ''")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_session ON entries(session_id)")
                    conn.commit()
                    logger.info("[LongTermMemory] 已迁移：为 entries 表添加 session_id 列")
            except Exception as e:
                logger.warning(f"[LongTermMemory] session_id 迁移失败（可能已存在）: {e}")
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Entries 操作
    # ------------------------------------------------------------------

    def insert_entry(self, entry: MemoryEntry) -> None:
        """插入或替换 entry（REPLACE 语义）。"""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO entries
                    (entry_id, claim, source, confidence, agent_id, timestamp,
                     evidence_type, embedding_json, topic, metadata_json, session_id)
                    VALUES
                    (:entry_id, :claim, :source, :confidence, :agent_id, :timestamp,
                     :evidence_type, :embedding_json, :topic, :metadata_json, :session_id)
                    """,
                    entry.to_dict(),
                )
                conn.commit()
            finally:
                conn.close()

    def get_entry(self, entry_id: str) -> Optional[MemoryEntry]:
        """按 ID 查询单条 entry。"""
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT * FROM entries WHERE entry_id = ?", (entry_id,)
                )
                row = cur.fetchone()
                return MemoryEntry.from_row(row) if row else None
            finally:
                conn.close()

    def get_all_entries(self, session_id: Optional[str] = None) -> list[MemoryEntry]:
        """加载 entries。若指定 session_id，只加载该会话的数据。"""
        with self._lock:
            conn = self._connect()
            try:
                if session_id is not None:
                    cur = conn.execute("SELECT * FROM entries WHERE session_id = ?", (session_id,))
                else:
                    cur = conn.execute("SELECT * FROM entries")
                return [MemoryEntry.from_row(r) for r in cur.fetchall()]
            finally:
                conn.close()

    def query_by_topic(self, topic: str) -> list[MemoryEntry]:
        """按 topic 精确查询。"""
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT * FROM entries WHERE topic = ? ORDER BY timestamp DESC",
                    (topic,),
                )
                return [MemoryEntry.from_row(r) for r in cur.fetchall()]
            finally:
                conn.close()

    def query_by_agent(self, agent_id: str) -> list[MemoryEntry]:
        """按 agent_id 查询。"""
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT * FROM entries WHERE agent_id = ? ORDER BY timestamp DESC",
                    (agent_id,),
                )
                return [MemoryEntry.from_row(r) for r in cur.fetchall()]
            finally:
                conn.close()

    def delete_entry(self, entry_id: str) -> bool:
        """删除 entry 及其关联的 conflicts。返回是否成功删除。"""
        with self._lock:
            conn = self._connect()
            try:
                # 先删除关联 conflicts
                conn.execute(
                    "DELETE FROM conflicts WHERE entry_id_1 = ? OR entry_id_2 = ?",
                    (entry_id, entry_id),
                )
                cur = conn.execute(
                    "DELETE FROM entries WHERE entry_id = ?", (entry_id,)
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def get_sessions(self) -> list[dict[str, Any]]:
        """列出所有 session_id 及其条目数、最后更新时间。"""
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    SELECT session_id, COUNT(*) as count, MAX(timestamp) as last_update
                    FROM entries
                    WHERE session_id != ''
                    GROUP BY session_id
                    ORDER BY last_update DESC
                    """
                )
                return [
                    {
                        "session_id": r["session_id"],
                        "count": r["count"],
                        "last_update": r["last_update"],
                    }
                    for r in cur.fetchall()
                ]
            finally:
                conn.close()

    def get_entries_by_session(self, session_id: str) -> list[MemoryEntry]:
        """按 session_id 查询所有 entries。"""
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT * FROM entries WHERE session_id = ? ORDER BY timestamp DESC",
                    (session_id,),
                )
                return [MemoryEntry.from_row(r) for r in cur.fetchall()]
            finally:
                conn.close()

    def count_entries(self, session_id: Optional[str] = None) -> int:
        """返回 entries 数量。可指定 session_id 过滤。"""
        with self._lock:
            conn = self._connect()
            try:
                if session_id is not None:
                    cur = conn.execute("SELECT COUNT(*) FROM entries WHERE session_id = ?", (session_id,))
                else:
                    cur = conn.execute("SELECT COUNT(*) FROM entries")
                return cur.fetchone()[0]
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Conflicts 操作
    # ------------------------------------------------------------------

    def insert_conflict(self, record: ConflictRecord) -> None:
        """插入矛盾记录（忽略重复）。"""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO conflicts
                    (conflict_id, entry_id_1, entry_id_2, claim_1, claim_2,
                     similarity, status, resolution, detected_at)
                    VALUES
                    (:conflict_id, :entry_id_1, :entry_id_2, :claim_1, :claim_2,
                     :similarity, :status, :resolution, :detected_at)
                    """,
                    {
                        "conflict_id": record.conflict_id,
                        "entry_id_1": record.entry_id_1,
                        "entry_id_2": record.entry_id_2,
                        "claim_1": record.claim_1,
                        "claim_2": record.claim_2,
                        "similarity": record.similarity,
                        "status": record.status,
                        "resolution": record.resolution,
                        "detected_at": record.detected_at,
                    },
                )
                conn.commit()
            finally:
                conn.close()

    def get_conflicts(self, status: Optional[str] = None) -> list[ConflictRecord]:
        """查询矛盾记录，可指定状态过滤。"""
        with self._lock:
            conn = self._connect()
            try:
                if status:
                    cur = conn.execute(
                        "SELECT * FROM conflicts WHERE status = ? ORDER BY detected_at DESC",
                        (status,),
                    )
                else:
                    cur = conn.execute(
                        "SELECT * FROM conflicts ORDER BY detected_at DESC"
                    )
                rows = cur.fetchall()
                return [
                    ConflictRecord(
                        conflict_id=r["conflict_id"],
                        entry_id_1=r["entry_id_1"],
                        entry_id_2=r["entry_id_2"],
                        claim_1=r["claim_1"],
                        claim_2=r["claim_2"],
                        similarity=r["similarity"],
                        status=r["status"],
                        resolution=r["resolution"],
                        detected_at=r["detected_at"],
                    )
                    for r in rows
                ]
            finally:
                conn.close()

    def update_conflict_resolution(
        self,
        conflict_id: str,
        status: str,
        resolution: Optional[str] = None,
    ) -> bool:
        """更新矛盾记录的状态和消解结果。"""
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    UPDATE conflicts
                    SET status = ?, resolution = ?
                    WHERE conflict_id = ?
                    """,
                    (status, resolution, conflict_id),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def delete_conflict(self, conflict_id: str) -> bool:
        """删除矛盾记录。"""
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "DELETE FROM conflicts WHERE conflict_id = ?", (conflict_id,)
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def get_lowest_score_entries(self, limit: int, session_id: Optional[str] = None) -> list[tuple[str, float]]:
        """
        按综合评分升序返回最低分的 entries，用于淘汰。

        综合评分 = confidence × evidence_weight × recency × conflict_bonus
        这里 evidence_type 权重: primary=1.0, secondary=0.8, inference=0.6
        recency 用时间衰减: exp(-days/30)
        conflict_bonus: 有矛盾的 entry 保留（返回极低分数以便排在最后）
        """
        evidence_weights = {"primary": 1.0, "secondary": 0.8, "inference": 0.6}
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                if session_id is not None:
                    cur = conn.execute(
                        """
                        SELECT e.entry_id, e.confidence, e.evidence_type, e.timestamp,
                               (SELECT COUNT(*) FROM conflicts c
                                WHERE (c.entry_id_1 = e.entry_id OR c.entry_id_2 = e.entry_id)
                                AND c.status = 'open') AS open_conflicts
                        FROM entries e
                        WHERE e.session_id = ?
                        ORDER BY e.timestamp ASC
                        """,
                        (session_id,),
                    )
                else:
                    cur = conn.execute(
                        """
                        SELECT e.entry_id, e.confidence, e.evidence_type, e.timestamp,
                               (SELECT COUNT(*) FROM conflicts c
                                WHERE (c.entry_id_1 = e.entry_id OR c.entry_id_2 = e.entry_id)
                                AND c.status = 'open') AS open_conflicts
                        FROM entries e
                        ORDER BY e.timestamp ASC
                        """
                    )
                rows = cur.fetchall()
                scores: list[tuple[str, float]] = []
                for r in rows:
                    entry_id = r["entry_id"]
                    confidence = r["confidence"]
                    ew = evidence_weights.get(r["evidence_type"], 0.5)
                    days_old = max((now - r["timestamp"]) / 86400.0, 0.0)
                    recency = np.exp(-days_old / 30.0)
                    conflict_bonus = 2.0 if r["open_conflicts"] > 0 else 1.0
                    score = confidence * ew * recency * conflict_bonus
                    scores.append((entry_id, score))
                scores.sort(key=lambda x: x[1])
                return scores[:limit]
            finally:
                conn.close()
