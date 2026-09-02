"""Representative evidence selection stays bounded, diverse, and deterministic."""

from collections import Counter

from src.research.evidence_selection import select_representative_evidence
from src.research.models import (
    EvidenceItem,
    RequirementCoverage,
    RequirementStatus,
    ResearchBrief,
    ResearchResult,
    ResearchStatus,
)
from src.research.rendering import render_report


def _evidence(evidence_id: str, requirement_id: str, url: str) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        finding=f"Finding {evidence_id}",
        source_type="paper" if "arxiv.org" in url else "web",
        title=f"Source {evidence_id}",
        source_ref=url,
        locator=f"{url}#{evidence_id}",
        excerpt="Grounded excerpt",
        requirement_id=requirement_id,
    )


def test_selection_balances_requirements_and_prefers_primary_sources() -> None:
    evidence = (
        _evidence("R1-low", "R1", "https://medium.com/example/r1"),
        _evidence("R1-primary", "R1", "https://arxiv.org/abs/1"),
        _evidence("R2-low", "R2", "https://benchlm.ai/r2"),
        _evidence("R2-primary", "R2", "https://www.anthropic.com/research/r2"),
        _evidence("R3-primary", "R3", "https://qwenlm.github.io/blog/r3"),
    )
    coverage = (
        RequirementCoverage("R1", RequirementStatus.WEAK, ("R1-primary",)),
        RequirementCoverage("R2", RequirementStatus.WEAK, ("R2-primary",)),
        RequirementCoverage("R3", RequirementStatus.WEAK, ("R3-primary",)),
    )

    selected = select_representative_evidence(evidence, coverage, limit=3)

    assert [item.evidence_id for item in selected] == [
        "R1-primary",
        "R2-primary",
        "R3-primary",
    ]


def test_selection_respects_agent_ranked_shortlist_without_refilling() -> None:
    evidence = (
        _evidence("agent-first", "R1", "https://example.com/direct"),
        _evidence("static-primary", "R1", "https://regulator.gov/report"),
        _evidence("agent-second", "R1", "https://example.org/support"),
    )
    coverage = (
        RequirementCoverage(
            "R1",
            RequirementStatus.WEAK,
            ("agent-first", "agent-second"),
            rationale="Agent-ranked directly useful evidence.",
        ),
    )

    selected = select_representative_evidence(evidence, coverage, limit=3)

    assert [item.evidence_id for item in selected] == [
        "agent-first",
        "agent-second",
    ]


def test_empty_agent_shortlist_is_not_refilled_by_static_ranking() -> None:
    evidence = (
        _evidence("unselected", "R1", "https://regulator.gov/report"),
    )
    coverage = (
        RequirementCoverage(
            "R1",
            RequirementStatus.UNSUPPORTED,
            (),
            rationale="Agent found no directly useful evidence.",
        ),
    )

    assert select_representative_evidence(evidence, coverage, limit=3) == ()


def test_selection_uses_one_item_per_source_before_duplicates() -> None:
    evidence = (
        _evidence("same-a", "R1", "https://arxiv.org/abs/shared"),
        _evidence("same-b", "R1", "https://arxiv.org/abs/shared"),
        _evidence("other", "R1", "https://www.anthropic.com/research/other"),
    )

    selected = select_representative_evidence(evidence, limit=2)

    assert len({item.source_ref for item in selected}) == 2


def test_selection_is_bounded_and_deterministic() -> None:
    evidence = tuple(
        _evidence(f"E{index}", f"R{index % 4 + 1}", f"https://arxiv.org/abs/{index}")
        for index in range(80)
    )

    first = select_representative_evidence(evidence, limit=24)
    second = select_representative_evidence(evidence, limit=24)

    assert len(first) == 24
    assert first == second
    assert {item.requirement_id for item in first} == {"R1", "R2", "R3", "R4"}


def test_selection_enforces_requirement_and_source_caps() -> None:
    evidence = tuple(
        _evidence(
            f"R{requirement}-{index}",
            f"R{requirement}",
            f"https://authority{index % 12}.gov/report",
        )
        for requirement in range(1, 5)
        for index in range(12)
    )

    selected = select_representative_evidence(
        evidence,
        limit=24,
        max_per_requirement=6,
        max_per_source=2,
    )

    requirement_counts = Counter(item.requirement_id for item in selected)
    source_counts = Counter(item.source_ref for item in selected)
    assert len(selected) == 24
    assert max(requirement_counts.values()) == 6
    assert max(source_counts.values()) <= 2


def test_selection_generically_prefers_government_source() -> None:
    evidence = (
        _evidence("secondary", "R1", "https://medium.com/example/summary"),
        _evidence("official", "R1", "https://regulator.gov.cn/rules/standard"),
    )

    selected = select_representative_evidence(evidence, limit=1)

    assert [item.evidence_id for item in selected] == ["official"]


def test_dynamic_primary_source_cap_requires_distinct_locators() -> None:
    evidence = tuple(
        EvidenceItem(
            evidence_id=f"official-{index}",
            finding=f"Official clause {index}",
            source_type="official",
            title="Official standard",
            source_ref="https://regulator.gov/standard",
            locator=("page:1" if index == 4 else f"page:{index + 1}"),
            excerpt=f"Clause {index}",
            requirement_id="R1",
        )
        for index in range(5)
    )

    selected = select_representative_evidence(
        evidence,
        limit=12,
        max_per_requirement=6,
        max_per_source=2,
        max_per_primary_source=4,
    )

    assert len(selected) == 4
    assert len({item.locator for item in selected}) == 4


def test_legacy_report_bounds_display_but_keeps_full_inventory() -> None:
    evidence = tuple(
        EvidenceItem(
            evidence_id=f"E{index}",
            finding=f"Finding {index}: " + ("x" * 900),
            source_type="official",
            title=f"Official source {index}",
            source_ref=f"https://regulator{index}.gov/rule",
            locator=f"section:{index}",
            excerpt="Grounded excerpt",
            requirement_id=f"R{index % 5 + 1}",
        )
        for index in range(30)
    )
    result = ResearchResult(
        task_id="legacy-bounded-report",
        status=ResearchStatus.COMPLETED,
        summary="Bounded report.",
        evidence=evidence,
    )

    report = render_report(
        ResearchBrief(
            question="Bound the legacy evidence view",
            objective="Keep full evidence while rendering a concise report.",
            scope=(),
            directions=(),
            constraints=(),
            expected_output="A concise evidence-backed report.",
        ),
        result,
        report_note="Report-bounded",
        evidence_notes={item.evidence_id: item.evidence_id for item in evidence},
        root_thread_id="root-bounded",
    )

    details = report.split("## Evidence-backed Details", 1)[1].split("## Unresolved", 1)[0]
    assert details.count("|Evidence]]") == 24
    assert "Showing 24 of 30 collected evidence items" in details
    assert "x" * 600 not in details
    assert len(result.evidence) == 30
