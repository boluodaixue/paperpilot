"""Acceptance tests for the persistence-neutral Headless Research Core."""
from __future__ import annotations

import ast
from dataclasses import fields
from pathlib import Path

import pytest

import src.research.core as core_module
from src.research.agent_graph import create_research_agent_state
from src.research.core import (
    CoreResearchRequest,
    PriorEvidence,
    PriorEvidenceBundle,
    run_core_research,
)
from src.research.models import (
    AgentLimits,
    EvidenceItem,
    ExecutionIdentity,
    ResearchResult,
    ResearchStatus,
    ResearchTask,
)


ROOT = Path(__file__).resolve().parents[1]


def test_core_contract_has_no_product_identity_fields() -> None:
    names = {item.name for item in fields(CoreResearchRequest)}
    assert "memory_id" not in names
    assert "session_id" not in names
    assert "vault_root" not in names
    assert "obsidian_uri" not in names


def test_core_module_does_not_import_product_modules() -> None:
    source = (ROOT / "src/research/core.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    banned = ("memory", "obsidian", "workflow", "web.server", "runtime")
    assert not {
        module
        for module in imported
        if any(part in module.split(".") for part in banned)
    }


def test_initial_evidence_enters_agent_state_without_a_memory_contract() -> None:
    evidence = EvidenceItem(
        evidence_id="evidence-prior-1",
        finding="Prior grounded finding.",
        source_type="prior",
        title="Prior source",
        source_ref="https://example.com/prior",
    )
    identity = ExecutionIdentity("core-state", None, "core-state", 0)
    state = create_research_agent_state(
        ResearchTask("core-task", "Use prior evidence", require_evidence=True),
        identity,
        AgentLimits(),
        initial_evidence=(evidence,),
    )

    assert state["observed_evidence"] == [evidence]


@pytest.mark.asyncio
async def test_headless_core_builds_a_generic_task_and_returns_without_persistence(
    monkeypatch,
) -> None:
    captured = {}

    async def fake_run(task, policy, tools, **kwargs):
        evidence = tuple(kwargs["initial_evidence"])
        kwargs["initial_evidence"] = evidence
        captured["task"] = task
        captured["tools"] = tuple(tools)
        captured["kwargs"] = kwargs
        return ResearchResult(
            task_id=task.task_id,
            status=ResearchStatus.PARTIAL,
            summary="Core summary",
            evidence=evidence,
            report_markdown="# Core report\n\nGrounded result.",
        )

    monkeypatch.setattr(core_module, "run_research_agent", fake_run)
    prior = PriorEvidence(
        evidence_id="evidence-prior-2",
        finding="A supplied finding.",
        source_ref="https://example.com/source",
        requirement_id="R1",
        provenance="adapter",
    )
    request = CoreResearchRequest(
        objective="Compare two architectures",
        scope=("architecture A", "architecture B"),
        directions=("performance",),
        constraints=("cite sources",),
        expected_output="A comparison report",
        prior_evidence=PriorEvidenceBundle((prior,)),
        run_id="core-contract-test",
    )

    result = await run_core_research(request, policy=object(), tools=("tool",))

    task = captured["task"]
    assert task.objective == request.objective
    assert task.context["scope"] == ["architecture A", "architecture B"]
    assert task.context["directions"] == ["performance"]
    assert "memory_id" not in task.context
    assert captured["kwargs"]["initial_evidence"][0].evidence_id == prior.evidence_id
    assert result.run_id == "core-contract-test"
    assert result.report_markdown.startswith("# Core report")
    assert result.evidence[0].source_ref == prior.source_ref


def test_prior_evidence_bundle_rejects_duplicate_ids() -> None:
    item = PriorEvidence("evidence-duplicate", "Finding", "https://example.com")
    with pytest.raises(ValueError, match="duplicate"):
        PriorEvidenceBundle((item, item))
