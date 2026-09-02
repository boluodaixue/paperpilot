"""Deterministic structured sufficiency responses shared by offline test policies."""

from __future__ import annotations

import json
from typing import Any


def assessment_response(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    content = str(messages[-1].get("content") or "")
    if not content.startswith("ASSESS_RESEARCH_STATE"):
        return None
    state = json.loads(content.split("STATE:\n", 1)[1])
    require_evidence = bool(state.get("require_evidence", True))
    coverage = []
    for requirement in state["requirements"]:
        requirement_id = requirement["requirement_id"]
        evidence_ids = [
            item["evidence_id"]
            for item in state["evidence"]
            if not item.get("requirement_id") or item.get("requirement_id") == requirement_id
        ]
        supported = bool(evidence_ids) or not require_evidence
        coverage.append(
            {
                "requirement_id": requirement_id,
                "status": "supported" if supported else "unsupported",
                "evidence_ids": evidence_ids if supported else [],
                "rationale": (
                    "Offline fixture evidence supports the scoped requirement."
                    if supported
                    else "No source-locatable evidence was available."
                ),
                "remaining_gap": None if supported else "Evidence is unavailable.",
            }
        )
    all_supported = all(item["status"] == "supported" for item in coverage)
    gaps = (
        []
        if all_supported
        else [
            {
                "requirement_id": requirement["requirement_id"],
                "reason": "The required evidence path failed.",
                "impact": "high",
            }
            for requirement, item in zip(state["requirements"], coverage)
            if item["status"] != "supported"
        ]
    )
    payload = {
        "decision": "stop_research",
        "coverage": coverage,
        "critical_gaps": gaps,
        "next_actions": [],
        "termination_reason": "coverage_complete" if all_supported else "tool_failure",
        "replan_reason": None,
        "exhaustion_reason": None,
    }
    return {"content": json.dumps(payload), "tool_calls": []}
