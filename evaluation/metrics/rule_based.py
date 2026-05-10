#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
evaluation/metrics/rule_based.py
================================================================================
基于规则/统计的轻量级评测指标。

适用于批量运行、CI/CD、消融实验等需要快速、免费、可复现评分的场景。
================================================================================
"""

from __future__ import annotations

import math
import re
from typing import Any


class RuleBasedMetrics:
    """研究报告质量评测指标集合（规则版）。"""

    # -----------------------------------------------------------------------
    # 1. 事实准确性 (Factual Accuracy) — 字符串匹配版（快速但粗糙）
    # -----------------------------------------------------------------------
    @staticmethod
    def fact_accuracy(report: str, ground_truth: dict[str, Any] | None = None) -> float:
        """
        计算报告中的关键事实与 ground_truth 的匹配程度。

        当前实现采用简单启发式：统计报告中包含的 ground_truth 关键短语比例。
        若无 ground_truth，则返回 0.0（需外部 Judge LLM 补充评估）。
        """
        if not ground_truth:
            return 0.0

        report_lower = report.lower()
        matched = 0
        for key_fact in ground_truth.keys():
            if key_fact.lower() in report_lower:
                matched += 1

        return matched / len(ground_truth) if ground_truth else 0.0

    # -----------------------------------------------------------------------
    # 1b. 语义事实准确性 (Semantic Factual Accuracy) — 面试强化版
    # -----------------------------------------------------------------------
    @staticmethod
    def semantic_fact_accuracy(
        report: str,
        ground_truth: dict[str, Any] | None = None,
        threshold: float = 0.65,
    ) -> float:
        """
        基于 embedding 语义相似度的事实准确性验证。

        改进点（相比字符串匹配）：
        1. 把 ground_truth 的 key + description 编码为语义向量
        2. 把报告拆分成句子 chunk，分别编码
        3. 计算每个 ground_truth 条目与报告中最相似 chunk 的 cosine similarity
        4. 超过阈值（默认 0.65）才判定为"事实被覆盖"

        这样能避免"GPT-4o 是 Google 发布的"这种关键词命中但语义错误的误报。

        Args:
            report: 研究报告全文
            ground_truth: 期望事实字典 {key: description}
            threshold: 语义相似度阈值，0-1

        Returns:
            0.0 ~ 1.0 的覆盖率
        """
        if not ground_truth:
            return 0.0

        import numpy as np
        from src.memory.embedder import Embedder

        embedder = Embedder()

        # 把报告拆成句子 chunk（避免长报告淹没短事实）
        chunks = [s.strip() for s in re.split(r"[。！？\n]", report) if len(s.strip()) > 10]
        if not chunks:
            return 0.0

        # 批量编码 chunk（Sentencetransformer 支持批量）
        try:
            chunk_embs = np.array(embedder._load_model().encode(chunks, normalize_embeddings=True))
        except Exception:
            # fallback：逐条编码
            chunk_embs = np.array([embedder.encode(c) for c in chunks])

        matched = 0
        for key_fact, expected_desc in ground_truth.items():
            # 组合 key + description 作为语义查询
            fact_text = f"{key_fact}：{expected_desc}"
            fact_emb = np.array(embedder.encode(fact_text))

            # 计算与所有 chunk 的 cosine similarity
            sims = chunk_embs.dot(fact_emb)
            max_sim = float(np.max(sims)) if sims.size > 0 else 0.0

            if max_sim > threshold:
                matched += 1

        return matched / len(ground_truth)

    # -----------------------------------------------------------------------
    # 2. 幻觉率 (Hallucination Rate)
    # -----------------------------------------------------------------------
    @staticmethod
    def hallucination_rate(report: str) -> float:
        """
        估算报告中可能存在的幻觉内容比例。

        当前启发式策略：
        - 检测无引用的数值声明（数字+单位）。
        - 检测缺乏来源的绝对化表述（"绝对"、"毫无疑问"等）。
        - 检测模型常见的幻觉模式（"据我所知"、"研究表明"但无具体引用）。

        Returns:
            0.0 ~ 1.0，越高表示幻觉风险越大。
        """
        if not report:
            return 1.0

        sentences = re.split(r"[。！？\n]", report)
        sentences = [s.strip() for s in sentences if s.strip()]
        if not sentences:
            return 1.0

        hallucination_indicators = [
            r"\d+[\d,]*\.?\d*\s*(%|倍|个|人|元|美元|亿|万)",  # 带单位的孤立数字
            r"毫无疑问|绝对|必然|一定|众所周知",
            r"据我所知|据了解|研究显示[^【\[（(]",  # 模糊引用开头
        ]

        suspicious_count = 0
        for sentence in sentences:
            # 如果句子中无引用标记，检查是否包含幻觉特征
            if not re.search(r"[\[【（(].*?[\]）)]", sentence):
                for pattern in hallucination_indicators:
                    if re.search(pattern, sentence):
                        suspicious_count += 1
                        break

        return min(1.0, suspicious_count / max(len(sentences), 1))

    # -----------------------------------------------------------------------
    # 3. 引用覆盖率 (Citation Coverage)
    # -----------------------------------------------------------------------
    @staticmethod
    def citation_coverage(report: str) -> float:
        """
        计算报告中包含引用来源的段落比例。

        引用标记形式：
        - [N] 或 [来源: ...]
        - 【来源: ...】
        - (来源: ...)
        """
        if not report:
            return 0.0

        paragraphs = [p.strip() for p in report.split("\n") if p.strip()]
        if not paragraphs:
            return 0.0

        citation_patterns = [
            r"\[\d+\]",
            r"\[来源[：:]",
            r"【来源[：:]",
            r"\(来源[：:]",
            r"https?://",
            r"arxiv\.org",
        ]

        cited_paragraphs = 0
        for para in paragraphs:
            for pattern in citation_patterns:
                if re.search(pattern, para):
                    cited_paragraphs += 1
                    break

        return cited_paragraphs / len(paragraphs)

    # -----------------------------------------------------------------------
    # 4. 逻辑一致性 (Logical Consistency)
    # -----------------------------------------------------------------------
    @staticmethod
    def logical_consistency(report: str) -> float:
        """
        估算报告的逻辑一致性分数。

        当前启发式策略：
        - 检测明显的自相矛盾关键词对（"是" vs "不是" 在同一上下文）。
        - 检测逻辑连接词使用是否合理（"因此"、"然而"前是否有前提）。
        """
        if not report:
            return 0.0

        # 简单检测矛盾对：句子中同时出现 A 和 非A（同一句话）
        contradiction_pairs = [
            ("是", "不是"),
            ("可以", "不可以"),
            ("会", "不会"),
            ("支持", "反对"),
            ("增加", "减少"),
        ]

        sentences = re.split(r"[。！？\n]", report)
        sentences = [s.strip() for s in sentences if s.strip()]
        if not sentences:
            return 0.0

        contradiction_count = 0
        for sentence in sentences:
            for a, b in contradiction_pairs:
                if a in sentence and b in sentence:
                    # 更严格的检查：确保它们之间没有否定词分隔
                    contradiction_count += 1
                    break

        # 同时奖励使用逻辑连接词
        connectives = ["因此", "所以", "然而", "但是", "首先", "其次", "综上所述"]
        connective_count = sum(1 for c in connectives if c in report)
        connective_bonus = min(0.1, connective_count * 0.01)

        base_score = 1.0 - (contradiction_count / max(len(sentences), 1))
        return min(1.0, max(0.0, base_score + connective_bonus))

    # -----------------------------------------------------------------------
    # 5. 完备性 (Comprehensiveness)
    # -----------------------------------------------------------------------
    @staticmethod
    def comprehensiveness(report: str, expected_topics: list[str] | None = None) -> float:
        """
        计算报告对期望主题的覆盖程度。
        """
        if not expected_topics:
            return 0.0

        report_lower = report.lower()
        covered = 0
        for topic in expected_topics:
            if topic.lower() in report_lower:
                covered += 1

        return covered / len(expected_topics) if expected_topics else 0.0

    # -----------------------------------------------------------------------
    # 6. 综合得分 (Composite Score)
    # -----------------------------------------------------------------------
    @staticmethod
    def composite_score(
        metrics: dict[str, float],
        weights: dict[str, float] | None = None,
    ) -> float:
        """
        基于多维度指标和权重计算加权综合得分。

        默认权重与 Red Agent 的五维度对齐：
        - factual_accuracy: 0.25
        - logical_consistency: 0.20
        - citation_coverage: 0.20
        - bias (1 - hallucination_rate 作为代理): 0.20
        - comprehensiveness: 0.15
        """
        default_weights = {
            "factual_accuracy": 0.25,
            "logical_consistency": 0.20,
            "citation_coverage": 0.20,
            "bias": 0.20,
            "comprehensiveness": 0.15,
        }

        w = weights if weights is not None else default_weights
        total_score = 0.0
        total_weight = 0.0

        for key, weight in w.items():
            value = metrics.get(key, 0.0)
            total_score += value * weight
            total_weight += weight

        return total_score / total_weight if total_weight > 0 else 0.0

    # -----------------------------------------------------------------------
    # 7. 效率指标 (Efficiency)
    # -----------------------------------------------------------------------
    @staticmethod
    def efficiency_score(
        num_turns: int,
        target_turns: float = 8.0,
        slope: float = 0.5,
        max_bonus: float = 0.5,
    ) -> float:
        """
        基于 sigmoid 的效率奖励分数。

        公式：max_bonus * sigmoid(slope * (target_turns - num_turns))
        """
        sigmoid = 1.0 / (1.0 + math.exp(-slope * (target_turns - num_turns)))
        return max_bonus * sigmoid
