"""Fixed-input acceptance comparison captured before deleting the legacy loop."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.research import MarkdownMemoryStore, ResearchStatus
from src.research.runtime import build_research_runtime


FIXTURE = Path(__file__).parent / "fixtures" / "n5_legacy_fixed_result.json"


class FixedComparisonTool:
    name = "web_search"

    def get_openai_tool_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "fixed offline source lookup",
                "parameters": {"type": "object", "properties": {}},
            },
        }

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "results": [
                {
                    "title": "Fixed offline source",
                    "url": "https://example.com/fixed-offline-source",
                    "snippet": "The fixed offline finding is reproducible.",
                }
            ]
        }


class FixedComparisonPolicy:
    def __call__(self, messages, *, tools=None):
        if "before research begins" in str(messages[0].get("content", "")):
            return {
                "content": json.dumps(
                    {
                        "objective": "Compare the fixed offline finding",
                        "scope": ["fixed offline evidence"],
                        "directions": ["verify reproducibility"],
                        "constraints": ["use the supplied fixed source"],
                        "expected_output": "Evidence-backed Markdown report",
                    }
                ),
                "tool_calls": [],
            }
        if messages[-1]["role"] == "tool":
            return {
                "content": json.dumps(
                    {
                        "status": "completed",
                        "summary": "The fixed offline finding is reproducible.",
                        "findings": ["The fixed offline finding is reproducible."],
                        "unresolved": [],
                    }
                ),
                "tool_calls": [],
            }
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "fixed-source-call",
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "arguments": json.dumps({"query": "fixed offline finding"}),
                    },
                }
            ],
        }

    def fork(self):
        return FixedComparisonPolicy()


class BrokenResearchPolicy(FixedComparisonPolicy):
    def __call__(self, messages, *, tools=None):
        if "before research begins" in str(messages[0].get("content", "")):
            return super().__call__(messages, tools=tools)
        raise RuntimeError("fixed model failure")

    def fork(self):
        return BrokenResearchPolicy()


@pytest.mark.asyncio
async def test_new_workflow_preserves_delivery_and_adds_locatable_evidence(tmp_path: Path) -> None:
    legacy = json.loads(FIXTURE.read_text(encoding="utf-8"))
    runtime = build_research_runtime(
        {},
        policy=FixedComparisonPolicy(),
        tools=[FixedComparisonTool()],
        memory_store=MarkdownMemoryStore(tmp_path),
    )

    current = await runtime.run_auto_confirmed(
        legacy["query"],
        thread_id="root-fixed-comparison",
    )

    assert legacy["baseline_commit"] == "82a4fa7"
    assert legacy["report"]["content"].strip()
    assert current.research_result.status == ResearchStatus.COMPLETED
    assert "fixed offline finding is reproducible" in current.report_markdown.lower()
    assert current.research_result.evidence
    assert current.research_result.evidence[0].source_ref.startswith("https://")
    assert "[[evidence/" in current.report_markdown
    assert not hasattr(current.research_result, "confidence")


@pytest.mark.asyncio
async def test_new_workflow_turns_fixed_model_failure_into_a_persisted_typed_result(
    tmp_path: Path,
) -> None:
    legacy = json.loads(FIXTURE.read_text(encoding="utf-8"))
    runtime = build_research_runtime(
        {},
        policy=BrokenResearchPolicy(),
        tools=[],
        memory_store=MarkdownMemoryStore(tmp_path),
    )

    current = await runtime.run_auto_confirmed(
        legacy["query"],
        thread_id="root-fixed-failure",
    )

    assert legacy["failure_contract"]["all_task_failures"] == "terminal_failed"
    assert current.research_result.status == ResearchStatus.FAILED
    assert current.research_result.stop_reason == "policy_error: fixed model failure"
    assert (tmp_path / current.memory_manifest.report_path).exists()
