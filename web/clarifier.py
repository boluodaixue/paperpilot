#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
web/clarifier.py
================================================================================
PaperPilot 研究前澄清（Research Scoping）LLM 调用。

在正式启动深度研究（会拆分子任务、消耗大量 token）之前，先用一次短 LLM 调用
与用户确认研究方向/范围：信息不足时主动追问（ask），足够时产出研究方案（confirm）。

设计：每轮只问 1 个问题、最多追问 2 轮，之后取合理默认直接 confirm；
输出严格 JSON，解析失败时安全降级为 ask（保守，不误启动研究）。
================================================================================
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

MAX_CLARIFY_ROUNDS = 2

SYSTEM_CLARIFIER = (
    "你是一名研究范围确认助手（Research Scoper）。在大型深度研究启动前，你需要"
    "和用户确认研究方向与范围，避免跑偏或浪费算力。\n"
    "决策规则：\n"
    "1. 如果用户的意图已经足够清晰（主题、范围、角度明确），输出 action=confirm，"
    "   附一份简洁的研究方案 plan。\n"
    "2. 如果存在会实质影响研究方向的歧义（如：时间范围、地域/语言、应用领域、"
    "   要求的深度、切入角度、对比基准），输出 action=ask，每轮只问 1 个最关键的"
    "   问题（question）。\n"
    "3. 不要无谓追问；总体最多追问 2 轮。若用户已作答但仍模糊，取合理默认并 confirm。\n"
    "4. plan 格式：{topic, scope, angle, depth, focus_areas:[...]}；"
    "research_query 是合并澄清后的最终研究查询串（中文，供启动研究使用）。\n"
    "输出必须是严格 JSON，不要任何额外文字。\n"
    '格式一 {"action":"ask","question":"...仅一句话..."}\n'
    '格式二 {"action":"confirm","plan":{"topic":"...","scope":"...","angle":"...",'
    '"depth":"...","focus_areas":["...","..."]},"research_query":"最终研究查询串"}'
)


def _format_history(messages: list[dict], max_items: int = 10) -> str:
    """把会话历史压成 prompt 文本（保留最近 max_items 条）。"""
    if not messages:
        return "（无历史）"
    lines = []
    for m in messages[-max_items:]:
        role = "用户" if m.get("role") == "user" else "助手"
        lines.append(f"[{role}] {m.get('content', '')}")
    return "\n".join(lines)


def _parse_json(lenient_text: str) -> dict | None:
    """容错解析：剥离 markdown fence → 直接 json.loads → 取大括号块。"""
    text = (lenient_text or "").strip()
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


def _looks_like_api_error(text: str) -> bool:
    """LLM 返回的 assistant content 可能是 API 错误文本（VLLMPolicy 不抛异常）。"""
    t = (text or "").lower()
    return "error code" in t or "error:" in t and "429" in t or "rate limit" in t


def run_clarifier(policy, messages: list[dict], user_msg: str) -> dict[str, Any]:
    """对最新用户消息做澄清决策，返回结构化结果。

    Args:
        policy: LLM policy（同步 __call__）。
        messages: 会话历史（chat_store.get_messages 的结构）。
        user_msg: 本次用户新输入。

    Returns:
        规范化的结果 dict（始终含 action；confirm 额外含 plan 与 research_query）。
    """
    history = _format_history(messages)
    prompt = (
        "# 研究意图（用户）\n"
        f"{user_msg}\n\n"
        "# 会话历史\n"
        f"{history}"
    )
    llm_messages = [
        {"role": "system", "content": SYSTEM_CLARIFIER},
        {"role": "user", "content": prompt},
    ]
    try:
        resp = policy(llm_messages)
        raw = resp.content if hasattr(resp, "content") else (resp or {}).get("content", "")
    except Exception as e:
        logger.warning(f"[Clarify] LLM 调用失败，保守 ask: {e}")
        return {"action": "ask", "question": "请再补充一些研究背景，我好确认研究范围。"}

    if _looks_like_api_error(raw):
        logger.warning(f"[Clarify] LLM 返回 API 错误，保守 ask: {raw[:120]}")
        return {"action": "ask", "question": "（澄清服务暂时不可用，请稍后重试或直接补充研究背景。）"}

    result = _parse_json(raw)
    if result is None:
        # 解析失败 → 保守 ask，不误启动研究
        return {"action": "ask", "question": raw[:200] or "请补充信息以确认研究范围。"}

    action = str(result.get("action", "")).lower().strip()
    if action == "confirm":
        plan = result.get("plan") or {}
        return {
            "action": "confirm",
            "plan": {
                "topic": plan.get("topic", ""),
                "scope": plan.get("scope", ""),
                "angle": plan.get("angle", ""),
                "depth": plan.get("depth", ""),
                "focus_areas": plan.get("focus_areas") or [],
            },
            "research_query": str(result.get("research_query") or user_msg),
        }
    return {
        "action": "ask",
        "question": str(result.get("question") or "请补充信息以确认研究范围。"),
    }