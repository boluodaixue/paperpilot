"""
GapAnalyzerAgent — Evidence Graph 驱动的知识缺口分析器（Research Loop 核心）。

Reading data from evidence coverage stats, decide whether to continue research
(produce supplementary sub-tasks for under-covered topics) or stop (synthesis).
Strict JSON output; parse failure falls back to 'complete' (safe, no waste).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class GapTask:
    """缺口补搜任务（与 SubTask 兼容的字段子集）。"""

    description: str
    search_hints: list[str] = field(default_factory=list)


@dataclass
class GapPlan:
    """Gap 分析结果：continue（补研究）或 complete（研究充分）。"""

    decision: str  # 'continue' | 'complete'
    gaps: list[dict] = field(default_factory=list)     # [{topic, reason}]
    new_tasks: list[GapTask] = field(default_factory=list)
    reason: str = ""


SYSTEM_GAP_ANALYZER = (
    "你是一名研究缺口分析器（Gap Analyst）。在迭代式深研究中，你分析每个子主题的证据覆盖，"
    "决定是否需要补研究，或研究是否已充分。\n"
    "规则：\n"
    "1. 对每个已规划子主题：证据数量低于阈值（0 条→缺口，1 条→较弱）视为缺口，"
    "产出 1 个补搜任务（description + search_hints 聚焦该子主题）。\n"
    "2. 若发现覆盖列表中明显缺少某个关键子主题，也可补产出任务。\n"
    "3. 所有子主题证据都达标、或没有值得补的方向 → decision=complete。\n"
    "4. 最多产出少量任务（调用方会再截断），不要重复已覆盖充分的子主题。\n"
    '5. 只输出一个 JSON 对象，无额外文字：\n'
    '{"decision":"continue|complete","reason":"结论理由",'
    '"gaps":[{"topic":"缺口子主题","reason":"为何缺口"}],'
    '"tasks":[{"description":"补搜任务描述","search_hints":["关键词1","关键词2"]}]}'
)


def _format_coverage(plan_topics: list[str], evidence_by_topic: dict[str, int], min_evidence: int) -> str:
    lines = [f"-   {t}: {evidence_by_topic.get(t, 0)} 条" for t in plan_topics]
    return "\n".join(lines) if lines else "（无计划子主题）"


def _parse_json(text: str) -> dict | None:
    """容错解析：剥离 markdown fence → json.loads → 大括号块。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        try:
            return json.loads(brace.group(0))
        except json.JSONDecodeError:
            pass
    return None


def run_gap_analysis(
    policy,
    query: str,
    round_num: int,
    plan_topics: list[str],
    evidence_by_topic: dict[str, int],
    min_evidence: int,
) -> GapPlan:
    """基于证据覆盖做缺口分析，返回 GapPlan。

    Args:
        policy: 同步 LLM policy。
        query: 研究问题。
        round_num: 当前研究轮次（第几轮收集完成）。
        plan_topics: 计划中的子主题列表（子任务描述）。
        evidence_by_topic: {子主题: 证据条数}。
        min_evidence: 子主题证据充分阈值。

    Returns:
        GapPlan；任何失败都按 complete（不浪费轮次）。
    """
    prompt = (
        "# 研究问题\n"
        f"{query}\n\n"
        f"# 第 {round_num} 轮证据覆盖\n"
        f"{_format_coverage(plan_topics, evidence_by_topic, min_evidence)}\n\n"
        f"证据充分阈值：每个子主题至少 {min_evidence} 条。"
    )
    messages = [
        {"role": "system", "content": SYSTEM_GAP_ANALYZER},
        {"role": "user", "content": prompt},
    ]
    try:
        resp = policy(messages)
        raw = resp.content if hasattr(resp, "content") else (resp or {}).get("content", "")
    except Exception as e:
        logger.warning(f"[GapAnalysis] LLM 调用失败，按 complete: {e}")
        return GapPlan(decision="complete", reason="Gap 分析 LLM 调用失败，视为研究充分")

    data = _parse_json(raw)
    if data is None:
        logger.warning("[GapAnalysis] 输出解析失败，按 complete")
        return GapPlan(decision="complete", reason="Gap 分析输出解析失败，视为研究充分")

    decision = str(data.get("decision", "complete")).lower().strip()
    gaps = [
        {"topic": g.get("topic", ""), "reason": g.get("reason", "")}
        for g in data.get("gaps", [])
        if isinstance(g, dict)
    ]
    tasks: list[GapTask] = []
    for t in data.get("tasks", []):
        if not isinstance(t, dict):
            continue
        desc = str(t.get("description", "")).strip()
        hints = [str(h) for h in t.get("search_hints", []) if isinstance(h, str)]
        if desc:
            tasks.append(GapTask(description=desc, search_hints=hints))
    return GapPlan(
        decision=decision,
        gaps=gaps,
        new_tasks=tasks,
        reason=str(data.get("reason", "")),
    )