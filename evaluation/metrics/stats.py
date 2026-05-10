#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
evaluation/metrics/stats.py
================================================================================
统计显著性检验工具：bootstrap 置信区间、效应量、配对 t 检验。

适用于小样本消融实验和 head-to-head benchmark 的统计严谨性验证。
================================================================================
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np


def bootstrap_ci_paired(
    diffs: list[float],
    n_bootstrap: int = 10000,
    confidence: float = 0.95,
) -> dict[str, Any]:
    """
    配对差异的 bootstrap 置信区间。

    Args:
        diffs: 配对差异列表（如 full_score - no_adv_score）
        n_bootstrap: bootstrap 采样次数
        confidence: 置信水平

    Returns:
        dict with mean_diff, ci_lower, ci_upper, p_value, significant
    """
    if not diffs:
        return {"mean_diff": 0.0, "ci_lower": 0.0, "ci_upper": 0.0, "p_value": 1.0, "significant": False}

    diffs_arr = np.array(diffs)
    mean_diff = float(np.mean(diffs_arr))

    boot_means = []
    for _ in range(n_bootstrap):
        sample = np.random.choice(diffs_arr, size=len(diffs_arr), replace=True)
        boot_means.append(float(np.mean(sample)))

    boot_means = np.array(boot_means)
    alpha = 1 - confidence
    ci_lower = float(np.percentile(boot_means, alpha / 2 * 100))
    ci_upper = float(np.percentile(boot_means, (1 - alpha / 2) * 100))

    # p-value: H0 mean_diff <= 0
    p_value = float(np.mean(boot_means <= 0))

    return {
        "mean_diff": round(mean_diff, 4),
        "ci_lower": round(ci_lower, 4),
        "ci_upper": round(ci_upper, 4),
        "p_value": round(p_value, 4),
        "significant": ci_lower > 0,  # 95% CI 完全在 0 右侧
        "n": len(diffs),
    }


def bootstrap_ci_two_sample(
    scores_a: list[float],
    scores_b: list[float],
    n_bootstrap: int = 10000,
    confidence: float = 0.95,
) -> dict[str, Any]:
    """
    两组独立样本的 bootstrap 置信区间（非配对）。

    Args:
        scores_a: 系统 A 的分数列表
        scores_b: 系统 B 的分数列表
    """
    if not scores_a or not scores_b:
        return {"mean_diff": 0.0, "ci_lower": 0.0, "ci_upper": 0.0, "p_value": 1.0, "significant": False}

    a_arr = np.array(scores_a)
    b_arr = np.array(scores_b)
    mean_diff = float(np.mean(a_arr) - np.mean(b_arr))

    boot_diffs = []
    for _ in range(n_bootstrap):
        a_sample = np.random.choice(a_arr, size=len(a_arr), replace=True)
        b_sample = np.random.choice(b_arr, size=len(b_arr), replace=True)
        boot_diffs.append(float(np.mean(a_sample) - np.mean(b_sample)))

    boot_diffs = np.array(boot_diffs)
    alpha = 1 - confidence
    ci_lower = float(np.percentile(boot_diffs, alpha / 2 * 100))
    ci_upper = float(np.percentile(boot_diffs, (1 - alpha / 2) * 100))
    p_value = float(np.mean(boot_diffs <= 0))

    return {
        "mean_diff": round(mean_diff, 4),
        "ci_lower": round(ci_lower, 4),
        "ci_upper": round(ci_upper, 4),
        "p_value": round(p_value, 4),
        "significant": ci_lower > 0,
        "n_a": len(scores_a),
        "n_b": len(scores_b),
    }


def cohens_d(scores_a: list[float], scores_b: list[float]) -> float:
    """计算 Cohen's d 效应量。"""
    a_arr = np.array(scores_a)
    b_arr = np.array(scores_b)
    pooled_std = math.sqrt((np.var(a_arr, ddof=1) + np.var(b_arr, ddof=1)) / 2)
    if pooled_std < 1e-9:
        return 0.0
    return float((np.mean(a_arr) - np.mean(b_arr)) / pooled_std)


def paired_t_test(scores_a: list[float], scores_b: list[float]) -> dict[str, Any]:
    """配对 t 检验（假设正态分布）。作为 bootstrap 的补充。"""
    try:
        from scipy import stats
        diffs = np.array(scores_a) - np.array(scores_b)
        t_stat, p_value = stats.ttest_1samp(diffs, popmean=0)
        return {
            "t_statistic": round(float(t_stat), 4),
            "p_value": round(float(p_value), 4),
            "mean_diff": round(float(np.mean(diffs)), 4),
            "n": len(diffs),
        }
    except ImportError:
        # 无 scipy 时退化为 bootstrap
        return bootstrap_ci_paired([a - b for a, b in zip(scores_a, scores_b)])
