"""Phase 6 tests for citation-approved V2 bundle persistence."""

from __future__ import annotations

import pytest

from src.research.memory import MarkdownMemoryStore
from src.research.models import ExecutionIdentity, ResearchBrief, ResearchResult, ResearchStatus
from tests._v2_workflow_fakes import package


def test_v2_persistence_resolves_internal_ids_before_atomic_report_write(tmp_path) -> None:
    _, supervisor, _, draft = package()
    evidence = supervisor.worker_results[0].evidence
    result = ResearchResult("v2", ResearchStatus.COMPLETED, "supported", evidence=evidence)
    brief = ResearchBrief("Question", "Objective", (), ("Verify",), (), "Report")
    store = MarkdownMemoryStore(tmp_path)

    report, manifest = store.persist_research(
        brief, result, ExecutionIdentity("root-v2-persist", None, "root-v2-persist", 0),
        report_body_markdown=draft.markdown,
    )

    assert "architecture: \"supervisor_v2\"" in report
    assert "[[EVIDENCE:" not in report
    assert "## References" in report
    assert store.read_text(manifest.report_path) == report


def test_unknown_internal_id_fails_before_report_is_written(tmp_path) -> None:
    _, supervisor, _, _ = package()
    result = ResearchResult(
        "v2", ResearchStatus.COMPLETED, "supported",
        evidence=supervisor.worker_results[0].evidence,
    )
    store = MarkdownMemoryStore(tmp_path)
    identity = ExecutionIdentity("root-v2-invalid", None, "root-v2-invalid", 0)
    brief = ResearchBrief("Question", "Objective", (), ("Verify",), (), "Report")

    with pytest.raises(ValueError, match="unknown or unpersisted Evidence"):
        store.persist_research(
            brief, result, identity,
            report_body_markdown="Forged [[EVIDENCE:evidence-unknown]]",
        )
    assert not (tmp_path / "reports").exists()
