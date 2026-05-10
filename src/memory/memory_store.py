"""
Shared Memory Store 模块：跨 Agent 统一读写接口

设计决策：
1. 持久层用 LongTermMemory（SQLite），内存层维护 numpy 向量索引，读写平衡
2. 写前自动去重（cosine > 0.92）和矛盾检测（0.65 < cosine < 0.92 + 语义对立）
3. 矛盾消解支持 majority_vote / source_weight / llm_judge 三种策略
4. 淘汰策略综合评分 = confidence × evidence_weight × recency × conflict_bonus
5. get_context_for_query 将相关记忆组装为文本，控制总 token 在预算内
6. 所有内存索引操作受 threading.Lock 保护，保证线程安全
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from typing import Any, Optional

import numpy as np

from src.memory.embedder import Embedder
from src.memory.long_term import ConflictRecord, LongTermMemory, MemoryEntry
from src.utils.tracing import trace_retriever

logger = logging.getLogger(__name__)

# 去重和矛盾检测的相似度阈值
_DEDUP_THRESHOLD = 0.92
_CONFLICT_LOW = 0.65
_CONFLICT_HIGH = 0.92

# evidence_type 权重（用于淘汰评分和矛盾消解）
_EVIDENCE_WEIGHTS = {"primary": 1.0, "secondary": 0.8, "inference": 0.6}

# 常见否定词和反义词列表（用于简单启发式矛盾检测）
_NEGATION_WORDS = {"不", "没", "无", "非", "未", "否", "not", "no", "never", "without", "not"}
_ANTONYM_PAIRS: list[tuple[set[str], set[str]]] = [
    ({"增加", "上升", "增长", "提高", "扩大", "increase", "rise", "grow"},
     {"减少", "下降", "降低", "缩减", "收缩", "decrease", "fall", "drop"}),
    ({"好", "优", "强", "positive", "good", "strong"},
     {"坏", "劣", "弱", "negative", "bad", "weak"}),
    ({"支持", "赞成", "agree", "support"},
     {"反对", "reject", "oppose", "disagree"}),
    ({"成功", "success"}, {"失败", "failure"}),
    ({"高", "high"}, {"低", "low"}),
    ({"大", "big", "large"}, {"小", "small", "tiny"}),
]


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """计算两个向量的 cosine similarity。"""
    norm_a = float(np.linalg.norm(a))
    norm_b = float(np.linalg.norm(b))
    if norm_a < 1e-9 or norm_b < 1e-9:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


def _is_semantically_opposite(claim_a: str, claim_b: str) -> bool:
    """
    简单启发式判断两个 claim 是否语义对立。

    策略：
    1. 检查是否一方含否定词而另一方不含
    2. 检查是否包含反义词对
    """
    ca = claim_a.lower()
    cb = claim_b.lower()

    # 否定词检查：一方含否定，另一方不含，且结构相似
    a_has_neg = any(w in ca for w in _NEGATION_WORDS)
    b_has_neg = any(w in cb for w in _NEGATION_WORDS)
    if a_has_neg != b_has_neg:
        # 进一步检查：去掉否定词后是否高度相似
        # 简单判断：去掉否定词后的 Jaccard 相似度
        def _strip_neg(text: str) -> set[str]:
            words = set(text.split())
            for w in _NEGATION_WORDS:
                words.discard(w)
            return words
        sim_words = len(_strip_neg(ca) & _strip_neg(cb))
        union_words = len(_strip_neg(ca) | _strip_neg(cb))
        if union_words > 0 and sim_words / union_words > 0.5:
            return True

    # 反义词对检查
    for pos_set, neg_set in _ANTONYM_PAIRS:
        a_has_pos = any(w in ca for w in pos_set)
        a_has_neg_word = any(w in ca for w in neg_set)
        b_has_pos = any(w in cb for w in pos_set)
        b_has_neg_word = any(w in cb for w in neg_set)
        if (a_has_pos and b_has_neg_word) or (a_has_neg_word and b_has_pos):
            return True

    return False


class SharedMemoryStore:
    """
    跨 Agent 共享记忆存储。

    提供 put/query/conflict_resolution/evict 等高级语义接口，
    底层通过 LongTermMemory 做 SQLite 持久化，内存中维护 numpy 向量索引加速相似度查询。
    """

    def __init__(
        self,
        db_path: str = "memory.db",
        embedder: Optional[Embedder] = None,
        session_id: str = "",
    ) -> None:
        """
        初始化共享记忆存储。

        Args:
            db_path: SQLite 数据库路径
            embedder: 向量化器，None 时自动创建
            session_id: 会话 ID，空字符串表示加载所有历史数据（不隔离）
        """
        self.lt = LongTermMemory(db_path=db_path)
        self.embedder = embedder or Embedder()
        self._lock = threading.RLock()
        self.session_id = session_id

        # 内存向量索引
        self._entry_ids: list[str] = []
        self._embeddings: np.ndarray = np.zeros((0, self.embedder.dim), dtype=np.float32)
        self._entries_cache: dict[str, MemoryEntry] = {}

        # 加载已有数据到内存索引（按 session 过滤）
        self._rebuild_index()

    # ------------------------------------------------------------------
    # 内部索引管理
    # ------------------------------------------------------------------

    def _rebuild_index(self) -> None:
        """从 SQLite 重建内存向量索引。若指定了 session_id，只加载该会话数据。"""
        entries = self.lt.get_all_entries(session_id=self.session_id or None)
        with self._lock:
            self._entry_ids = [e.entry_id for e in entries]
            self._entries_cache = {e.entry_id: e for e in entries}
            if entries:
                mat = np.array([e.embedding for e in entries], dtype=np.float32)
                # 归一化（防御性）
                norms = np.linalg.norm(mat, axis=1, keepdims=True)
                norms[norms < 1e-9] = 1.0
                self._embeddings = mat / norms
            else:
                self._embeddings = np.zeros((0, self.embedder.dim), dtype=np.float32)
        scope = f"session={self.session_id}" if self.session_id else "all sessions"
        logger.info(f"Memory index rebuilt: {len(entries)} entries loaded ({scope}).")

    def _add_to_index(self, entry: MemoryEntry) -> None:
        """将单条 entry 追加到内存索引。"""
        vec = np.array(entry.embedding, dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        if norm > 1e-9:
            vec = vec / norm
        with self._lock:
            self._entry_ids.append(entry.entry_id)
            self._entries_cache[entry.entry_id] = entry
            if self._embeddings.shape[0] == 0:
                self._embeddings = vec.reshape(1, -1)
            else:
                self._embeddings = np.vstack([self._embeddings, vec.reshape(1, -1)])

    def _remove_from_index(self, entry_id: str) -> None:
        """从内存索引删除 entry。"""
        with self._lock:
            if entry_id not in self._entry_ids:
                return
            idx = self._entry_ids.index(entry_id)
            self._entry_ids.pop(idx)
            self._entries_cache.pop(entry_id, None)
            if self._embeddings.shape[0] > 0:
                self._embeddings = np.delete(self._embeddings, idx, axis=0)

    # ------------------------------------------------------------------
    # 核心接口
    # ------------------------------------------------------------------

    # 垃圾内容检测模式（正则/关键词，写入前过滤）
    _JUNK_PATTERNS = [
        re.compile(r"i'm ready to help", re.I),
        re.compile(r"您好|你好|hello|hi there", re.I),
        re.compile(r"error\s*:\s*error code:\s*\d+", re.I),
        re.compile(r"^\s*error\s*:\s*", re.I),
        re.compile(r"could you please|请问您想要|您想让我", re.I),
    ]

    def _is_junk(self, entry: MemoryEntry) -> bool:
        """启发式判断 entry 是否为低质量/垃圾内容，应拒绝入库。"""
        claim = entry.claim or ""
        # 1. 过短
        if len(claim.strip()) < 30:
            return True
        # 2. confidence 过低
        if entry.confidence < 0.3:
            return True
        # 3. 命中垃圾模式
        for pat in self._JUNK_PATTERNS:
            if pat.search(claim):
                return True
        return False

    @trace_retriever(name="memory.put", tags=["m4", "memory"])
    def put(self, entry: MemoryEntry) -> str:
        """
        写入记忆条目。

        流程：
        1. 质量过滤：低质量/垃圾内容不入库
        2. 若 embedding 为空，自动生成
        3. 去重检测：cosine > 0.92 → 合并（保留 confidence 更高的）
        4. 矛盾检测：0.65 < cosine < 0.92 且语义对立 → 标记 ConflictRecord
        5. 持久化到 SQLite 并更新内存索引

        Args:
            entry: 记忆条目

        Returns:
            entry_id（如果是合并，返回被合并的已有 entry_id）
        """
        # 1. 质量过滤
        if self._is_junk(entry):
            logger.info(f"[M4] Junk entry rejected (conf={entry.confidence:.2f}, len={len(entry.claim)}): {entry.claim[:60]}...")
            return entry.entry_id

        # 2. 确保有 embedding
        if not entry.embedding:
            entry.embedding = self.embedder.encode(entry.claim)

        # 去重检测
        duplicate_id = self._find_duplicate(entry)
        if duplicate_id:
            existing = self.lt.get_entry(duplicate_id)
            if existing and entry.confidence > existing.confidence:
                # 用新 entry 更新，但保留旧 ID
                entry.entry_id = duplicate_id
                entry.timestamp = max(entry.timestamp, existing.timestamp)
                self.lt.insert_entry(entry)
                self._remove_from_index(duplicate_id)
                self._add_to_index(entry)
                logger.info(f"Merged entry {duplicate_id} with higher confidence.")
            else:
                logger.info(f"Duplicate detected, kept existing {duplicate_id}.")
            return duplicate_id

        # 写入 session_id 并持久化
        entry.session_id = self.session_id
        self.lt.insert_entry(entry)
        self._add_to_index(entry)

        # 矛盾检测（与新 entry 比较）
        self._detect_conflicts(entry)

        return entry.entry_id

    def _find_duplicate(self, entry: MemoryEntry) -> Optional[str]:
        """
        在内存索引中搜索与 entry  cosine similarity > 0.92 的已有条目。

        Returns:
            最相似的已有 entry_id，无则 None
        """
        if self._embeddings.shape[0] == 0:
            return None
        vec = np.array(entry.embedding, dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        if norm < 1e-9:
            return None
        vec = vec / norm
        with self._lock:
            sims = self._embeddings.dot(vec)
        best_idx = int(np.argmax(sims))
        best_sim = float(sims[best_idx])
        if best_sim > _DEDUP_THRESHOLD:
            return self._entry_ids[best_idx]
        return None

    def _detect_conflicts(self, new_entry: MemoryEntry) -> None:
        """
        检测新 entry 与已有 entries 之间的潜在矛盾。

        条件：
        - cosine similarity 在 (0.65, 0.92) 区间（话题相关但不完全相同）
        - claim 语义对立（启发式判断）
        """
        if self._embeddings.shape[0] == 0:
            return
        vec = np.array(new_entry.embedding, dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        if norm < 1e-9:
            return
        vec = vec / norm
        with self._lock:
            sims = self._embeddings.dot(vec)
        # 排除自身（最后一个就是新加入的，但这里新 entry 刚写入索引）
        # 实际上 put 流程是先写 SQLite 再加索引，所以新 entry 已经在 _embeddings 末尾
        # 我们只检查前面的条目
        for idx, sim in enumerate(sims[:-1]):
            if _CONFLICT_LOW < float(sim) < _CONFLICT_HIGH:
                existing_id = self._entry_ids[idx]
                existing = self._entries_cache.get(existing_id)
                if existing is None:
                    continue
                if _is_semantically_opposite(new_entry.claim, existing.claim):
                    conflict = ConflictRecord(
                        conflict_id=str(uuid.uuid4()),
                        entry_id_1=existing.entry_id,
                        entry_id_2=new_entry.entry_id,
                        claim_1=existing.claim,
                        claim_2=new_entry.claim,
                        similarity=float(sim),
                        status="open",
                    )
                    self.lt.insert_conflict(conflict)
                    logger.info(
                        f"Conflict detected between {existing.entry_id} and {new_entry.entry_id}: "
                        f"sim={sim:.3f}"
                    )

    @trace_retriever(name="memory.query", tags=["m4", "memory"])
    def query_by_similarity(
        self, query: str, top_k: int = 5, min_sim: float = 0.50
    ) -> list[tuple[MemoryEntry, float]]:
        """
        按 query 语义相似度搜索记忆，带相关性门槛过滤。

        Args:
            query: 查询文本
            top_k: 返回条数上限
            min_sim: 最小相似度门槛，低于此值的记忆视为不相关

        Returns:
            (MemoryEntry, similarity) 列表（按相似度降序）
        """
        if self._embeddings.shape[0] == 0:
            return []
        q_vec = np.array(self.embedder.encode(query), dtype=np.float32)
        norm = float(np.linalg.norm(q_vec))
        if norm < 1e-9:
            return []
        q_vec = q_vec / norm
        with self._lock:
            sims = self._embeddings.dot(q_vec)
        top_indices = np.argsort(sims)[::-1][:top_k]
        results = []
        for idx in top_indices:
            sim = float(sims[int(idx)])
            if sim < min_sim:
                continue
            entry_id = self._entry_ids[int(idx)]
            entry = self._entries_cache.get(entry_id)
            if entry:
                results.append((entry, sim))
        return results

    def query_by_topic(self, topic: str) -> list[MemoryEntry]:
        """
        按 topic 精确查询。

        Args:
            topic: 话题名称

        Returns:
            该 topic 下的所有 entries
        """
        return self.lt.query_by_topic(topic)

    def get_conflicts(self, status: Optional[str] = None) -> list[ConflictRecord]:
        """
        获取矛盾记录。

        Args:
            status: 过滤状态（"open" / "resolved" / "dismissed"），None 表示全部

        Returns:
            ConflictRecord 列表
        """
        return self.lt.get_conflicts(status=status)

    def resolve_conflict(
        self,
        conflict_id: str,
        strategy: str,
        llm_policy: Optional[Any] = None,
    ) -> Optional[MemoryEntry]:
        """
        消解指定矛盾。

        支持策略：
        - "majority_vote": 同 topic 下多数 agent 支持的 claim 胜出
        - "source_weight": 按 evidence_type 权重 × confidence 加权
        - "llm_judge": 调用 VLLMPolicy 做 LLM 判断（需传入 llm_policy）

        Args:
            conflict_id: 矛盾记录 ID
            strategy: 消解策略
            llm_policy: VLLMPolicy 实例（llm_judge 策略必需）

        Returns:
            胜出的 MemoryEntry，或 None（如果无法消解）
        """
        conflicts = self.lt.get_conflicts()
        target: Optional[ConflictRecord] = None
        for c in conflicts:
            if c.conflict_id == conflict_id:
                target = c
                break
        if target is None:
            logger.warning(f"Conflict {conflict_id} not found.")
            return None

        entry_1 = self.lt.get_entry(target.entry_id_1)
        entry_2 = self.lt.get_entry(target.entry_id_2)
        if entry_1 is None or entry_2 is None:
            logger.warning("One or both entries missing, marking dismissed.")
            self.lt.update_conflict_resolution(conflict_id, "dismissed")
            return None

        winner: Optional[MemoryEntry] = None

        if strategy == "majority_vote":
            winner = self._resolve_by_majority(entry_1, entry_2)
        elif strategy == "source_weight":
            winner = self._resolve_by_source_weight(entry_1, entry_2)
        elif strategy == "llm_judge":
            if llm_policy is None:
                raise ValueError("llm_judge strategy requires llm_policy")
            winner = self._resolve_by_llm(entry_1, entry_2, llm_policy)
        else:
            raise ValueError(f"Unknown strategy: {strategy}")

        if winner is not None:
            self.lt.update_conflict_resolution(
                conflict_id, "resolved", resolution=winner.entry_id
            )
            logger.info(f"Conflict {conflict_id} resolved by {strategy}: {winner.entry_id}")
        return winner

    def _resolve_by_majority(
        self, e1: MemoryEntry, e2: MemoryEntry
    ) -> Optional[MemoryEntry]:
        """同 topic 下统计各 claim 的支持 agent 数，多数胜出。"""
        topic_entries = self.lt.query_by_topic(e1.topic)
        count_1 = sum(1 for e in topic_entries if e.claim == e1.claim)
        count_2 = sum(1 for e in topic_entries if e.claim == e2.claim)
        if count_1 >= count_2:
            return e1
        return e2

    def _resolve_by_source_weight(
        self, e1: MemoryEntry, e2: MemoryEntry
    ) -> Optional[MemoryEntry]:
        """按 evidence_type 权重 × confidence 加权评分。"""
        w1 = _EVIDENCE_WEIGHTS.get(e1.evidence_type, 0.5) * e1.confidence
        w2 = _EVIDENCE_WEIGHTS.get(e2.evidence_type, 0.5) * e2.confidence
        if w1 >= w2:
            return e1
        return e2

    def _resolve_by_llm(
        self,
        e1: MemoryEntry,
        e2: MemoryEntry,
        llm_policy: Any,
    ) -> Optional[MemoryEntry]:
        """调用 LLM 判断哪个 claim 更可信。"""
        prompt = f"""请判断以下两个陈述哪个更可信，只回答 "A" 或 "B"。

陈述 A（来源: {e1.source}, 置信度: {e1.confidence}, 证据类型: {e1.evidence_type}）:
{e1.claim}

陈述 B（来源: {e2.source}, 置信度: {e2.confidence}, 证据类型: {e2.evidence_type}）:
{e2.claim}

请只输出 A 或 B："""
        try:
            resp = llm_policy([{"role": "user", "content": prompt}])
            content = str(resp.content or "").strip().upper()
            if content.startswith("A"):
                return e1
            elif content.startswith("B"):
                return e2
        except Exception as ex:
            logger.warning(f"LLM judge failed: {ex}")
        # fallback 到 source_weight
        return self._resolve_by_source_weight(e1, e2)

    def evict(self, max_entries: int = 10000) -> int:
        """
        淘汰低分条目，使总数不超过 max_entries。

        淘汰逻辑：
        - 先计算每条 entry 的综合评分
        - 删除评分最低的 entries，直到总数达标
        - 有 open conflict 的 entry 受保护（conflict_bonus 使其排在最后）

        Args:
            max_entries: 最大保留条目数

        Returns:
            实际删除的条目数
        """
        current_count = self.lt.count_entries(session_id=self.session_id or None)
        if current_count <= max_entries:
            return 0

        to_remove = current_count - max_entries
        low_score_entries = self.lt.get_lowest_score_entries(limit=to_remove, session_id=self.session_id or None)
        removed = 0
        for entry_id, score in low_score_entries:
            # 再次确认：如果仍有 open conflict，跳过
            conflicts = self.lt.get_conflicts(status="open")
            has_conflict = any(
                c.entry_id_1 == entry_id or c.entry_id_2 == entry_id
                for c in conflicts
            )
            if has_conflict:
                logger.info(f"Protected entry {entry_id} from eviction (open conflict).")
                continue
            if self.lt.delete_entry(entry_id):
                self._remove_from_index(entry_id)
                removed += 1
                logger.info(f"Evicted entry {entry_id} (score={score:.4f}).")
        return removed

    def get_context_for_query(self, query: str, max_tokens: int = 4000) -> str:
        """
        为 Agent 组装与 query 相关的记忆上下文文本。

        策略：
        1. 语义相似度搜索 top-10（门槛 min_sim=0.55，避免无关记忆污染）
        2. 加入时间衰减：太旧的记忆降低权重
        3. 按综合得分排序，逐条拼接直到接近 max_tokens
        4. 返回格式化的文本块

        Args:
            query: 当前查询
            max_tokens: token 预算上限

        Returns:
            组装好的上下文文本（空字符串表示无相关记忆）
        """
        import time

        entries_with_sim = self.query_by_similarity(query, top_k=10, min_sim=0.55)
        if not entries_with_sim:
            return ""

        now = time.time()

        def _score(entry: MemoryEntry, sim: float) -> float:
            """综合得分 = 相似度 × confidence × 时间衰减。"""
            days_old = max((now - entry.timestamp) / 86400.0, 0.0)
            recency = np.exp(-days_old / 30.0)  # 30 天半衰期
            return sim * entry.confidence * recency

        # 按综合得分降序排序
        entries_with_sim.sort(key=lambda x: _score(x[0], x[1]), reverse=True)

        max_chars = int(max_tokens * 3.5)  # token → 字符经验换算
        parts: list[str] = []
        current_chars = 0

        header = "## 相关背景知识\n"
        current_chars += len(header)
        parts.append(header)

        for entry, sim in entries_with_sim:
            block = (
                f"- [{entry.topic}] {entry.claim}\n"
                f"  来源: {entry.source} | 置信度: {entry.confidence:.2f} | "
                f"证据类型: {entry.evidence_type} | 相关度: {sim:.2f}\n"
            )
            if current_chars + len(block) > max_chars:
                break
            parts.append(block)
            current_chars += len(block)

        return "".join(parts)

    def __len__(self) -> int:
        return self.lt.count_entries(session_id=self.session_id or None)

    def list_sessions(self) -> list[dict[str, Any]]:
        """列出数据库中所有 session 及其统计信息。"""
        return self.lt.get_sessions()
