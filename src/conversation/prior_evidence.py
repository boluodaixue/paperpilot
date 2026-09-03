"""Convert product Memory hits into opaque Research Core prior Evidence."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable

from ..research.core import PriorEvidence, PriorEvidenceBundle


@dataclass(frozen=True)
class PriorEvidenceProjection:
    """Core-safe bundle plus product-side citation bindings."""

    bundle: PriorEvidenceBundle
    source_bindings: tuple[tuple[str, str], ...]


def memory_hits_to_prior_evidence(
    hits: Iterable[Any],
) -> PriorEvidenceProjection:
    """Remove Memory identity/path details before crossing the Core boundary."""

    items: list[PriorEvidence] = []
    bindings: list[tuple[str, str]] = []
    seen_paths: set[str] = set()
    for hit in hits:
        path = str(getattr(hit, "relative_path", "") or "").strip()
        summary = str(getattr(hit, "summary", "") or "").strip()
        title = str(getattr(hit, "title", "") or "").strip()
        if not path or not summary or path in seen_paths:
            continue
        seen_paths.add(path)
        digest = hashlib.sha256(
            f"{path}\n{summary}".encode("utf-8")
        ).hexdigest()[:16]
        evidence_id = f"evidence-prior-{digest}"
        items.append(PriorEvidence(
            evidence_id=evidence_id,
            finding=summary,
            source_ref=f"prior://{digest}",
            title=title,
            source_type="prior_knowledge",
            provenance="selected_memory",
        ))
        bindings.append((evidence_id, path))
    return PriorEvidenceProjection(
        bundle=PriorEvidenceBundle(tuple(items)),
        source_bindings=tuple(bindings),
    )


__all__ = ["PriorEvidenceProjection", "memory_hits_to_prior_evidence"]
