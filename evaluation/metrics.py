#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
evaluation/metrics.py
================================================================================
评测指标模块：定义 DeepResearch Agent 输出报告的多维度质量评估方法。

当前实现为基于规则/统计的轻量级指标。生产环境中部分指标（如幻觉检测）
可替换为基于 Judge LLM 的评估，以提高准确度。
================================================================================
"""

from __future__ import annotations

import math
import re
from typing import Any


class ResearchMetrics:
    """研究报告质量评测指标集合。"""

    # -----------------------------------------------------------------------
    # 1. 事实准确性 (Factual Accuracy)
    # -----------------------------------------------------------------------
    @staticmethod
    def fact_accuracy(report: str, ground_truth: dict[str, Any] | None = None) -> float:
        """
        计算报告中的关键事实与 ground_truth 的匹配程度。

        当前实现采用简单启发式：统计报告中包含的 ground_truth 关键短语比例。
        若无 ground_truth，则返回 0.0（需外部 Judge LLM 补充评估）。

        Args:
            report: 生成的研究报告文本。
            ground_truth: 期望包含的关键事实字典，键为事实短语。

        Returns:
            0.0 ~ 1.0 之间的准确率分数。
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

        Args:
            report: 生成的研究报告文本。

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

        Args:
            report: 生成的研究报告文本。

        Returns:
            0.0 ~ 1.0，越高表示引用越充分。
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

        Args:
            report: 生成的研究报告文本。

        Returns:
            0.0 ~ 1.0，越高表示逻辑越一致。
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

        Args:
            report: 生成的研究报告文本。
            expected_topics: 期望覆盖的子主题列表。

        Returns:
            0.0 ~ 1.0，越高表示覆盖越全面。
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
    def composite_score(metrics: dict[str, float], weights: dict[str, float] | None = None) -> float:
        """
        基于多维度指标和权重计算加权综合得分。

        默认权重与 Red Agent 的五维度对齐：
        - factual_accuracy: 0.25
        - logical_consistency: 0.20
        - citation_coverage: 0.20
        - bias (1 - hallucination_rate 作为代理): 0.20
        - comprehensiveness: 0.15

        Args:
            metrics: 指标名称 -> 指标值的字典。
            weights: 指标名称 -> 权重的字典，None 时使用默认权重。

        Returns:
            0.0 ~ 1.0 的综合得分。
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
    def efficiency_score(num_turns: int, target_turns: float = 8.0, slope: float = 0.5, max_bonus: float = 0.5) -> float:
        """
        基于 sigmoid 的效率奖励分数。

        公式：max_bonus * sigmoid(slope * (target_turns - num_turns))

        Args:
            num_turns: 实际使用的交互轮次。
            target_turns: 期望的理想轮次数。
            slope: sigmoid 斜率。
            max_bonus: 最大奖励上限。

        Returns:
            0.0 ~ max_bonus 的效率分数。
        """
        sigmoid = 1.0 / (1.0 + math.exp(-slope * (target_turns - num_turns)))
        return max_bonus * sigmoid

    # -----------------------------------------------------------------------
    # 8. MiMo Judge 评分 (LLM-as-Judge)
    # -----------------------------------------------------------------------
    @staticmethod
    def judge_score(report: str, query: str, ground_truth: dict[str, Any] | None = None) -> dict[str, Any]:
        """使用 MiMo 2.5 Pro 作为 Judge 对报告进行多维度评分。

        当规则指标不足以精确评估时，调用此方法获取 LLM 的定性判断。
        返回结构包含事实准确性、逻辑一致性、引用质量、整体置信度。

        Args:
            report: 生成的研究报告文本。
            query: 原始研究问题。
            ground_truth: 期望包含的关键事实（可选）。

        Returns:
            包含各维度得分和理由的字典。
        """
        import json
        import re

        from src.models.model_router import ModelRouter

        gt_section = ""
        if ground_truth:
            gt_lines = "\n".join(f"- {k}: {v}" for k, v in ground_truth.items())
            gt_section = f"期望包含的关键事实：\n{gt_lines}\n"

        prompt = f"""你是一位严谨的研究报告评审专家。请对以下研究报告进行评分。

研究问题：{query}

{gt_section}
--- 研究报告 ---
{report[:4000]}

请从以下维度评分（每项 0-10 分，10 分为最高）：
1. factual_accuracy: 事实准确性（数字、日期、人名、机构名是否正确）
2. logical_consistency: 逻辑一致性（论证是否自洽，有无矛盾）
3. citation_quality: 引用质量（来源是否可靠，引用是否充分）
4. comprehensiveness: 覆盖面（是否全面回答了研究问题的各个子维度）
5. overall: 整体质量

请输出严格 JSON 格式：
{{
  "factual_accuracy": {{"score": 分数, "reason": "简短理由"}},
  "logical_consistency": {{"score": 分数, "reason": "简短理由"}},
  "citation_quality": {{"score": 分数, "reason": "简短理由"}},
  "comprehensiveness": {{"score": 分数, "reason": "简短理由"}},
  "overall": {{"score": 分数, "reason": "简短理由"}}
}}"""

        try:
            policy = ModelRouter.create_backend("mimo")
            messages = [
                {"role": "system", "content": "你是研究报告评审专家。必须输出合法 JSON，不要输出任何其他内容。"},
                {"role": "user", "content": prompt},
            ]
            resp = policy(messages)
            content = resp.get("content", "")

            m = re.search(r"\{.*\}", content, re.DOTALL)
            if m:
                result = json.loads(m.group())
                # 计算平均分
                scores = [v["score"] for v in result.values() if isinstance(v, dict) and "score" in v]
                result["average"] = sum(scores) / len(scores) if scores else 0.0
                result["judge_backend"] = "mimo"
                return result
        except Exception as e:
            return {"error": str(e), "judge_backend": "mimo"}

        return {"error": "无法解析 MiMo Judge 输出", "judge_backend": "mimo"}


# =============================================================================
# 简单自测
# =============================================================================
if __name__ == "__main__":
    sample_report = """
    研究表明，人工智能在医疗诊断中的应用正在快速增长[1]。
    根据 Nature Medicine 2024 年的综述，AI 辅助诊断的准确率已达到 95%[2]。
    然而，这一技术也面临数据隐私和伦理挑战[3]。
    综上所述，AI 医疗的发展前景广阔，但需要审慎监管。
    """

    print("citation_coverage:", ResearchMetrics.citation_coverage(sample_report))
    print("hallucination_rate:", ResearchMetrics.hallucination_rate(sample_report))
    print("logical_consistency:", ResearchMetrics.logical_consistency(sample_report))
    print("comprehensiveness:", ResearchMetrics.comprehensiveness(sample_report, expected_topics=["医疗", "伦理"]))
    print("efficiency_score:", ResearchMetrics.efficiency_score(num_turns=6))
