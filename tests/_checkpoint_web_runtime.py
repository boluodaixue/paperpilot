"""Deterministic checkpointed Runtime fixture shared by historical Web tests."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from src.research.memory import MarkdownMemoryStore
from src.research.memory_workflows import build_memory_import_workflow
from src.research.runtime import ResearchRuntime, build_research_runtime


class CheckpointWebPolicy:
    """Small offline policy that exercises the real Memory workflow graphs."""

    def __init__(self) -> None:
        self.answer_calls = 0
        self.note_calls = 0
        self.import_calls = 0

    def __call__(self, messages, *, tools=None):
        del tools
        system = str(messages[0].get("content") or "")
        user = str(messages[-1].get("content") or "")
        if "before research begins" in system:
            return {
                "content": json.dumps(
                    {
                        "objective": "Continue selected-Memory research",
                        "scope": ["selected Memory"],
                        "directions": ["Find one new source"],
                        "constraints": ["Keep sources locatable"],
                        "expected_output": "Markdown report",
                        "known_information": ["Existing Memory content is known."],
                        "research_gaps": ["One new source remains."],
                    }
                ),
                "tool_calls": [],
            }
        if "Answer only from the supplied selected-Memory notes" in system:
            self.answer_calls += 1
            context = json.loads(user.split("MEMORY_CONTEXT_JSON:\n", 1)[1])
            path = context["hits"][0]["path"]
            return {
                "content": json.dumps(
                    {
                        "claims": [
                            {
                                "text": "The selected Memory supports this answer.",
                                "source_paths": [path],
                            }
                        ],
                        "insufficient_evidence": [],
                    }
                )
            }
        if "Create a complete Markdown note" in system:
            self.note_calls += 1
            contract = json.loads(
                user.split("FIXED_NOTE_CONTRACT_JSON:\n", 1)[1].split(
                    "\n\nMEMORY_ANSWER:", 1
                )[0]
            )
            fixed = contract["frontmatter"]
            sources = "\n".join(
                f"- [[{path[:-3]}]]" for path in contract["allowed_source_paths"]
            ) or "- None"
            markdown = (
                "---\n"
                f'id: {json.dumps(fixed["id"])}\n'
                f'type: {json.dumps(fixed["type"])}\n'
                f'memory_id: {json.dumps(fixed["memory_id"])}\n'
                'title: "Saved answer"\n'
                f'created_at: {json.dumps(fixed["created_at"])}\n'
                f'updated_at: {json.dumps(fixed["updated_at"])}\n'
                f'origin: {json.dumps(fixed["origin"])}\n'
                f'status: {json.dumps(fixed["status"])}\n'
                "tags:\n  - paperpilot\n"
                "---\n\n# Saved answer\n\nGrounded answer.\n\n"
                f"## Sources\n\n{sources}\n"
            )
            return {"content": json.dumps({"markdown": markdown})}

        self.import_calls += 1
        context = json.loads(user.split("IMPORT_CONTEXT_JSON:\n", 1)[1])
        locator = context["excerpts"][0]["locator"]
        return {
            "content": json.dumps(
                {
                    "title": "Imported source",
                    "summary": "A checkpointed import.",
                    "support": [
                        {
                            "text": "The source supports one point.",
                            "locators": [locator],
                            "memory_paths": [],
                        }
                    ],
                    "conflicts": [],
                    "gaps": [],
                }
            )
        }


def build_checkpointed_web_runtime(
    root: Path,
    *,
    policy: CheckpointWebPolicy | None = None,
) -> ResearchRuntime:
    """Build the real S1 facade with an offline URL reader and in-memory saver."""
    store = MarkdownMemoryStore(root)
    saver = InMemorySaver()
    selected_policy = policy or CheckpointWebPolicy()
    runtime = build_research_runtime(
        {
            "research": {"limits": {"max_iterations": 3}},
            "runtime": {
                "proposal_ttl_seconds": 3600,
                "terminal_retention_seconds": 3600,
                "lease_seconds": 60,
                "sweep_interval_seconds": 5,
            },
        },
        policy=selected_policy,
        tools=[],
        memory_store=store,
        checkpointer=saver,
    )
    runtime.memory_import_graph = build_memory_import_workflow(
        store,
        selected_policy,
        checkpointer=saver,
        url_fetcher=lambda url: (url, "text/plain", b"offline URL content"),
    )
    return runtime


async def checkpoint_values(
    runtime: ResearchRuntime,
    workflow_type: str,
    workflow_id: str,
) -> dict[str, Any]:
    snapshot = await runtime.get_workflow_snapshot(workflow_type, workflow_id)
    return dict(snapshot.values)
