#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
evaluation/metrics/composite.py
================================================================================
综合得分计算工具。

将规则指标和 Judge 指标聚合为统一的综合得分。
================================================================================
"""

from __future__ import annotations

from typing import Any


def compute_composite_score(
    rule_metrics: dict[str, float] | None = None,
    judge_result: dict[str, Any] | None = None,
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """
    综合评分：合并规则指标和 Judge 指标。

    Args:
        rule_metrics: 规则指标字典（如 factual_accuracy, citation_coverage 等）。
        judge_result: Judge 评分结果（如 LLMJudge.score_single 的返回）。
        weights: 自定义权重。默认规则指标 60%，Judge 指标 40%。

    Returns:
        包含综合得分、各维度明细的字典。
    """
    default_weights = {
        "rule": 0.6,
        "judge": 0.4,
    }
    w = weights if weights is not None else default_weights

    rule_score = 0.0
    if rule_metrics:
        # 规则指标已经是 0-1 的分数，直接加权平均
        rule_vals = [v for v in rule_metrics.values() if isinstance(v, (int, float))]
        rule_score = sum(rule_vals) / len(rule_vals) if rule_vals else 0.0

    judge_score = 0.0
    judge_dims = {}
    if judge_result:
        # Judge 结果是 0-10 分，归一化到 0-1
        dims = judge_result.get("dimensions", {})
        judge_dims = {
            k: v["score"] / 10.0
            for k, v in dims.items()
            if isinstance(v, dict) and "score" in v
        }
        judge_score = sum(judge_dims.values()) / len(judge_dims) if judge_dims else 0.0

    composite = w.get("rule", 0.6) * rule_score + w.get("judge", 0.4) * judge_score

    return {
        "composite_score": round(composite, 4),
        "rule_score": round(rule_score, 4),
        "judge_score": round(judge_score, 4),
        "rule_metrics": rule_metrics or {},
        "judge_dimensions": judge_dims,
    }
