#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
evaluation/judge.py
================================================================================
PaperPilot 的外部 LLM-as-Judge 评测接口。

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

_JUDGE_CHUNK_CHARS = 22_000
_JUDGE_CHUNK_OVERLAP = 500
_SCORE_KEYS = (
    "factual_accuracy",
    "logical_consistency",
    "citation_quality",
    "comprehensiveness",
    "overall",
)
_CHUNK_KEYS = (
    "factual_observations",
    "logical_observations",
    "citation_observations",
    "coverage_observations",
    "missing_or_uncertain",
)


def _report_chunks(report: str) -> list[str]:
    """Cover the complete report with bounded overlapping Judge inputs."""
    if len(report) <= _JUDGE_CHUNK_CHARS:
        return [report]
    chunks: list[str] = []
    start = 0
    while start < len(report):
        end = min(len(report), start + _JUDGE_CHUNK_CHARS)
        chunks.append(report[start:end])
        if end == len(report):
            break
        start = end - _JUDGE_CHUNK_OVERLAP
    return chunks


def _validated_score_payload(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, dict) or any(key not in value for key in _SCORE_KEYS):
        return None
    for key in _SCORE_KEYS:
        entry = value.get(key)
        if not isinstance(entry, dict) or not isinstance(entry.get("reason"), str):
            return None
        score = entry.get("score")
        if not isinstance(score, (int, float)) or not 0 <= float(score) <= 10:
            return None
    return value


def _validated_chunk_payload(value: dict[str, Any] | None) -> dict[str, Any] | None:
    """Accept only complete structured observations for a report chunk."""
    if not isinstance(value, dict) or set(value) != set(_CHUNK_KEYS):
        return None
    for key in _CHUNK_KEYS:
        entries = value.get(key)
        if (
            not isinstance(entries, list)
            or len(entries) > 6
            or any(not isinstance(entry, str) for entry in entries)
        ):
            return None
    return value


def _judge_input(report: str, chunks: list[str], *, complete: bool) -> dict[str, Any]:
    return {
        "report_chars": len(report),
        "chunks": len(chunks),
        "complete_report_covered": complete,
    }


class LLMJudge:
    """使用配置后端的 LLM-as-Judge 评审器。"""

    def __init__(self, backend: str = "deepseek", **policy_kwargs: Any) -> None:
        """
        Args:
            backend: Judge 后端名称，对应 ModelRouter 注册的后端。
            policy_kwargs: Judge 专用采样参数，例如 temperature 和 max_tokens。
        """
        self.backend = backend
        self.policy_kwargs = dict(policy_kwargs)
        self._policy = None

    def _get_policy(self):
        """惰性初始化 policy，避免在导入时触发网络请求。"""
        if self._policy is None:
            from src.models.model_router import ModelRouter
            self._policy = ModelRouter.create_backend(
                self.backend,
                **self.policy_kwargs,
            )
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

        chunks = _report_chunks(report)
        policy = self._get_policy()
        if len(chunks) == 1:
            report_context = f"--- Complete research report ---\n{report}"
        else:
            chunk_reviews: list[dict[str, Any]] = []
            for index, chunk in enumerate(chunks, 1):
                chunk_prompt = f"""Review chunk {index}/{len(chunks)} of a research report. Do not assign final scores.

Research question: {query}

{gt_section}
--- Report chunk {index}/{len(chunks)} ---
{chunk}

Return strict JSON with exactly these five arrays, each containing at most six strings:
{{
  "factual_observations": ["..."],
  "logical_observations": ["..."],
  "citation_observations": ["..."],
  "coverage_observations": ["..."],
  "missing_or_uncertain": ["..."]
}}"""
                try:
                    chunk_messages = [
                        {
                            "role": "system",
                            "content": (
                                "You are a research-report chunk reviewer. "
                                "Return valid JSON only."
                            ),
                        },
                        {"role": "user", "content": chunk_prompt},
                    ]
                    chunk_response = policy(chunk_messages)
                    chunk_raw = str(chunk_response.get("content", ""))
                    parsed_chunk = _validated_chunk_payload(
                        self._extract_json(chunk_raw)
                    )
                    if parsed_chunk is None:
                        repaired_chunk = policy(
                            [
                                *chunk_messages,
                                {"role": "assistant", "content": chunk_raw[:8000]},
                                {
                                    "role": "user",
                                    "content": (
                                        "REPAIR_CHUNK_OBSERVATION_JSON\n"
                                        "Repair structure only and preserve the review. "
                                        "Return exactly five arrays named "
                                        "factual_observations, logical_observations, "
                                        "citation_observations, coverage_observations, "
                                        "and missing_or_uncertain. Each array may contain "
                                        "at most six strings. Return JSON only."
                                    ),
                                },
                            ]
                        )
                        parsed_chunk = _validated_chunk_payload(
                            self._extract_json(
                                str(repaired_chunk.get("content", ""))
                            )
                        )
                    if parsed_chunk is None:
                        return {
                            "error": f"invalid Judge observation schema for chunk {index}",
                            "judge_backend": self.backend,
                            "judge_input": _judge_input(
                                report,
                                chunks,
                                complete=False,
                            ),
                        }
                    chunk_reviews.append(parsed_chunk)
                except Exception as exc:
                    return {
                        "error": (
                            f"Judge chunk {index} failed: "
                            f"{type(exc).__name__}: {exc}"
                        ),
                        "judge_backend": self.backend,
                        "judge_input": _judge_input(
                            report,
                            chunks,
                            complete=False,
                        ),
                    }
            report_context = (
                "--- Structured observations covering every report chunk ---\n"
                + json.dumps(chunk_reviews, ensure_ascii=False)
            )

        prompt = f"""You are a rigorous research-report evaluator. Score the complete report evidence below.

Research question: {query}

{gt_section}
{report_context}

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
            messages = [
                {"role": "system", "content": "你是研究报告评审专家。必须输出合法 JSON，不要输出任何其他内容。"},
                {"role": "user", "content": prompt},
            ]
            resp = policy(messages)
            content = resp.get("content", "")

            result = _validated_score_payload(self._extract_json(content))
            if result is None:
                repair = policy(
                    [
                        messages[0],
                        messages[1],
                        {"role": "assistant", "content": str(content)[:8000]},
                        {
                            "role": "user",
                            "content": (
                                "你的输出不符合评分 JSON 契约。只修复结构，不改变判断。"
                                "必须且只能包含 factual_accuracy、logical_consistency、"
                                "citation_quality、comprehensiveness、overall 五个键；"
                                "每个值必须是 {score: 0到10数字, reason: 字符串}。"
                            ),
                        },
                    ]
                )
                result = _validated_score_payload(
                    self._extract_json(str(repair.get("content", "")))
                )
            if result is not None:
                scores = [float(result[key]["score"]) for key in _SCORE_KEYS]
                avg = sum(scores) / len(scores) if scores else 0.0
                dimensions = {k: v for k, v in result.items() if k != "overall"}
                overall = result.get("overall", {"score": avg, "reason": ""})
                return {
                    "overall": overall,
                    "dimensions": dimensions,
                    "average": avg,
                    "judge_backend": self.backend,
                    "judge_input": _judge_input(report, chunks, complete=True),
                }
        except Exception as e:
            logger.warning(f"LLM Judge 单篇评分失败: {e}")
            return {
                "error": str(e),
                "judge_backend": self.backend,
                "judge_input": _judge_input(report, chunks, complete=True),
            }

        return {
            "error": "无法解析 LLM Judge 输出",
            "judge_backend": self.backend,
            "judge_input": _judge_input(report, chunks, complete=True),
        }

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
            logger.warning(f"LLM Judge 对比评分失败: {e}")
            return {"error": str(e), "judge_backend": self.backend}

        return {"error": "无法解析 LLM Judge 输出", "judge_backend": self.backend}

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
