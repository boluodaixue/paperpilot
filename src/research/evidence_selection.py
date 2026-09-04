"""Deterministic evidence selection for bounded synthesis and report rendering."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from urllib.parse import urlparse

from .models import EvidenceItem, RequirementCoverage


_PRIMARY_HOST_SUFFIXES = (
    "openai.com",
    "anthropic.com",
    "deepmind.google",
    "ai.google.dev",
    "blog.google",
    "qwenlm.github.io",
    "arxiv.org",
    "aclanthology.org",
    "swebench.com",
    "longbench2.github.io",
)
_SECONDARY_HOST_SUFFIXES = (
    "github.com",
    "huggingface.co",
)
_LOW_SIGNAL_HOST_SUFFIXES = (
    "medium.com",
    "substack.com",
    "llm-stats.com",
    "benchlm.ai",
)


def _host(source_ref: str) -> str:
    return (urlparse(source_ref).hostname or "").lower()


def _host_matches(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def _quality_score(item: EvidenceItem) -> int:
    host = _host(item.source_ref)
    score = 0
    if _host_matches(host, _PRIMARY_HOST_SUFFIXES):
        score += 6
    elif _host_matches(host, _SECONDARY_HOST_SUFFIXES):
        score += 3
    if _host_matches(host, _LOW_SIGNAL_HOST_SUFFIXES):
        score -= 4
    if item.source_type.lower() in {"paper", "official", "dataset"}:
        score += 3
    if item.locator and item.locator != item.source_ref:
        score += 1
    if item.excerpt:
        score += 1
    return score


def select_representative_evidence(
    evidence: Iterable[EvidenceItem],
    coverage: Iterable[RequirementCoverage] = (),
    *,
    limit: int = 32,
) -> tuple[EvidenceItem, ...]:
    """Select a source-diverse, requirement-balanced evidence subset.

    Coverage-cited evidence is preferred, but primary/official sources and
    unique source references win within each requirement. The full evidence
    inventory remains checkpointed and persisted separately.
    """
    if limit <= 0:
        return ()
    unique = {item.evidence_id: item for item in evidence}
    if len(unique) <= limit:
        return tuple(unique.values())

    coverage = tuple(coverage)
    cited_ids = {
        evidence_id
        for item in coverage
        for evidence_id in item.evidence_ids
    }
    requirement_order = [item.requirement_id for item in coverage]
    for item in unique.values():
        if item.requirement_id and item.requirement_id not in requirement_order:
            requirement_order.append(item.requirement_id)
    if not requirement_order:
        requirement_order = [""]

    grouped: dict[str, list[EvidenceItem]] = defaultdict(list)
    for item in unique.values():
        grouped[item.requirement_id or ""].append(item)
    for items in grouped.values():
        items.sort(
            key=lambda item: (
                item.evidence_id in cited_ids,
                _quality_score(item),
                bool(item.requirement_id),
                item.evidence_id,
            ),
            reverse=True,
        )

    selected: list[EvidenceItem] = []
    selected_ids: set[str] = set()
    selected_sources: set[str] = set()
    per_requirement = max(1, limit // max(1, len(requirement_order)))

    def add(item: EvidenceItem) -> None:
        selected.append(item)
        selected_ids.add(item.evidence_id)
        selected_sources.add(item.source_ref)

    # First pass guarantees requirement breadth and source diversity.
    for requirement_id in requirement_order:
        added = 0
        for item in grouped.get(requirement_id, []):
            if item.evidence_id in selected_ids or item.source_ref in selected_sources:
                continue
            add(item)
            added += 1
            if len(selected) >= limit or added >= per_requirement:
                break
        if len(selected) >= limit:
            return tuple(selected)

    # Fill unused capacity by global quality while retaining one item per source.
    ranked = sorted(
        unique.values(),
        key=lambda item: (
            item.evidence_id in cited_ids,
            _quality_score(item),
            bool(item.requirement_id),
            item.evidence_id,
        ),
        reverse=True,
    )
    for item in ranked:
        if item.evidence_id in selected_ids or item.source_ref in selected_sources:
            continue
        add(item)
        if len(selected) >= limit:
            return tuple(selected)

    # Only duplicate a source when the inventory has fewer distinct sources.
    for item in ranked:
        if item.evidence_id in selected_ids:
            continue
        add(item)
        if len(selected) >= limit:
            break
    return tuple(selected)
