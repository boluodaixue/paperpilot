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
_INSTITUTIONAL_HOST_SUFFIXES = (
    "gov",
    "gov.cn",
    "edu",
    "edu.cn",
    "ac.uk",
    "int",
)
_ORGANIZATION_HOST_SUFFIXES = (
    "org",
    "org.cn",
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
    if _host_matches(host, _INSTITUTIONAL_HOST_SUFFIXES):
        score += 7
    elif _host_matches(host, _PRIMARY_HOST_SUFFIXES):
        score += 6
    elif _host_matches(host, _SECONDARY_HOST_SUFFIXES):
        score += 3
    elif _host_matches(host, _ORGANIZATION_HOST_SUFFIXES):
        score += 1
    if _host_matches(host, _LOW_SIGNAL_HOST_SUFFIXES):
        score -= 4
    if item.source_type.lower() in {"paper", "official", "dataset"}:
        score += 3
    if item.locator and item.locator != item.source_ref:
        score += 1
    if item.excerpt:
        score += 1
    return score


def _is_primary_evidence(item: EvidenceItem) -> bool:
    host = _host(item.source_ref)
    return (
        _host_matches(host, _INSTITUTIONAL_HOST_SUFFIXES)
        or _host_matches(host, _PRIMARY_HOST_SUFFIXES)
        or item.source_type.lower() in {"official", "paper", "dataset"}
    )


def select_representative_evidence(
    evidence: Iterable[EvidenceItem],
    coverage: Iterable[RequirementCoverage] = (),
    *,
    limit: int = 24,
    max_per_requirement: int = 6,
    max_per_source: int = 2,
    max_per_primary_source: int | None = None,
) -> tuple[EvidenceItem, ...]:
    """Select a source-diverse, requirement-balanced evidence subset.

    Coverage-cited evidence is preferred, but primary/official sources and
    unique source references win within each requirement. The full evidence
    inventory remains checkpointed and persisted separately.
    """
    primary_source_limit = (
        max_per_source
        if max_per_primary_source is None
        else int(max_per_primary_source)
    )
    if (
        limit <= 0
        or max_per_requirement <= 0
        or max_per_source <= 0
        or primary_source_limit <= 0
    ):
        return ()
    unique = {item.evidence_id: item for item in evidence}

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
    source_counts: dict[str, int] = defaultdict(int)
    source_locators: dict[str, set[str]] = defaultdict(set)
    requirement_counts: dict[str, int] = defaultdict(int)

    def add(item: EvidenceItem) -> None:
        selected.append(item)
        selected_ids.add(item.evidence_id)
        source_counts[item.source_ref] += 1
        source_locators[item.source_ref].add(item.locator or item.source_ref)
        requirement_counts[item.requirement_id or ""] += 1

    # Round-robin distinct sources first so one evidence-rich requirement does
    # not crowd out the rest of the final synthesis inventory.
    while len(selected) < limit:
        progress = False
        for requirement_id in requirement_order:
            if requirement_counts[requirement_id] >= max_per_requirement:
                continue
            for item in grouped.get(requirement_id, []):
                if item.evidence_id in selected_ids or source_counts[item.source_ref] > 0:
                    continue
                add(item)
                progress = True
                break
            if len(selected) >= limit:
                return tuple(selected)
        if not progress:
            break

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
        requirement_id = item.requirement_id or ""
        if item.evidence_id in selected_ids:
            continue
        if requirement_counts[requirement_id] >= max_per_requirement:
            continue
        source_limit = (
            primary_source_limit if _is_primary_evidence(item) else max_per_source
        )
        if source_counts[item.source_ref] >= source_limit:
            continue
        if (item.locator or item.source_ref) in source_locators[item.source_ref]:
            continue
        add(item)
        if len(selected) >= limit:
            break
    return tuple(selected)
