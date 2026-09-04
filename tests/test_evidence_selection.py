"""Representative evidence selection stays bounded, diverse, and deterministic."""

from src.research.evidence_selection import select_representative_evidence
from src.research.models import EvidenceItem, RequirementCoverage, RequirementStatus


def _evidence(evidence_id: str, requirement_id: str, url: str) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        finding=f"Finding {evidence_id}",
        source_type="paper" if "arxiv.org" in url else "web",
        title=f"Source {evidence_id}",
        source_ref=url,
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
