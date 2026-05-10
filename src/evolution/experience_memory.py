"""
M6 自进化引擎 — 经验记忆模块

ExperienceMemory 存储成功/失败 trajectory 的关键模式，支持基于 sentence-transformer
嵌入的相似度检索，以及按综合评分淘汰旧经验。

设计决策：
1. SQLite 持久化：轻量、无需额外服务，适合研究实验环境。
2. embedding 缓存：首次编码后存入数据库，避免重复计算。
3. 淘汰策略：综合评分 = 0.4×质量 + 0.3×新鲜度 + 0.3×实用性，定期清理低分经验。
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from typing import Any

from src.orchestrator.schemas import ResearchReport


__all__ = ["ExperienceMemory"]


class ExperienceMemory:
    """经验记忆：存储、检索和淘汰研究轨迹的关键模式。

    Attributes:
        db_path: SQLite 数据库文件路径。
        embedding_dim: embedding 向量维度（默认 384，对应 all-MiniLM-L6-v2）。
    """

    def __init__(self, db_path: str = "experience.db", embedding_dim: int = 384):
        self.db_path = db_path
        self.embedding_dim = embedding_dim
        self._embedder: Any | None = None
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    # ------------------------------------------------------------------
    # 数据库初始化
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        """创建 SQLite 表结构（若不存在）。"""
        conn = self._get_conn()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS experiences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_summary TEXT NOT NULL,
                trajectory_json TEXT NOT NULL,
                success INTEGER NOT NULL,
                score REAL NOT NULL,
                embedding TEXT,
                created_round INTEGER NOT NULL,
                access_count INTEGER DEFAULT 0,
                last_access_round INTEGER DEFAULT 0
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_exp_round ON experiences(created_round)"
        )
        conn.commit()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    # ------------------------------------------------------------------
    # Embedder 懒加载
    # ------------------------------------------------------------------

    def _get_embedder(self) -> Any | None:
        """懒加载 embedder，优先使用项目已有的 Embedder。"""
        if self._embedder is not None:
            return self._embedder
        try:
            from memory.embedder import Embedder
            self._embedder = Embedder()
        except Exception:
            self._embedder = None
        return self._embedder

    def _encode(self, text: str) -> list[float]:
        """将文本编码为 embedding 向量，失败时返回零向量。"""
        embedder = self._get_embedder()
        if embedder is not None:
            try:
                return embedder.encode(text)
            except Exception:
                pass
        return [0.0] * self.embedding_dim

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """计算两向量的余弦相似度。"""
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return dot / (norm_a * norm_b)

    # ------------------------------------------------------------------
    # 增删改查
    # ------------------------------------------------------------------

    def add(
        self,
        trajectory: list[dict[str, Any]],
        success: bool,
        score: float,
        strategy_summary: str,
        current_round: int = 0,
    ) -> int:
        """添加一条经验到记忆库。

        Args:
            trajectory: 交互轨迹列表。
            success: 是否成功完成任务。
            score: 最终综合评分。
            strategy_summary: 策略摘要（用于 embedding 和人工阅读）。
            current_round: 当前进化轮次，用于新鲜度计算。

        Returns:
            新插入记录的 id。
        """
        embedding = self._encode(strategy_summary)
        embedding_str = json.dumps(embedding)
        traj_str = json.dumps(trajectory, ensure_ascii=False)

        conn = self._get_conn()
        cursor = conn.execute(
            """
            INSERT INTO experiences
            (strategy_summary, trajectory_json, success, score, embedding, created_round, access_count, last_access_round)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                strategy_summary,
                traj_str,
                1 if success else 0,
                score,
                embedding_str,
                current_round,
                current_round,
            ),
        )
        conn.commit()
        return cursor.lastrowid

    def retrieve(
        self,
        query: str,
        top_k: int = 3,
        current_round: int = 0,
    ) -> list[dict[str, Any]]:
        """基于语义相似度检索相关经验。

        Args:
            query: 查询文本（如当前研究问题或策略摘要）。
            top_k: 返回最相关的 k 条经验。
            current_round: 当前进化轮次，用于更新访问统计。

        Returns:
            经验字典列表，按相似度降序排列。
        """
        query_emb = self._encode(query)
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, strategy_summary, trajectory_json, success, score, embedding, "
            "created_round, access_count, last_access_round FROM experiences"
        ).fetchall()

        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            emb_str = row["embedding"] or ""
            if not emb_str:
                continue
            try:
                emb = json.loads(emb_str)
                sim = self._cosine_similarity(query_emb, emb)
                scored.append((sim, row))
            except (json.JSONDecodeError, ValueError):
                continue

        scored.sort(key=lambda x: x[0], reverse=True)
        results: list[dict[str, Any]] = []
        for sim, row in scored[:top_k]:
            results.append(
                {
                    "id": row["id"],
                    "strategy_summary": row["strategy_summary"],
                    "trajectory": json.loads(row["trajectory_json"]),
                    "success": bool(row["success"]),
                    "score": row["score"],
                    "similarity": round(sim, 4),
                    "created_round": row["created_round"],
                }
            )
            # 更新访问统计
            conn.execute(
                "UPDATE experiences SET access_count = access_count + 1, last_access_round = ? WHERE id = ?",
                (current_round, row["id"]),
            )
        conn.commit()
        return results

    def evict_old_experiences(
        self,
        max_age_rounds: int = 5,
        current_round: int = 0,
        retain_min: int = 100,
    ) -> int:
        """淘汰旧经验。

        淘汰规则：
        1. 超过 max_age_rounds 未访问的经验（last_access_round 差距大）。
        2. 综合评分低的经验：composite = 0.4×质量 + 0.3×新鲜度 + 0.3×实用性。
           - 质量: score / 10
           - 新鲜度: 1 - min(age, max_age) / max_age
           - 实用性: min(access_count / 5, 1.0)
        3. 保留至少 retain_min 条。

        Args:
            max_age_rounds: 最大允许的年龄（以轮次计）。
            current_round: 当前进化轮次。
            retain_min: 最少保留条数。

        Returns:
            被删除的记录数。
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, score, created_round, access_count, last_access_round FROM experiences"
        ).fetchall()

        if len(rows) <= retain_min:
            return 0

        # 计算每条记录的综合评分
        scored: list[tuple[float, int]] = []  # (composite_score, id)
        for row in rows:
            age = current_round - row["last_access_round"]
            quality = row["score"] / 10.0
            freshness = 1.0 - min(age, max_age_rounds) / max_age_rounds
            utility = min(row["access_count"] / 5.0, 1.0)
            composite = 0.4 * quality + 0.3 * freshness + 0.3 * utility
            scored.append((composite, row["id"]))

        # 按综合评分升序排序，低分先淘汰
        scored.sort(key=lambda x: x[0])
        to_evict_count = max(0, len(scored) - retain_min)
        to_evict_ids = [sid for _, sid in scored[:to_evict_count]]

        if to_evict_ids:
            placeholders = ",".join("?" * len(to_evict_ids))
            conn.execute(
                f"DELETE FROM experiences WHERE id IN ({placeholders})",
                to_evict_ids,
            )
            conn.commit()
        return len(to_evict_ids)

    def get_stats(self) -> dict[str, Any]:
        """返回记忆库统计信息。"""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) as cnt, AVG(score) as avg_score, "
            "SUM(success) as total_success FROM experiences"
        ).fetchone()
        return {
            "total_experiences": row["cnt"] or 0,
            "avg_score": row["avg_score"] or 0.0,
            "success_rate": (row["total_success"] or 0) / max(row["cnt"], 1),
        }
