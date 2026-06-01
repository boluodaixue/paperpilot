"""
EvidenceStore：结构化证据的 SQLite 持久化 + 语义去重。

独立于 M4 SharedMemoryStore，避免侵入既有去重/矛盾检测机制；
复用 Embedder（无 key 时确定性 hash 兜底）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Optional

import numpy as np

from ..memory.embedder import Embedder
from .schemas import Evidence

logger = logging.getLogger(__name__)

_DEDUP_THRESHOLD = 0.92


class EvidenceStore:
    """evidence 表 CRUD + claim 级向量去重。"""

    def __init__(
        self,
        db_path: str = "data/memory.db",
        embedder: Optional[Embedder] = None,
        session_id: str = "",
    ) -> None:
        self.db_path = db_path
        self.embedder = embedder or Embedder()
        self._lock = threading.RLock()
        # 空 session_id 自动生成 run id，避免跨 run 证据混入报告
        self.session_id = session_id or f"run-{uuid.uuid4().hex[:12]}"
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._ensure_tables()

    def _connect(self) -> sqlite3.Connection:
        """SQLite 连接非线程安全，每次操作新建连接。"""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_tables(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS evidence (
                        session_id TEXT NOT NULL DEFAULT '',
                        evidence_id TEXT NOT NULL,
                        query TEXT NOT NULL,
                        paper_id TEXT NOT NULL,
                        paper_title TEXT NOT NULL,
                        source_url TEXT NOT NULL DEFAULT '',
                        claim TEXT NOT NULL,
                        evidence_text TEXT NOT NULL DEFAULT '',
                        confidence REAL NOT NULL DEFAULT 0.5,
                        topic TEXT NOT NULL DEFAULT '',
                        embedding_json TEXT NOT NULL,
                        PRIMARY KEY (session_id, evidence_id)
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_evidence_session ON evidence(session_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_evidence_paper ON evidence(paper_id)"
                )
                conn.commit()
            finally:
                conn.close()

    def _next_evidence_id(self) -> str:
        """生成当前 session 内递增的证据 ID：E-1, E-2, ..."""
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT evidence_id FROM evidence WHERE session_id = ?",
                    (self.session_id,),
                )
                max_seq = 0
                for (eid,) in cur.fetchall():
                    try:
                        seq = int(str(eid).split("-", 1)[1])
                        max_seq = max(max_seq, seq)
                    except (ValueError, IndexError):
                        continue
                return f"E-{max_seq + 1}"
            finally:
                conn.close()

    def put(self, evidence: Evidence) -> str:
        """写入证据：claim 级去重（同 session 同论文，cos > 0.92 跳过），返回最终 evidence_id。"""
        evidence.session_id = self.session_id
        if not evidence.embedding:
            evidence.embedding = self.embedder.encode(evidence.claim)

        vec = np.array(evidence.embedding, dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        vec = vec / norm if norm > 1e-9 else None

        if vec is not None:
            with self._lock:
                conn = self._connect()
                try:
                    cur = conn.execute(
                        "SELECT evidence_id, embedding_json FROM evidence "
                        "WHERE session_id = ? AND paper_id = ?",
                        (self.session_id, evidence.paper_id),
                    )
                    rows = cur.fetchall()
                finally:
                    conn.close()
            for row in rows:
                try:
                    emb = np.array(json.loads(row["embedding_json"]), dtype=np.float32)
                except (ValueError, TypeError):
                    continue
                n = float(np.linalg.norm(emb))
                if n < 1e-9:
                    continue
                sim = float(emb.dot(vec) / n)
                if sim > _DEDUP_THRESHOLD:
                    logger.info(
                        f"[Evidence] 重复证据已跳过: {row['evidence_id']} (cos={sim:.3f})"
                    )
                    return row["evidence_id"]

        evidence.evidence_id = self._next_evidence_id()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO evidence
                    (evidence_id, query, paper_id, paper_title, source_url, claim,
                     evidence_text, confidence, topic, session_id, embedding_json)
                    VALUES
                    (:evidence_id, :query, :paper_id, :paper_title, :source_url, :claim,
                     :evidence_text, :confidence, :topic, :session_id, :embedding_json)
                    """,
                    evidence.to_dict(),
                )
                conn.commit()
            finally:
                conn.close()
        logger.info(f"[Evidence] 已存储 {evidence.evidence_id}: {evidence.claim[:60]}...")
        return evidence.evidence_id

    def get_all(self, session_id: Optional[str] = None) -> list[Evidence]:
        """加载证据，默认当前 session。"""
        scope = session_id if session_id is not None else self.session_id
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT * FROM evidence WHERE session_id = ? ORDER BY evidence_id",
                    (scope,),
                )
                return [Evidence.from_row(r) for r in cur.fetchall()]
            finally:
                conn.close()

    def get_by_paper(self, paper_id: str) -> list[Evidence]:
        """按论文 ID 查询当前 session 的证据。"""
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT * FROM evidence WHERE paper_id = ? AND session_id = ? "
                    "ORDER BY evidence_id",
                    (paper_id, self.session_id),
                )
                return [Evidence.from_row(r) for r in cur.fetchall()]
            finally:
                conn.close()

    def count(self, session_id: Optional[str] = None) -> int:
        """统计证据数量，默认当前 session。"""
        scope = session_id if session_id is not None else self.session_id
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM evidence WHERE session_id = ?", (scope,)
                )
                return int(cur.fetchone()[0])
            finally:
                conn.close()
