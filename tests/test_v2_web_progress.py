"""Phase 6 product projection tests for V2 status and disclosures."""

from __future__ import annotations

from dataclasses import asdict

from src.research.models import (
    MemoryManifest,
    ResearchBrief,
    ResearchResult,
    ResearchStatus,
    ResearchWorkflowResult,
)


def test_web_result_projects_v2_disclosures() -> None:
    from web.server import ResearchTask, _research_task_result

    task = ResearchTask("task", "session", "question", memory_id=None)
    workflow = ResearchWorkflowResult(
        brief=ResearchBrief("Q", "O", (), ("D",), (), "R"),
        research_result=ResearchResult("r", ResearchStatus.PARTIAL, "summary"),
        report_markdown="# Report",
        memory_manifest=MemoryManifest("reports/r.md"),
        research_architecture="supervisor_v2",
        challenges=({"category": "weak_source", "status": "accepted"},),
        citation_issues=({"category": "overclaim", "status": "repaired"},),
        supplemental_wave_count=1,
        finalization_token_reserve=18000,
    )

    result = _research_task_result(task, workflow, elapsed=1.0)

    assert result["research_architecture"] == "supervisor_v2"
    assert result["challenges"][0]["category"] == "weak_source"
    assert result["citation_issues"][0]["category"] == "overclaim"
    assert result["supplemental_wave_count"] == 1
    assert result["finalization_token_reserve"] == 18000


def test_frontend_has_human_readable_v2_stage_events() -> None:
    from pathlib import Path
    html = Path("web/static/index.html").read_text(encoding="utf-8")
    for stage in (
        "planning", "blue_research", "red_review", "supplemental",
        "drafting", "citation_audit", "persisting",
    ):
        assert f"case '{stage}'" in html
