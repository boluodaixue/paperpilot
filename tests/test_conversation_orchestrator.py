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
        "research_ready": True,
        "clarifying_question": "",
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


@pytest.mark.asyncio
async def test_research_proposal_with_a_question_is_downgraded_to_clarification() -> None:
    policy = QueuePolicy(_payload(
        "propose_research",
        query="Agent long-horizon tasks",
        response="你希望重点研究可靠性还是规划能力？",
    ))

    decision = await route_conversation(
        ConversationRequest("帮查一些关于 Agent 长程任务的问题"),
        policy,
    )

    assert decision.action is ConversationAction.CLARIFY
    assert decision.reason_code == "service_action_still_needs_clarification"
    assert decision.requires_confirmation is False


@pytest.mark.asyncio
async def test_topic_only_research_request_asks_for_scope() -> None:
    policy = QueuePolicy(_payload(
        "propose_research",
        query="Agent long-horizon tasks",
        research_ready=False,
        clarifying_question="你更关注规划、记忆还是长期执行可靠性？",
    ))

    decision = await route_conversation(
        ConversationRequest("帮查一些关于 Agent 长程任务的问题"),
        policy,
    )

    assert decision.action is ConversationAction.CLARIFY
    assert decision.reason_code == "research_request_needs_scope"


@pytest.mark.asyncio
async def test_narrowing_answer_is_exposed_as_conversation_continuation() -> None:
    class InspectContinuationPolicy:
        def __call__(self, messages, *, tools=None):
            payload = json.loads(messages[-1]["content"])
            assert payload["conversation_continuation"] == {
                "latest_assistant_question": "你想重点了解哪个方向？",
                "prior_user_goal": "先帮我查一下最新技术",
                "newest_answer": "记忆机制",
            }
            return {
                "content": _payload(
                    "propose_research",
                    query="Agent 长程任务中的最新记忆机制技术",
                    research_ready=True,
                ),
                "tool_calls": [],
            }

    decision = await route_conversation(
        ConversationRequest(
            "记忆机制",
            recent_messages=(
                ConversationMessage("user", "先帮我查一下最新技术"),
                ConversationMessage("assistant", "你想重点了解哪个方向？"),
            ),
        ),
        InspectContinuationPolicy(),
    )

    assert decision.action is ConversationAction.PROPOSE_RESEARCH
    assert decision.query == "Agent 长程任务中的最新记忆机制技术"


@pytest.mark.asyncio
async def test_quick_search_mode_command_reconstructs_prior_topic() -> None:
    policy = QueuePolicy()

    decision = await route_conversation(
        ConversationRequest(
            "快速联网查",
            recent_messages=(
                ConversationMessage("user", "帮我查 Agent 长程任务"),
                ConversationMessage("assistant", "你想重点了解哪个方向？"),
                ConversationMessage("user", "记忆机制"),
            ),
        ),
        policy,
    )

    assert decision.action is ConversationAction.QUICK_SEARCH
    assert policy.calls == 0
    assert decision.query == "帮我查 Agent 长程任务；记忆机制"


@pytest.mark.asyncio
async def test_concrete_option_answer_does_not_trigger_repeated_clarification() -> None:
    policy = QueuePolicy()

    decision = await route_conversation(
        ConversationRequest(
            "记忆机制",
            recent_messages=(
                ConversationMessage("user", "帮查一些关于 Agent 长程任务的问题"),
                ConversationMessage(
                    "assistant",
                    "你想了解评测基准、规划与记忆，还是应用场景？",
                ),
            ),
        ),
        policy,
    )

    assert policy.calls == 0
    assert decision.action is ConversationAction.PROPOSE_RESEARCH
    assert decision.reason_code == "resolved_research_clarification"
    assert "Agent 长程任务" in decision.query
    assert "记忆机制" in decision.query


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
