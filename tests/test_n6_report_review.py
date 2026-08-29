"""Deterministic offline acceptance tests for optional N6 report review."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from src.research import (
    ExecutionIdentity,
    MarkdownMemoryStore,
    MemoryManifest,
    ResearchStatus,
    build_research_runtime,
    build_research_workflow,
    create_research_workflow_state,
    resume_research_workflow,
)
from src.research.report_review import validate_revised_report
from tests._research_assessment import assessment_response


SOURCE_URL = "https://arxiv.org/abs/1706.03762"


def _tool_call() -> dict[str, Any]:
    return {
        "id": "call-web-search",
        "type": "function",
        "function": {
            "name": "web_search",
            "arguments": json.dumps({"query": "transformer evidence"}),
        },
    }


class FixedWebTool:
    name = "web_search"

    def __init__(self) -> None:
        self.calls = 0

    def get_openai_tool_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "fixed offline search",
                "parameters": {"type": "object", "properties": {}},
            },
        }

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {
            "results": [
                {
                    "title": "Attention Is All You Need",
                    "url": SOURCE_URL,
                    "snippet": "The Transformer uses attention rather than recurrence.",
                }
            ]
        }


class TrackingMemoryStore(MarkdownMemoryStore):
    """Capture the immutable bundle before optional report-only replacement."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.draft_report: str | None = None
        self.persisted_manifest = None
        self.persisted_result = None
        self.non_report_before: dict[str, str] = {}

    def persist_research(self, brief, result, identity):
        report, manifest = super().persist_research(brief, result, identity)
        self.draft_report = report
        self.persisted_manifest = manifest
        self.persisted_result = result
        paths = (*manifest.evidence_paths, *manifest.source_paths)
        self.non_report_before = {path: self.read_text(path) for path in paths}
        return report, manifest


class FailingReplaceStore(TrackingMemoryStore):
    def replace_report(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("report replacement unavailable")


Payload = str | dict[str, Any] | Callable[[str], str | dict[str, Any]]


class ReviewPolicy:
    """One deterministic policy for alignment, research, Red, and Blue."""

    def __init__(
        self,
        store: TrackingMemoryStore,
        *,
        red_payload: Payload | None = None,
        blue_payload: Payload | None = None,
    ) -> None:
        self.store = store
        self.red_payload = red_payload if red_payload is not None else {"issues": []}
        self.blue_payload = blue_payload
        self.alignment_calls = 0
        self.research_calls = 0
        self.red_calls = 0
        self.blue_calls = 0
        self.red_tools: list[list[dict[str, Any]]] = []
        self.blue_tools: list[list[dict[str, Any]]] = []

    @staticmethod
    def _content(payload: Payload, draft: str) -> str:
        value = payload(draft) if callable(payload) else payload
        return value if isinstance(value, str) else json.dumps(value)

    def __call__(self, messages, *, tools=None):
        assessment = assessment_response(messages)
        if assessment is not None:
            return assessment
        system = str(messages[0].get("content", ""))
        lowered = system.lower()
        active_tools = list(tools or [])

        if "before research begins" in lowered:
            self.alignment_calls += 1
            return {
                "content": json.dumps(
                    {
                        "objective": "Explain the evidence behind Transformer architecture",
                        "scope": ["architecture", "empirical evidence"],
                        "directions": ["original paper"],
                        "constraints": ["preserve source links"],
                        "expected_output": "An evidence-backed Markdown report",
                    }
                ),
                "tool_calls": [],
            }

        if lowered.startswith("you are the red reviewer"):
            self.red_calls += 1
            self.red_tools.append(active_tools)
            draft = self.store.draft_report or ""
            return {"content": self._content(self.red_payload, draft), "tool_calls": []}

        if lowered.startswith("you are the blue editor"):
            self.blue_calls += 1
            self.blue_tools.append(active_tools)
            if self.blue_payload is None:
                raise AssertionError("Blue must not run without an explicit response")
            draft = self.store.draft_report or ""
            return {"content": self._content(self.blue_payload, draft), "tool_calls": []}

        self.research_calls += 1
        if messages[-1]["role"] == "tool" or tools == []:
            return {
                "content": json.dumps(
                    {
                        "status": "completed",
                        "summary": (
                            "Attention replaced recurrence. Provisional wording. "
                            f"Source: {SOURCE_URL}"
                        ),
                        "findings": ["The original architecture is attention-based."],
                        "unresolved": [],
                    }
                ),
                "tool_calls": [],
            }
        return {"content": "", "tool_calls": [_tool_call()]}


def _identity(thread_id: str) -> ExecutionIdentity:
    return ExecutionIdentity(thread_id, None, thread_id, 0)


def _config(thread_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": thread_id}}


async def _complete(
    tmp_path: Path,
    *,
    thread_id: str,
    enabled: bool,
    red_payload: Payload | None = None,
    blue_payload: Payload | None = None,
    store_type: type[TrackingMemoryStore] = TrackingMemoryStore,
    checkpointer: InMemorySaver | None = None,
):
    store = store_type(tmp_path)
    policy = ReviewPolicy(
        store,
        red_payload=red_payload,
        blue_payload=blue_payload,
    )
    tool = FixedWebTool()
    graph = build_research_workflow(
        policy,
        [tool],
        store,
        checkpointer=checkpointer,
        report_review_enabled=enabled,
    )
    identity = _identity(thread_id)
    await graph.ainvoke(
        create_research_workflow_state("How do Transformers work?", identity),
        config=_config(thread_id),
    )
    final = await resume_research_workflow(
        graph,
        thread_id=thread_id,
        action="confirm",
    )
    return graph, store, policy, tool, final["workflow_result"]


def _issue() -> dict[str, str]:
    return {
        "category": "factual",
        "target": "Attention replaced recurrence.",
        "description": "Use more precise wording without changing the cited source.",
    }


def _successful_blue(draft: str) -> dict[str, Any]:
    edits = [
        {
            "operation": "MODIFY",
            "target": "Attention replaced recurrence.",
            "replacement": "Attention mechanisms replaced recurrence.",
        },
        {
            "operation": "DELETE",
            "target": "Provisional wording.",
            "replacement": "",
        },
        {
            "operation": "ADD",
            "target": "## Findings",
            "replacement": "\n\nAn additional synthesis sentence.",
        },
        {
            "operation": "VERIFY",
            "target": SOURCE_URL,
            "replacement": "",
        },
    ]
    revised = draft
    for edit in edits:
        operation = edit["operation"]
        target = edit["target"]
        replacement = edit["replacement"]
        assert revised.count(target) == 1
        if operation == "ADD":
            revised = revised.replace(target, target + replacement, 1)
        elif operation == "DELETE":
            revised = revised.replace(target, "", 1)
        elif operation == "MODIFY":
            revised = revised.replace(target, replacement, 1)
    return {
        "edits": edits,
        "report_markdown": revised,
    }


def _bundle_files(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*.md"))
    }


def test_replace_report_accepts_only_an_existing_report_path(tmp_path) -> None:
    store = MarkdownMemoryStore(tmp_path)
    report = tmp_path / "reports" / "Report-test.md"
    evidence = tmp_path / "evidence" / "Evidence-test.md"
    source = tmp_path / "sources" / "Source-test.md"
    for path, content in (
        (report, "original report"),
        (evidence, "original evidence"),
        (source, "original source"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    store.replace_report("reports/Report-test.md", "revised report")
    assert report.read_text(encoding="utf-8") == "revised report"

    invalid_paths = [
        "evidence/Evidence-test.md",
        "sources/Source-test.md",
        str(report.resolve()),
        "../reports/Report-test.md",
        "reports/../evidence/Evidence-test.md",
    ]
    for invalid_path in invalid_paths:
        before = _bundle_files(tmp_path)
        with pytest.raises(ValueError):
            store.replace_report(invalid_path, "must not be written")
        assert _bundle_files(tmp_path) == before

    before = _bundle_files(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.replace_report("reports/Report-missing.md", "must not be written")
    assert _bundle_files(tmp_path) == before


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({}, False),
        ({"research": {}}, False),
        ({"research": {"report_review": {}}}, False),
        ({"research": {"report_review": {"enabled": True}}}, True),
    ],
)
def test_runtime_parses_report_review_enabled(
    tmp_path, config: dict[str, Any], expected: bool
) -> None:
    runtime = build_research_runtime(
        config,
        policy=lambda *args, **kwargs: None,
        tools=[],
        memory_store=MarkdownMemoryStore(tmp_path),
        checkpointer=InMemorySaver(),
    )

    assert runtime.report_review_enabled is expected


@pytest.mark.parametrize("value", [None, "true", 1, 0, []])
def test_runtime_rejects_non_boolean_report_review_enabled(tmp_path, value: Any) -> None:
    with pytest.raises(ValueError, match="enabled must be a boolean"):
        build_research_runtime(
            {"research": {"report_review": {"enabled": value}}},
            policy=lambda *args, **kwargs: None,
            tools=[],
            memory_store=MarkdownMemoryStore(tmp_path),
            checkpointer=InMemorySaver(),
        )


@pytest.mark.parametrize("value", [None, True, [], "enabled"])
def test_runtime_rejects_non_mapping_report_review_config(tmp_path, value: Any) -> None:
    with pytest.raises(ValueError, match="report_review must be a mapping"):
        build_research_runtime(
            {"research": {"report_review": value}},
            policy=lambda *args, **kwargs: None,
            tools=[],
            memory_store=MarkdownMemoryStore(tmp_path),
            checkpointer=InMemorySaver(),
        )


@pytest.mark.asyncio
async def test_review_is_disabled_by_default_without_extra_policy_calls(tmp_path) -> None:
    store = TrackingMemoryStore(tmp_path)
    policy = ReviewPolicy(store)
    tool = FixedWebTool()
    graph = build_research_workflow(policy, [tool], store)
    thread_id = "n6-disabled"
    await graph.ainvoke(
        create_research_workflow_state("How do Transformers work?", _identity(thread_id)),
        config=_config(thread_id),
    )
    final = await resume_research_workflow(graph, thread_id=thread_id, action="confirm")
    outcome = final["workflow_result"]

    assert outcome.report_review is None
    assert policy.alignment_calls == 1
    assert policy.research_calls == 2
    assert policy.red_calls == policy.blue_calls == 0


@pytest.mark.asyncio
async def test_enabled_review_applies_only_report_and_accepts_four_operations(tmp_path) -> None:
    _, store, policy, tool, outcome = await _complete(
        tmp_path,
        thread_id="n6-applied",
        enabled=True,
        red_payload={"issues": [_issue()]},
        blue_payload=_successful_blue,
    )

    review = outcome.report_review
    assert review is not None and review.applied is True
    assert review.fallback_reason is None
    assert [(issue.category, issue.target, issue.description) for issue in review.issues] == [
        ("factual", _issue()["target"], _issue()["description"])
    ]
    assert {edit.operation for edit in review.edits} == {"ADD", "DELETE", "MODIFY", "VERIFY"}
    assert policy.red_calls == policy.blue_calls == 1
    assert policy.red_tools == policy.blue_tools == [[]]
    assert tool.calls == 1

    assert outcome.research_result is store.persisted_result
    assert outcome.memory_manifest == store.persisted_manifest
    assert outcome.report_markdown != store.draft_report
    assert store.read_text(outcome.memory_manifest.report_path) == outcome.report_markdown
    for path, original in store.non_report_before.items():
        assert store.read_text(path) == original


@pytest.mark.asyncio
async def test_red_with_no_issues_skips_blue(tmp_path) -> None:
    _, store, policy, _, outcome = await _complete(
        tmp_path,
        thread_id="n6-no-issues",
        enabled=True,
        red_payload={"issues": []},
    )

    review = outcome.report_review
    assert review is not None and review.applied is False
    assert review.issues == review.edits == ()
    assert policy.red_calls == 1
    assert policy.blue_calls == 0
    assert outcome.report_markdown == store.draft_report


@pytest.mark.asyncio
async def test_red_policy_exception_falls_back_without_calling_blue(tmp_path) -> None:
    def raise_red(_: str) -> dict[str, Any]:
        raise RuntimeError("red reviewer unavailable")

    _, store, policy, _, outcome = await _complete(
        tmp_path,
        thread_id="n6-red-exception",
        enabled=True,
        red_payload=raise_red,
    )

    review = outcome.report_review
    assert review is not None and review.applied is False
    assert review.fallback_reason == "RuntimeError: red reviewer unavailable"
    assert policy.red_calls == 1
    assert policy.blue_calls == 0
    assert outcome.research_result.status == ResearchStatus.COMPLETED
    assert outcome.report_markdown == store.draft_report


@pytest.mark.asyncio
async def test_blue_policy_exception_falls_back_to_original_report(tmp_path) -> None:
    def raise_blue(_: str) -> dict[str, Any]:
        raise RuntimeError("blue editor unavailable")

    _, store, policy, _, outcome = await _complete(
        tmp_path,
        thread_id="n6-blue-exception",
        enabled=True,
        red_payload={"issues": [_issue()]},
        blue_payload=raise_blue,
    )

    review = outcome.report_review
    assert review is not None and review.applied is False
    assert review.fallback_reason == "RuntimeError: blue editor unavailable"
    assert policy.red_calls == policy.blue_calls == 1
    assert outcome.research_result.status == ResearchStatus.COMPLETED
    assert outcome.report_markdown == store.draft_report


@pytest.mark.asyncio
async def test_illegal_red_category_falls_back_without_calling_blue(tmp_path) -> None:
    invalid_issue = {**_issue(), "category": "style"}
    _, store, policy, _, outcome = await _complete(
        tmp_path,
        thread_id="n6-invalid-red-category",
        enabled=True,
        red_payload={"issues": [invalid_issue]},
    )

    review = outcome.report_review
    assert review is not None and review.applied is False
    assert review.fallback_reason and "category" in review.fallback_reason
    assert policy.red_calls == 1
    assert policy.blue_calls == 0
    assert outcome.report_markdown == store.draft_report


@pytest.mark.asyncio
async def test_all_red_categories_parse_with_verify_only_blue_review(tmp_path) -> None:
    issues = [
        {
            "category": "factual",
            "target": "Attention replaced recurrence.",
            "description": "Check factual precision.",
        },
        {
            "category": "logical_consistency",
            "target": "Provisional wording.",
            "description": "Check consistency with the finding.",
        },
        {
            "category": "citation_quality",
            "target": SOURCE_URL,
            "description": "Verify the existing source supports the statement.",
        },
    ]

    def verify_only(draft: str) -> dict[str, Any]:
        edits = [
            {
                "operation": "VERIFY",
                "target": issue["target"],
                "replacement": "",
            }
            for issue in issues
        ]
        assert all(draft.count(edit["target"]) == 1 for edit in edits)
        return {"edits": edits, "report_markdown": draft}

    _, store, policy, _, outcome = await _complete(
        tmp_path,
        thread_id="n6-all-red-categories",
        enabled=True,
        red_payload={"issues": issues},
        blue_payload=verify_only,
    )

    review = outcome.report_review
    assert review is not None and review.applied is False
    assert review.fallback_reason is None
    assert [issue.category for issue in review.issues] == [
        "factual",
        "logical_consistency",
        "citation_quality",
    ]
    assert [edit.operation for edit in review.edits] == ["VERIFY", "VERIFY", "VERIFY"]
    assert policy.red_calls == policy.blue_calls == 1
    assert policy.red_tools == policy.blue_tools == [[]]
    assert outcome.report_markdown == store.draft_report


def _invalid_blue_case(kind: str) -> Payload:
    def payload(draft: str) -> str | dict[str, Any]:
        valid = _successful_blue(draft)

        def single_edit(operation: str, target: str, replacement: str) -> dict[str, Any]:
            assert draft.count(target) == 1
            if operation == "ADD":
                revised = draft.replace(target, target + replacement, 1)
            elif operation == "DELETE":
                revised = draft.replace(target, "", 1)
            else:
                revised = draft.replace(target, replacement, 1)
            return {
                "edits": [
                    {
                        "operation": operation,
                        "target": target,
                        "replacement": replacement,
                    }
                ],
                "report_markdown": revised,
            }

        if kind == "malformed_blue":
            return "{not-json"
        if kind == "illegal_operation":
            valid["edits"][0]["operation"] = "REWRITE"
        elif kind == "target_missing":
            valid["edits"][0]["target"] = "text absent from the report"
        elif kind == "target_repeated":
            assert draft.count("Evidence") > 1
            valid["edits"][0]["target"] = "Evidence"
        elif kind == "delete_nonempty":
            valid["edits"][1]["replacement"] = "must be empty"
        elif kind == "verify_nonempty":
            valid["edits"][3]["replacement"] = "must be empty"
        elif kind == "add_empty":
            valid["edits"][2]["replacement"] = ""
        elif kind == "modify_empty":
            valid["edits"][0]["replacement"] = ""
        elif kind == "replay_mismatch":
            valid["report_markdown"] = draft.replace(
                "Attention replaced recurrence.",
                "A different undeclared revision.",
                1,
            )
        elif kind == "undeclared_change":
            valid["report_markdown"] += "\nAn undeclared prose change.\n"
        elif kind == "frontmatter_changed":
            line = re.search(r"(?m)^root_thread_id:.*$", draft)
            assert line is not None
            return single_edit(
                "MODIFY",
                line.group(0),
                'root_thread_id: "another-root"',
            )
        elif kind == "wikilink_removed":
            line = re.search(r"(?m)^- .*\[\[evidence/[^\]]+\]\]$", draft)
            assert line is not None
            return single_edit("DELETE", line.group(0), "")
        elif kind == "wikilink_added":
            return single_edit(
                "ADD",
                "## Evidence-backed Details",
                "\n\n[[evidence/not-in-manifest|Evidence]]",
            )
        elif kind == "invalid_wikilink":
            line = re.search(r"(?m)^- .*\[\[evidence/[^\]]+\]\]$", draft)
            assert line is not None
            replacement = re.sub(
                r"\[\[evidence/[^\]]+\]\]",
                "[[evidence/not-in-manifest|Evidence]]",
                line.group(0),
            )
            return single_edit("MODIFY", line.group(0), replacement)
        elif kind == "url_removed":
            return single_edit("DELETE", SOURCE_URL, "")
        elif kind == "url_added":
            return single_edit(
                "ADD",
                "## Summary",
                "\n\nhttps://untrusted.example/new-source",
            )
        return valid

    return payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "malformed_red",
        "malformed_blue",
        "illegal_operation",
        "target_missing",
        "target_repeated",
        "delete_nonempty",
        "verify_nonempty",
        "add_empty",
        "modify_empty",
        "replay_mismatch",
        "undeclared_change",
        "frontmatter_changed",
        "wikilink_removed",
        "wikilink_added",
        "invalid_wikilink",
        "url_removed",
        "url_added",
        "replace_failure",
    ],
)
async def test_invalid_or_failed_review_falls_back_to_original_report(tmp_path, case: str) -> None:
    red_payload: Payload = "{not-json" if case == "malformed_red" else {"issues": [_issue()]}
    blue_payload = None if case == "malformed_red" else _invalid_blue_case(case)
    store_type = FailingReplaceStore if case == "replace_failure" else TrackingMemoryStore

    _, store, policy, tool, outcome = await _complete(
        tmp_path,
        thread_id=f"n6-fallback-{case}",
        enabled=True,
        red_payload=red_payload,
        blue_payload=blue_payload,
        store_type=store_type,
    )

    review = outcome.report_review
    assert review is not None and review.applied is False
    assert review.fallback_reason
    assert outcome.research_result.status == ResearchStatus.COMPLETED
    assert outcome.research_result is store.persisted_result
    assert outcome.memory_manifest == store.persisted_manifest
    assert outcome.report_markdown == store.draft_report
    assert store.read_text(outcome.memory_manifest.report_path) == store.draft_report
    assert tool.calls == 1
    assert policy.red_calls == 1
    assert policy.blue_calls == (0 if case == "malformed_red" else 1)
    expected_gate = {
        "frontmatter_changed": "changed YAML frontmatter",
        "wikilink_removed": "changed WikiLink targets",
        "wikilink_added": "changed WikiLink targets",
        "invalid_wikilink": "changed WikiLink targets",
        "url_removed": "changed external URLs",
        "url_added": "changed external URLs",
    }.get(case)
    if expected_gate is not None:
        assert expected_gate in review.fallback_reason
    for path, original in store.non_report_before.items():
        assert store.read_text(path) == original


def test_revised_report_rejects_wikilink_absent_from_manifest() -> None:
    report = (
        "---\n"
        'id: "Report-test"\n'
        'type: "report"\n'
        'root_thread_id: "root"\n'
        "---\n\n"
        "[[evidence/not-in-manifest|Evidence]]\n"
    )
    manifest = MemoryManifest(
        report_path="reports/Report-test.md",
        evidence_paths=("evidence/Evidence-known.md",),
        source_paths=(),
    )

    with pytest.raises(ValueError, match="absent from the manifest"):
        validate_revised_report(report, report, manifest)


@pytest.mark.asyncio
async def test_checkpoint_reentry_does_not_repeat_review_or_create_files(tmp_path) -> None:
    saver = InMemorySaver()
    graph, store, policy, _, outcome = await _complete(
        tmp_path,
        thread_id="n6-reentry",
        enabled=True,
        red_payload={"issues": [_issue()]},
        blue_payload=_successful_blue,
        checkpointer=saver,
    )
    before = _bundle_files(tmp_path)
    calls_before = (policy.red_calls, policy.blue_calls)

    repeated = await graph.ainvoke(None, config=_config("n6-reentry"))

    repeated_outcome = repeated["workflow_result"]
    assert repeated_outcome.report_markdown == outcome.report_markdown
    assert repeated_outcome.memory_manifest.report_path == outcome.memory_manifest.report_path
    assert repeated_outcome.report_review.applied == outcome.report_review.applied
    assert (policy.red_calls, policy.blue_calls) == calls_before
    assert _bundle_files(tmp_path) == before
    assert store.read_text(outcome.memory_manifest.report_path) == outcome.report_markdown
