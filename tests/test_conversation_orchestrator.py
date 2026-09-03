"""Conversation routing stays product-side and side-effect free."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from src.conversation import (
    ActionOverride,
    ConversationAction,
    ConversationMessage,
    ConversationRequest,
    MemorySelection,
    route_conversation,
)


ROOT = Path(__file__).resolve().parents[1]


class QueuePolicy:
    def __init__(self, *contents: str) -> None:
        self.contents = list(contents)
        self.calls = 0

    def __call__(self, messages, *, tools=None):
        self.calls += 1
        assert tools == []
        return {"content": self.contents.pop(0), "tool_calls": []}


def _payload(action: str, **overrides) -> str:
    payload = {
        "action": action,
        "confidence": 0.9,
        "response": "",
        "query": "",
        "reason_code": "fixture",
    }
    payload.update(overrides)
    return json.dumps(payload)


@pytest.mark.asyncio
async def test_greeting_is_answered_without_research_or_memory() -> None:
    policy = QueuePolicy(_payload("reply", response="我是 PaperPilot。"))

    decision = await route_conversation(
        ConversationRequest("你是什么？"),
        policy,
    )

    assert decision.action is ConversationAction.REPLY
    assert decision.response == "我是 PaperPilot。"
    assert decision.requires_confirmation is False
    assert decision.requires_memory is False


@pytest.mark.asyncio
async def test_memory_route_requires_an_explicit_selection() -> None:
    policy = QueuePolicy(_payload("memory_answer", query="Transformer"))

    decision = await route_conversation(
        ConversationRequest("这个 Memory 里有 Transformer 吗？"),
        policy,
    )

    assert decision.action is ConversationAction.CLARIFY
    assert decision.reason_code == "memory_selection_required"


@pytest.mark.asyncio
async def test_explicit_research_bypasses_the_router_model() -> None:
    policy = QueuePolicy()

    decision = await route_conversation(
        ConversationRequest(
            "研究 Transformer 的发展",
            explicit_action=ActionOverride.DEEP_RESEARCH,
        ),
        policy,
    )

    assert policy.calls == 0
    assert decision.action is ConversationAction.PROPOSE_RESEARCH
    assert decision.requires_confirmation is True


@pytest.mark.asyncio
async def test_selected_memory_is_routed_without_exposing_its_id_to_the_model() -> None:
    class InspectPolicy:
        def __call__(self, messages, *, tools=None):
            payload = json.loads(messages[-1]["content"])
            assert payload["selected_memory"] == {
                "available": True,
                "title": "Transformer Notes",
                "read_only": False,
            }
            assert "M-secret" not in messages[-1]["content"]
            return {
                "content": _payload("memory_answer", query="已有结论"),
                "tool_calls": [],
            }

    decision = await route_conversation(
        ConversationRequest(
            "总结已有结论",
            recent_messages=(ConversationMessage("user", "前一个问题"),),
            selected_memory=MemorySelection("M-secret", "Transformer Notes"),
        ),
        InspectPolicy(),
    )

    assert decision.action is ConversationAction.MEMORY_ANSWER
    assert decision.requires_memory is True


@pytest.mark.asyncio
async def test_invalid_router_transport_is_repaired_once() -> None:
    policy = QueuePolicy(
        "not json",
        _payload("clarify", response="你希望快速查找还是深度研究？"),
    )

    decision = await route_conversation(
        ConversationRequest("再查一下"),
        policy,
    )

    assert policy.calls == 2
    assert decision.action is ConversationAction.CLARIFY


def test_orchestrator_has_no_research_or_persistence_imports() -> None:
    source = (ROOT / "src/conversation/orchestrator.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not {
        module
        for module in imported
        if any(
            part in module.split(".")
            for part in ("research", "memory", "obsidian", "vault", "web")
        )
    }
