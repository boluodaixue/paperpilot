#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
evaluation/metrics/judge_based.py
================================================================================
基于 LLM-as-Judge 的深度评测指标。

适用于需要主观专家判断的场景（抽查、对比分析、最终质量验证）。
底层调用 evaluation.judge.LLMJudge，不进入 Research AgentGraph。
================================================================================
"""

from __future__ import annotations

from typing import Any


class JudgeBasedMetrics:
    """研究报告质量评测指标集合（LLM Judge 版）。"""

    @staticmethod
    def judge_score(
        report: str,
        query: str,
        ground_truth: dict[str, Any] | None = None,
        backend: str = "deepseek",
    ) -> dict[str, Any]:
        """
        使用指定 Judge 后端对报告进行多维度评分。

        当规则指标不足以精确评估时，调用此方法获取 LLM 的定性判断。
        返回结构包含事实准确性、逻辑一致性、引用质量、整体置信度。

        Args:
            report: 生成的研究报告文本。
            query: 原始研究问题。
            ground_truth: 期望包含的关键事实（可选）。
            backend: Judge 后端名称。

        Returns:
            包含各维度得分和理由的字典。
        """
        from evaluation.judge import LLMJudge

        judge = LLMJudge(backend=backend)
        return judge.score_single(report, query, ground_truth)
