"""
Context Compressor 模块：统一压缩入口

设计决策：
1. 三级渐进式压缩：L1 相关性过滤 → L2 关键句提取 → L3 层级摘要
2. 自动触发：根据当前 token 使用量占 budget 的比例自动选择压缩级别
   - >60% budget: L1
   - >80% budget: L1+L2
   - >95% budget: L1+L2+L3
3. Budget 管理：available = budget - system_prompt_tokens - output_reserve
4. 量化评测接口：compression_ratio + information_retention（entity/keyword 保留率）
5. 与 SlidingWindowCompressor 配合：先尝试语义压缩，最后兜底用滑动窗口截断
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from src.memory.embedder import Embedder
from src.models.vllm_policy import VLLMPolicy
from src.compressor.extractive import ExtractiveCompressor
from src.compressor.sliding_window import SlidingWindowCompressor
from src.compressor.summarizer import LLMSummarizer
from src.utils.tracing import trace_chain

logger = logging.getLogger(__name__)

# 压缩级别触发阈值
_L1_THRESHOLD = 0.60
_L2_THRESHOLD = 0.80
_L3_THRESHOLD = 0.95

# token 估算参数
_CHARS_PER_TOKEN = 3.5
_OUTPUT_RESERVE = 2048  # 为 LLM 输出预留的 token 数


class ContextCompressor:
    """
    上下文统一压缩器。

    对外暴露 compress() 接口，自动判断压缩级别并执行渐进式压缩。
    同时提供量化评测接口，用于 ablation 实验。
    """

    def __init__(
        self,
        llm_policy: VLLMPolicy,
        embedder: Optional[Embedder] = None,
        budget: int = 16000,
        output_reserve: int = _OUTPUT_RESERVE,
    ) -> None:
        """
        初始化上下文压缩器。

        Args:
            llm_policy: VLLMPolicy 实例（L3 摘要和 llm_judge 需要）
            embedder: 向量化器
            budget: 总上下文 token 预算
            output_reserve: 为模型输出预留的 token 数
        """
        self.llm_policy = llm_policy
        self.embedder = embedder or Embedder()
        self.budget = budget
        self.output_reserve = output_reserve
        self.available_budget = budget - output_reserve

        # 子压缩器
        self.sliding = SlidingWindowCompressor(max_tokens=self.available_budget)
        self.extractive = ExtractiveCompressor(embedder=self.embedder)
        self.summarizer = LLMSummarizer(llm_policy=llm_policy)

        # 统计累积
        self._stats_history: list[dict[str, Any]] = []

    def calculate_tokens(self, texts: list[str]) -> int:
        """
        估算文本列表的总 token 数。

        Args:
            texts: 文本列表

        Returns:
            估算 token 数
        """
        total_chars = sum(len(t) for t in texts)
        return int(total_chars / _CHARS_PER_TOKEN)

    @trace_chain(name="compressor.compress", tags=["m3", "compressor"])
    def compress(
        self,
        texts: list[str],
        query: str = "",
        level: Optional[int] = None,
        system_prompt_tokens: int = 0,
    ) -> list[str]:
        """
        对文本列表执行压缩。

        Args:
            texts: 原始文本列表（每篇文档/每条消息一个元素）
            query: 当前查询（用于 L1/L2 相关性加权）
            level: 强制指定压缩级别（1/2/3），None 时自动判断
            system_prompt_tokens: system prompt 占用的 token 数

        Returns:
            压缩后的文本列表
        """
        if not texts:
            return []

        # 计算实际可用 budget
        actual_budget = self.available_budget - system_prompt_tokens
        if actual_budget <= 0:
            logger.warning("Actual budget <= 0 after system prompt, forcing max compression.")
            actual_budget = self.available_budget // 2

        current_tokens = self.calculate_tokens(texts)
        usage_ratio = current_tokens / max(actual_budget, 1)

        # 确定压缩级别
        if level is None:
            if usage_ratio > _L3_THRESHOLD:
                level = 3
            elif usage_ratio > _L2_THRESHOLD:
                level = 2
            elif usage_ratio > _L1_THRESHOLD:
                level = 1
            else:
                # 无需压缩
                self._record_stats(texts, texts, 0, current_tokens)
                return texts

        logger.info(
            f"[ContextCompressor] Triggered L{level}: "
            f"{current_tokens} tokens / {actual_budget} budget ({usage_ratio:.1%})"
        )

        compressed = list(texts)

        # L1: 相关性过滤
        if level >= 1:
            compressed = self._l1_filter(compressed, query, actual_budget)

        # L2: 关键句提取
        if level >= 2 and compressed:
            compressed = self._l2_extract(compressed, query, actual_budget)

        # L3: 层级摘要
        if level >= 3 and compressed:
            compressed = self._l3_summarize(compressed, query, actual_budget)

        # 如果压缩后仍然超限，兜底滑动窗口截断
        final_tokens = self.calculate_tokens(compressed)
        if final_tokens > actual_budget:
            logger.warning(
                f"[ContextCompressor] Still over budget after L{level}: "
                f"{final_tokens} > {actual_budget}. Falling back to sliding window."
            )
            # 将文本列表合并为消息格式进行截断
            messages = [{"role": "user", "content": t} for t in compressed]
            truncated_msgs = self.sliding.compress(messages)
            compressed = [m["content"] for m in truncated_msgs]

        self._record_stats(texts, compressed, level, self.calculate_tokens(compressed))
        return compressed

    def _l1_filter(
        self,
        texts: list[str],
        query: str,
        budget: int,
    ) -> list[str]:
        """
        L1 相关性过滤：embedding cosine similarity 评分，自适应阈值。

        策略：
        - 计算每段文本与 query 的相似度
        - 初始阈值 0.25，若过滤后仍然超 budget，逐步降低到 0.15
        - 保留相似度 >= 阈值的文本
        """
        if not query or not query.strip():
            # 无 query 时不做过滤
            return texts

        query_emb = self.embedder.encode(query)
        query_vec = self._to_norm_vec(query_emb)

        scored: list[tuple[str, float]] = []
        for text in texts:
            text_emb = self.embedder.encode(text[:1000])  # 只取前 1000 字符加速
            text_vec = self._to_norm_vec(text_emb)
            sim = float(query_vec.dot(text_vec)) if text_vec is not None else 0.0
            scored.append((text, sim))

        # 自适应阈值：从 0.25 开始，若不够严格则递减
        best_result: list[str] = []
        for threshold in [0.25, 0.20, 0.15]:
            filtered = [t for t, s in scored if s >= threshold]
            tokens = self.calculate_tokens(filtered)
            if tokens <= budget * 0.8:
                best_result = filtered
                break
            if threshold == 0.15:
                best_result = filtered

        # 保底策略：若过滤后为空但原始有内容，至少保留相似度最高的 1 篇
        if not best_result and texts:
            best_text = max(scored, key=lambda x: x[1])[0]
            best_result = [best_text]
            logger.info(f"[L1] Fallback: kept top-1 similar doc.")

        logger.info(
            f"[L1] Filtered {len(texts)} -> {len(best_result)} docs, tokens={self.calculate_tokens(best_result)}"
        )
        return best_result

    def _l2_extract(
        self,
        texts: list[str],
        query: str,
        budget: int,
    ) -> list[str]:
        """
        L2 关键句提取：TextRank + query-biased，动态保留比例。

        策略：
        - 根据剩余 budget 计算每篇文档的目标保留比例
        - 预算越紧张，top_ratio 越低（最低 0.15）
        """
        current_tokens = self.calculate_tokens(texts)
        # 目标比例：线性映射，预算用满 80% 时保留 30%，用满 100% 时保留 15%
        target_ratio = max(0.15, min(0.40, 0.50 - (current_tokens / max(budget, 1)) * 0.35))

        compressed = []
        for text in texts:
            comp = self.extractive.compress(text, query, target_ratio=target_ratio)
            compressed.append(comp)

        after_tokens = self.calculate_tokens(compressed)
        logger.info(
            f"[L2] Extractive compression: {current_tokens} -> {after_tokens} tokens, "
            f"ratio={target_ratio:.2f}"
        )
        return compressed

    def _l3_summarize(
        self,
        texts: list[str],
        query: str,
        budget: int,
    ) -> list[str]:
        """
        L3 层级摘要：逐文档摘要 → 聚合摘要。

        策略：
        - 先对每篇文档做单文档摘要（控制长度）
        - 再将所有摘要聚合为一段综述
        - 最终返回单元素列表（聚合结果）
        """
        current_tokens = self.calculate_tokens(texts)
        # 单文档摘要目标长度
        per_doc_max = max(200, budget // max(len(texts), 1))
        summaries = []
        for text in texts:
            summary = self.summarizer.summarize_document(
                text, query, max_length=per_doc_max
            )
            summaries.append(summary)

        # 聚合摘要
        aggregate_max = max(400, budget // 2)
        aggregate = self.summarizer.summarize_documents(
            summaries, query, max_length=aggregate_max
        )

        after_tokens = self.calculate_tokens([aggregate])
        logger.info(
            f"[L3] LLM summarization: {current_tokens} -> {after_tokens} tokens"
        )
        return [aggregate]

    @staticmethod
    def _to_norm_vec(embedding: list[float]) -> Optional[Any]:
        """将 embedding 转为归一化 numpy 向量。"""
        import numpy as np
        vec = np.array(embedding, dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        if norm < 1e-9:
            return None
        return vec / norm

    def _record_stats(
        self,
        original: list[str],
        compressed: list[str],
        level: int,
        after_tokens: int,
    ) -> None:
        """记录本次压缩的统计信息。"""
        orig_tokens = self.calculate_tokens(original)
        ratio = after_tokens / max(orig_tokens, 1)
        retention = self._estimate_retention(original, compressed)
        stats = {
            "level": level,
            "original_tokens": orig_tokens,
            "compressed_tokens": after_tokens,
            "compression_ratio": round(ratio, 3),
            "information_retention": round(retention, 3),
        }
        self._stats_history.append(stats)

    def _estimate_retention(
        self,
        original: list[str],
        compressed: list[str],
    ) -> float:
        """
        估算信息保留率。

        简单启发式：
        - 提取 original 中的数字实体和英文专有名词
        - 检查有多少出现在 compressed 中
        - 保留率 = 出现的实体数 / 总实体数
        """
        orig_text = " ".join(original)
        comp_text = " ".join(compressed)

        # 数字实体（含百分比、日期）
        numbers = set(re.findall(r"\d+[\d,]*\.?\d*\s*%?|\d{4}-\d{2}-\d{2}", orig_text))
        # 英文专有名词（大写单词序列）
        names = set(re.findall(r"[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,3}", orig_text))
        entities = numbers | names
        if not entities:
            return 1.0

        preserved = sum(1 for e in entities if e in comp_text)
        return preserved / len(entities)

    def get_stats(self) -> dict[str, Any]:
        """
        返回累计压缩统计。

        Returns:
            {
                "total_compresses": 压缩次数,
                "avg_compression_ratio": 平均压缩比,
                "avg_retention": 平均信息保留率,
                "level_distribution": 各级别使用次数,
                "history": 每次压缩的详细记录,
            }
        """
        if not self._stats_history:
            return {
                "total_compresses": 0,
                "avg_compression_ratio": 1.0,
                "avg_retention": 1.0,
                "level_distribution": {0: 0, 1: 0, 2: 0, 3: 0},
                "history": [],
            }

        total = len(self._stats_history)
        avg_ratio = sum(s["compression_ratio"] for s in self._stats_history) / total
        avg_retention = sum(s["information_retention"] for s in self._stats_history) / total
        level_dist: dict[int, int] = {0: 0, 1: 0, 2: 0, 3: 0}
        for s in self._stats_history:
            level_dist[s["level"]] = level_dist.get(s["level"], 0) + 1

        return {
            "total_compresses": total,
            "avg_compression_ratio": round(avg_ratio, 3),
            "avg_retention": round(avg_retention, 3),
            "level_distribution": level_dist,
            "history": list(self._stats_history),
        }
