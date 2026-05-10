#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
src/core/judge.py
================================================================================
MiMo 2.5 Pro LLM-as-Judge 统一接口。

对外接口:
    - LLMJudge.score_single(report, query, ground_truth=None) -> dict
    - LLMJudge.compare_two(report_a, report_b, query) -> dict
================================================================================
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger("judge")


class LLMJudge:
    """基于 MiMo 2.5 Pro 的 LLM-as-Judge 评审器。"""

    def __init__(self, backend: str = "mimo") -> None:
        """
        Args:
            backend: Judge 后端名称，对应 ModelRouter 注册的后端。
        """
        self.backend = backend
        self._policy = None

    def _get_policy(self):
        """惰性初始化 policy，避免在导入时触发网络请求。"""
        if self._policy is None:
            from src.models.model_router import ModelRouter
            self._policy = ModelRouter.create_backend(self.backend)
        return self._policy

    # -----------------------------------------------------------------------
    # 单篇报告深度评分
    # -----------------------------------------------------------------------
    def score_single(
        self,
        report: str,
        query: str,
        ground_truth: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        对单篇报告进行 5 维度深度评分。

        返回结构:
            {
              "overall": {"score": 7.5, "reason": "..."},
              "dimensions": {
                "factual_accuracy": {"score": 8, "reason": "..."},
                "logical_consistency": {"score": 7, "reason": "..."},
                "citation_quality": {"score": 8, "reason": "..."},
                "comprehensiveness": {"score": 7, "reason": "..."}
              },
              "average": 7.5,
              "judge_backend": "mimo"
            }
        """
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
            policy = self._get_policy()
            messages = [
                {"role": "system", "content": "你是研究报告评审专家。必须输出合法 JSON，不要输出任何其他内容。"},
                {"role": "user", "content": prompt},
            ]
            resp = policy(messages)
            content = resp.get("content", "")

            result = self._extract_json(content)
            if result:
                scores = [
                    v["score"]
                    for v in result.values()
                    if isinstance(v, dict) and "score" in v
                ]
                avg = sum(scores) / len(scores) if scores else 0.0
                dimensions = {k: v for k, v in result.items() if k != "overall"}
                overall = result.get("overall", {"score": avg, "reason": ""})
                return {
                    "overall": overall,
                    "dimensions": dimensions,
                    "average": avg,
                    "judge_backend": self.backend,
                }
        except Exception as e:
            logger.warning(f"MiMo Judge 单篇评分失败: {e}")
            return {"error": str(e), "judge_backend": self.backend}

        return {"error": "无法解析 MiMo Judge 输出", "judge_backend": self.backend}

    # -----------------------------------------------------------------------
    # 两篇报告 head-to-head 对比
    # -----------------------------------------------------------------------
    def compare_two(
        self,
        report_a: str,
        report_b: str,
        query: str,
    ) -> dict[str, Any]:
        """
        对两份报告做 head-to-head 对比评分。

        返回结构:
            {
              "comprehensiveness": {"A": 4, "B": 5, "reason": "..."},
              "accuracy": {"A": 3, "B": 4, "reason": "..."},
              "structure": {"A": 4, "B": 4, "reason": "..."},
              "sources": {"A": 3, "B": 5, "reason": "..."},
              "judge_backend": "mimo"
            }
        """
        prompt = f"""你是一位严谨的研究报告评审专家。请对比以下两份研究报告，从 4 个维度评分（1-5分）。

研究问题：{query}

--- 报告 A ---
{report_a[:3000]}

--- 报告 B ---
{report_b[:3000]}

评分标准：
- comprehensiveness（覆盖面）：报告是否全面回答了研究问题的各个子维度
- accuracy（准确性）：报告中的事实、数据是否正确，有无明显幻觉
- structure（结构清晰度）：报告的组织结构是否合理，逻辑是否通顺
- sources（引用质量）：报告是否引用了可靠来源，引用是否充分

请输出严格 JSON 格式：
{{
  "comprehensiveness": {{"A": 分数, "B": 分数, "reason": "简短理由"}},
  "accuracy": {{"A": 分数, "B": 分数, "reason": "简短理由"}},
  "structure": {{"A": 分数, "B": 分数, "reason": "简短理由"}},
  "sources": {{"A": 分数, "B": 分数, "reason": "简短理由"}}
}}"""

        try:
            policy = self._get_policy()
            messages = [
                {"role": "system", "content": "你是研究报告评审专家。必须输出合法 JSON，不要输出任何其他内容。"},
                {"role": "user", "content": prompt},
            ]
            resp = policy(messages)
            content = resp.get("content", "")

            result = self._extract_json(content)
            if result:
                result["judge_backend"] = self.backend
                return result
        except Exception as e:
            logger.warning(f"MiMo Judge 对比评分失败: {e}")
            return {"error": str(e), "judge_backend": self.backend}

        return {"error": "无法解析 MiMo Judge 输出", "judge_backend": self.backend}

    # -----------------------------------------------------------------------
    # 内部工具：JSON 提取
    # -----------------------------------------------------------------------
    @staticmethod
    def _extract_json(text: str) -> dict[str, Any] | None:
        """从文本中提取 JSON 对象，支持多种 fallback 策略。"""
        # 策略 1: 直接找最外层 {}
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass

        # 策略 2: 找 ```json ... ``` 代码块
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

        # 策略 3: 修复常见 JSON 错误后再解析
        cleaned = text.strip()
        # 去除可能的 Markdown 标记
        cleaned = re.sub(r"^```.*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
        # 修复单引号
        cleaned = cleaned.replace("'", '"')
        # 修复 trailing comma
        cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        return None
