"""
EvidenceGraph：证据关系图。

节点：evidence / paper / topic
边：SUPPORTS / CONTRADICTS / EXTENDS（evidence↔evidence，无向语义）
    SOURCED_FROM（evidence→paper）/ ANSWERS（evidence→topic，有向结构）

SQLite 持久化边行，NetworkX 提供查询/分析层。
关系检测为启发式（cosine 分带 + 反义判断），零 LLM 成本、确定性可测。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np

from ..memory.embedder import Embedder
from ..memory.memory_store import _is_semantically_opposite
from .schemas import Evidence

logger = logging.getLogger(__name__)

# 近重复阈值（与 memory_store._DEDUP_THRESHOLD 一致）：高于此值视为同一 claim
DEDUP_THRESHOLD = 0.92
# 语义相关下界：低于此值认为无关
CONFLICT_LOW = 0.65
# 无向语义关系（落库时 canonical 排序，避免 (A,B)+(B,A) 重复）
UNDIRECTED_RELATIONS = {"SUPPORTS", "CONTRADICTS", "EXTENDS"}


@dataclass
class EdgeRecord:
    """一条证据关系边。"""

    session_id: str
    source_id: str
    target_id: str
    relation: str
    weight: float
    source_claim: str = ""
    target_claim: str = ""
    source_paper: str = ""
    target_paper: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "relation": self.relation,
            "weight": self.weight,
            "source_claim": self.source_claim,
            "target_claim": self.target_claim,
            "source_paper": self.source_paper,
            "target_paper": self.target_paper,
        }

    @classmethod
    def from_row(cls, row: Any) -> "EdgeRecord":
        return cls(
            session_id=row["session_id"],
            source_id=row["source_id"],
            target_id=row["target_id"],
            relation=row["relation"],
            weight=row["weight"],
            source_claim=row["source_claim"],
            target_claim=row["target_claim"],
            source_paper=row["source_paper"],
            target_paper=row["target_paper"],
            created_at=row["created_at"],
        )


class EvidenceGraph:
    """证据关系图：边持久化 + 启发式关系检测 + NetworkX 查询层。"""

    def __init__(
        self,
        db_path: str = "data/memory.db",
        embedder: Optional[Embedder] = None,
        session_id: str = "",
        supports_threshold: float = 0.75,
    ) -> None:
        self.db_path = db_path
        self.embedder = embedder or Embedder()
        self._lock = threading.RLock()
        self.session_id = session_id or f"run-{uuid.uuid4().hex[:12]}"
        self.supports_threshold = supports_threshold
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._ensure_tables()
        # 惰性语义索引（证据 id / claim / paper / embedding 矩阵）
        self._index_ids: list[str] = []
        self._index_claims: dict[str, str] = {}
        self._index_papers: dict[str, str] = {}
        self._index_paper_ids: dict[str, str] = {}
        self._index_embeddings: np.ndarray = np.zeros((0, self.embedder.dim), dtype=np.float32)
        self._index_count: int = -1

    # ------------------------------------------------------------------
    # 存储层
    # ------------------------------------------------------------------

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
                    CREATE TABLE IF NOT EXISTS evidence_edges (
                        session_id TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        target_id TEXT NOT NULL,
                        relation TEXT NOT NULL,
                        weight REAL NOT NULL,
                        source_claim TEXT NOT NULL DEFAULT '',
                        target_claim TEXT NOT NULL DEFAULT '',
                        source_paper TEXT NOT NULL DEFAULT '',
                        target_paper TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        PRIMARY KEY (session_id, source_id, target_id, relation)
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_edges_session ON evidence_edges(session_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_edges_relation ON evidence_edges(relation)"
                )
                conn.commit()
            finally:
                conn.close()

    def _insert_edge(self, record: EdgeRecord) -> None:
        """落库一条边（canonical 排序 + INSERT OR IGNORE 防重）。"""
        if record.relation in UNDIRECTED_RELATIONS:
            record.source_id, record.target_id = _canonical_pair(
                record.source_id, record.target_id
            )
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO evidence_edges
                    (session_id, source_id, target_id, relation, weight,
                     source_claim, target_claim, source_paper, target_paper, created_at)
                    VALUES
                    (:session_id, :source_id, :target_id, :relation, :weight,
                     :source_claim, :target_claim, :source_paper, :target_paper, :created_at)
                    """,
                    {
                        **record.to_dict(),
                        "created_at": record.created_at,
                    },
                )
                conn.commit()
            finally:
                conn.close()

    def _query_edges(self, session_id: str, where: str = "", params: tuple = (),
                     limit: Optional[int] = None) -> list[EdgeRecord]:
        sql = "SELECT * FROM evidence_edges WHERE session_id = ?"
        if where:
            sql += f" AND {where}"
        sql += " ORDER BY weight DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params = (*params, limit)
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(sql, (session_id, *params))
                return [EdgeRecord.from_row(r) for r in cur.fetchall()]
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # 语义索引
    # ------------------------------------------------------------------

    def _load_evidence_rows(self, session_id: str) -> list[sqlite3.Row]:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT evidence_id, claim, paper_id, paper_title, topic, embedding_json "
                    "FROM evidence WHERE session_id = ? ORDER BY evidence_id",
                    (session_id,),
                )
                return cur.fetchall()
            finally:
                conn.close()

    def _ensure_index(self) -> None:
        """索引按需重建：evidence 表行数变化时重载（单 run 证据量小，全量重建足够）。"""
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM evidence WHERE session_id = ?",
                    (self.session_id,),
                )
                count = int(cur.fetchone()[0])
            finally:
                conn.close()
        if count == self._index_count:
            return
        rows = self._load_evidence_rows(self.session_id)
        ids: list[str] = []
        claims: dict[str, str] = {}
        papers: dict[str, str] = {}
        paper_ids: dict[str, str] = {}
        mats: list[np.ndarray] = []
        for r in rows:
            eid = r["evidence_id"]
            ids.append(eid)
            claims[eid] = r["claim"]
            papers[eid] = r["paper_title"] or r["paper_id"]
            paper_ids[eid] = r["paper_id"]
            try:
                emb = np.array(json.loads(r["embedding_json"]), dtype=np.float32)
            except (ValueError, TypeError):
                emb = np.zeros(self.embedder.dim, dtype=np.float32)
            norm = float(np.linalg.norm(emb))
            mats.append(emb / norm if norm > 1e-9 else np.zeros(self.embedder.dim, dtype=np.float32))
        self._index_ids = ids
        self._index_claims = claims
        self._index_papers = papers
        self._index_paper_ids = paper_ids
        self._index_embeddings = np.vstack(mats) if mats else np.zeros((0, self.embedder.dim), dtype=np.float32)
        self._index_count = count

    # ------------------------------------------------------------------
    # 核心：新增一条证据
    # ------------------------------------------------------------------

    def add_evidence(self, ev: Evidence, evidence_id: Optional[str] = None) -> None:
        """为一条新证据落库结构边并做语义关系检测。

        effective_id 用 evidence_store.put() 的返回值；若与 ev.evidence_id 不一致
        说明 put 去重命中（该 claim 已存在），跳过语义扫描。
        """
        effective_id = evidence_id or ev.evidence_id
        if not effective_id:
            return

        # 结构边：evidence → paper / topic
        self._insert_edge(EdgeRecord(
            session_id=self.session_id,
            source_id=effective_id,
            target_id=ev.paper_id or "unknown",
            relation="SOURCED_FROM",
            weight=1.0,
            source_claim=ev.claim,
            target_paper=ev.paper_title,
        ))
        if ev.topic:
            self._insert_edge(EdgeRecord(
                session_id=self.session_id,
                source_id=effective_id,
                target_id=f"topic:{ev.topic}",
                relation="ANSWERS",
                weight=1.0,
                source_claim=ev.claim,
                target_claim=ev.topic,
            ))

        # 去重命中：跳过语义扫描
        if effective_id != ev.evidence_id:
            return

        if not ev.embedding:
            ev.embedding = self.embedder.encode(ev.claim)
        self._ensure_index()
        new_vec = np.array(ev.embedding, dtype=np.float32)
        n = float(np.linalg.norm(new_vec))
        if n > 1e-9:
            new_vec = new_vec / n
        else:
            return

        for i, eid in enumerate(self._index_ids):
            if eid == effective_id:
                continue
            if self._index_paper_ids.get(eid) == ev.paper_id:
                continue  # 同论文自配对，不加语义边
            sim = float(self._index_embeddings[i].dot(new_vec))
            relation: Optional[str] = None
            if sim >= DEDUP_THRESHOLD:
                continue  # 近重复（put 已处理），不加 SUPPORTS 边
            if sim >= self.supports_threshold:
                relation = "SUPPORTS"
            elif sim > CONFLICT_LOW:
                if _is_semantically_opposite(ev.claim, self._index_claims.get(eid, "")):
                    relation = "CONTRADICTS"
                else:
                    relation = "EXTENDS"
            if relation is None:
                continue
            src, tgt = _canonical_pair(effective_id, eid)
            self._insert_edge(EdgeRecord(
                session_id=self.session_id,
                source_id=src,
                target_id=tgt,
                relation=relation,
                weight=round(sim, 4),
                source_claim=self._index_claims.get(src, ev.claim),
                target_claim=self._index_claims.get(tgt, ev.claim),
                source_paper=self._index_papers.get(src, ""),
                target_paper=self._index_papers.get(tgt, ""),
            ))

    # ------------------------------------------------------------------
    # 查询 API
    # ------------------------------------------------------------------

    def get_relations(self, evidence_id: str, session_id: Optional[str] = None) -> list[EdgeRecord]:
        """查询某条证据的全部关系（双向）。"""
        scope = session_id or self.session_id
        return self._query_edges(
            scope,
            where="(source_id = ? OR target_id = ?)",
            params=(evidence_id, evidence_id),
        )

    def get_contradictions(self, session_id: Optional[str] = None, limit: Optional[int] = None) -> list[EdgeRecord]:
        scope = session_id or self.session_id
        return self._query_edges(scope, where="relation = 'CONTRADICTS'", limit=limit)

    def get_supports(self, session_id: Optional[str] = None, limit: Optional[int] = None) -> list[EdgeRecord]:
        scope = session_id or self.session_id
        return self._query_edges(scope, where="relation = 'SUPPORTS'", limit=limit)

    def get_extends(self, session_id: Optional[str] = None, limit: Optional[int] = None) -> list[EdgeRecord]:
        scope = session_id or self.session_id
        return self._query_edges(scope, where="relation = 'EXTENDS'", limit=limit)

    def graph_stats(self, session_id: Optional[str] = None) -> dict[str, Any]:
        """节点/边统计（供报告与后续 RCS 使用）。"""
        scope = session_id or self.session_id
        rows = self._load_evidence_rows(scope)
        evidence_count = len(rows)
        papers = {r["paper_id"] for r in rows if r["paper_id"]}
        topics = {r["topic"] for r in rows if r["topic"]}
        edges = self._query_edges(scope)
        relation_counts: dict[str, int] = {}
        for e in edges:
            relation_counts[e.relation] = relation_counts.get(e.relation, 0) + 1
        return {
            "session_id": scope,
            "nodes": {"evidence": evidence_count, "paper": len(papers), "topic": len(topics)},
            "edges": relation_counts,
        }

    def to_networkx(self, session_id: Optional[str] = None) -> Any:
        """构建 NetworkX MultiDiGraph（3.x 稳定 API）。"""
        import networkx as nx

        scope = session_id or self.session_id
        G = nx.MultiDiGraph()
        for r in self._load_evidence_rows(scope):
            G.add_node(r["evidence_id"], type="evidence", label=r["evidence_id"])
            if r["paper_id"]:
                G.add_node(r["paper_id"], type="paper", label=r["paper_title"])
            if r["topic"]:
                G.add_node(f"topic:{r['topic']}", type="topic", label=r["topic"])
        for e in self._query_edges(scope):
            G.add_edge(
                e.source_id,
                e.target_id,
                relation=e.relation,
                weight=e.weight,
            )
        return G

    def export_json(self, session_id: Optional[str] = None) -> dict[str, Any]:
        """导出为 JSON（可视化/调试用）。"""
        G = self.to_networkx(session_id)
        return {
            "nodes": [
                {"id": nid, "type": data.get("type", ""), "label": data.get("label", nid)}
                for nid, data in G.nodes(data=True)
            ],
            "edges": [
                {"source": u, "target": v, "relation": data["relation"], "weight": data["weight"]}
                for u, v, data in G.edges(data=True)
            ],
        }


def _canonical_pair(a: str, b: str) -> tuple[str, str]:
    """无向边的 canonical 排序，避免 (A,B) 与 (B,A) 重复。"""
    return (a, b) if a <= b else (b, a)
