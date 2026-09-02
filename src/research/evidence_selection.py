"""Deterministic evidence selection for bounded synthesis and report rendering."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
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
    "icmagroup.org",
    "ifc.org",
    "nafmii.org.cn",
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
    "wikipedia.org",
)
_LOW_SIGNAL_HOST_TOKENS = ("forum.", "forums.", "discuss.", "answers.")


def _host(source_ref: str) -> str:
    return (urlparse(source_ref).hostname or "").lower()


def _host_matches(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def _context_terms(value: str) -> set[str]:
    terms = {
        token.casefold()
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9.+-]{2,}", value)
    }
    for run in re.findall(r"[\u3400-\u9fff]{2,}", value):
        terms.update(run[index:index + 2] for index in range(len(run) - 1))
    return terms


def _quality_score(item: EvidenceItem, requirement: str = "") -> int:
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
    if any(token in host for token in _LOW_SIGNAL_HOST_TOKENS):
        score -= 4
    if item.source_type.lower() in {"paper", "official", "dataset"}:
        score += 3
    if item.locator and item.locator != item.source_ref:
        score += 1
    if item.excerpt:
        score += 1
    if requirement:
        evidence_terms = _context_terms(
            " ".join((item.title, item.finding, item.excerpt[:1200]))
        )
        score += min(6, len(evidence_terms & _context_terms(requirement)))
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
    requirement_descriptions: Mapping[str, str] | None = None,
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
    ranked_ids: dict[str, dict[str, int]] = {}
    for item in coverage:
        ranked_ids[item.requirement_id] = {
            evidence_id: index
            for index, evidence_id in enumerate(item.evidence_ids)
            if evidence_id in unique
        }
    agent_judged_requirements = set(ranked_ids)
    cited_ids = {
        evidence_id for values in ranked_ids.values() for evidence_id in values
    }
    descriptions = dict(requirement_descriptions or {})
    requirement_order = [item.requirement_id for item in coverage]
    for item in unique.values():
        if item.requirement_id and item.requirement_id not in requirement_order:
            requirement_order.append(item.requirement_id)
    if not requirement_order:
        requirement_order = [""]

    grouped: dict[str, list[EvidenceItem]] = defaultdict(list)
    for item in unique.values():
        requirement_id = item.requirement_id or ""
        # Once an Agent has supplied an ordered shortlist for its Requirement,
        # unlisted Evidence is an explicit non-selection rather than spare
        # capacity for the deterministic selector to refill with.
        if (
            requirement_id in agent_judged_requirements
            and item.evidence_id not in ranked_ids[requirement_id]
        ):
            continue
        grouped[requirement_id].append(item)
    for requirement_id, items in grouped.items():
        items.sort(
            key=lambda item: (
                item.evidence_id in cited_ids,
                -ranked_ids.get(requirement_id, {}).get(item.evidence_id, 10**9),
                _quality_score(item, descriptions.get(requirement_id, "")),
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
        (item for items in grouped.values() for item in items),
        key=lambda item: (
            item.evidence_id in cited_ids,
            -ranked_ids.get(item.requirement_id or "", {}).get(
                item.evidence_id, 10**9
            ),
            _quality_score(
                item, descriptions.get(item.requirement_id or "", "")
            ),
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
