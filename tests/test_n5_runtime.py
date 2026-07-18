"""N5 production bootstrap acceptance tests."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.research import MarkdownMemoryStore, ResearchStatus
from src.research.runtime import build_research_runtime, limits_from_config


class RuntimeTool:
    name = "web_search"

    def __init__(self) -> None:
        self.calls = 0

    def get_openai_tool_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "offline runtime tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {
            "results": [
                {
                    "title": "Runtime source",
                    "url": "https://example.com/runtime",
                    "snippet": "A source returned by the N5 runtime test.",
                }
            ]
        }


class RuntimePolicy:
    def __call__(self, messages, *, tools=None):
        if "before research begins" in str(messages[0].get("content", "")):
            revised = "narrow" in str(messages[-1].get("content", "")).lower()
            return {
                "content": json.dumps(
                    {
                        "objective": "Narrow objective" if revised else "Runtime objective",
                        "scope": ["runtime"],
                        "directions": ["verify production wiring"],
                        "constraints": ["cite sources"],
                        "expected_output": "Markdown report",
                    }
                ),
                "tool_calls": [],
            }
        if messages[-1]["role"] == "tool":
            return {
                "content": json.dumps(
                    {
                        "status": "completed",
                        "summary": "Runtime wiring works.",
                        "findings": ["The workflow used the configured tool."],
                        "unresolved": [],
                    }
                ),
                "tool_calls": [],
            }
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "runtime-tool-call",
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "arguments": json.dumps({"query": "runtime"}),
                    },
                }
            ],
        }

    def fork(self):
        return RuntimePolicy()


def _interrupt(state: dict[str, Any]) -> dict[str, Any]:
    return state["__interrupt__"][0].value


@pytest.mark.asyncio
async def test_runtime_starts_reviews_and_persists_one_markdown_result(tmp_path: Path) -> None:
    tool = RuntimeTool()
    runtime = build_research_runtime(
        {},
        policy=RuntimePolicy(),
        tools=[tool],
        memory_store=MarkdownMemoryStore(tmp_path),
    )

    paused = await runtime.start("Does the production runtime work?", thread_id="root-n5")
    assert _interrupt(paused)["brief"]["objective"] == "Runtime objective"
    assert tool.calls == 0

    revised = await runtime.review("root-n5", "modify", "Narrow the objective")
    assert _interrupt(revised)["brief"]["revision"] == 1
    final = await runtime.review("root-n5", "confirm")

    result = final["workflow_result"]
    assert result.research_result.status == ResearchStatus.COMPLETED
    assert result.memory_manifest.report_path.startswith("reports/")
    assert (tmp_path / result.memory_manifest.report_path).exists()
    assert tool.calls == 1


@pytest.mark.asyncio
async def test_runtime_auto_confirmation_is_explicit_and_thread_isolated(tmp_path: Path) -> None:
    runtime = build_research_runtime(
        {},
        policy=RuntimePolicy(),
        tools=[RuntimeTool()],
        memory_store=MarkdownMemoryStore(tmp_path),
    )

    first = await runtime.run_auto_confirmed("First", thread_id="root-auto-first")
    second = await runtime.run_auto_confirmed("Second", thread_id="root-auto-second")

    assert first.memory_manifest.report_path != second.memory_manifest.report_path
    assert (await runtime.get_state("root-auto-first"))["question"] == "First"
    assert (await runtime.get_state("root-auto-second"))["question"] == "Second"


def test_limits_are_loaded_only_from_the_research_contract() -> None:
    limits = limits_from_config(
        {
            "research": {
                "limits": {
                    "max_iterations": 3,
                    "max_fork_depth": 1,
                    "max_total_threads": 2,
                }
            },
            "orchestrator": {"max_total_tasks": 999},
        }
    )
    assert limits.max_iterations == 3
    assert limits.max_fork_depth == 1
    assert limits.max_total_threads == 2
