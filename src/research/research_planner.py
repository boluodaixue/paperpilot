"""No-tool ResearchBrief-to-ResearchPlan planner for Research Agent V2.

The defensive query normalization is a clean PaperPilot implementation of the
input-shape and fallback ideas in GPT Researcher's ``query_processing.py`` at
commit ``6f998577d547b1e54ec662dac63583aa11e3b84b``. See
``THIRD_PARTY_NOTICES.md``. No upstream search or persistence code is used.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from typing import Any

from .models import ResearchBrief
from .policy import call_policy
from .research_sufficiency import atomic_requirement_descriptions
from .v2_contracts import CoreQuestion, ResearchPlan

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)
_DEFAULT_OUTLINE = ("Research findings", "Limitations", "Sources")
_MAX_DYNAMIC_QUESTIONS = 5


def _unique_strings(values: list[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = " ".join(str(value).split())
        key = clean.casefold()
        if clean and key not in seen:
            seen.add(key)
            result.append(clean)
    return tuple(result)


def normalize_sub_queries(raw: Any, original_question: str) -> tuple[str, ...]:
    """Normalize common model output shapes with a conservative fallback."""

    def collect(value: Any) -> list[str]:
        if isinstance(value, str):
            clean = value.strip()
            if not clean:
                return []
            if clean[:1] in {"[", "{"}:
                try:
                    return collect(json.loads(clean))
                except json.JSONDecodeError:
                    pass
            return [clean]
        if isinstance(value, dict):
            if "queries" in value:
                return collect(value["queries"])
            if "query" in value:
                return collect(value["query"])
            if "question" in value:
                return collect(value["question"])
            if "description" in value:
                return collect(value["description"])
            return []
        if isinstance(value, (list, tuple)):
            collected: list[str] = []
            for item in value:
                collected.extend(collect(item))
            return collected
        return []

    normalized = _unique_strings(collect(raw))
    if normalized:
        return normalized
    fallback = " ".join(str(original_question).split())
    return (fallback,) if fallback else ()


def _string_tuple(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        raw = (raw,)
    if not isinstance(raw, (list, tuple)):
        return ()
    return _unique_strings([str(item) for item in raw if str(item).strip()])


def _required_questions(brief: ResearchBrief) -> tuple[CoreQuestion, ...]:
    descriptions = atomic_requirement_descriptions(brief.directions)
    if not descriptions:
        descriptions = [brief.objective.strip() or brief.question.strip()]
        origin = "fallback"
    else:
        origin = "brief_direction"
    return tuple(
        CoreQuestion.create(
            description,
            required=True,
            priority="high",
            origin=origin,
        )
        for description in descriptions
        if description.strip()
    )


def _planner_prompt(brief: ResearchBrief) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are the PaperPilot Research Planner. Do not call tools and do "
                "not research the topic. Convert the confirmed ResearchBrief into four "
                "or five main required Core Questions. Group closely related directions "
                "into one coherent research direction; the assigned Research Agent may "
                "Fork narrower subtopics later. Do not add a separate synthesis Core "
                "Question because the Composer owns synthesis. Return one JSON object "
                "with core_questions, report_outline, source_guidance, and work_hints "
                "arrays. Do not create a Cartesian coverage matrix, assign workers, or "
                "broaden the confirmed scope."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(asdict(brief), ensure_ascii=False, sort_keys=True),
        },
    ]


def _payload_from_content(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        payload = content
    else:
        candidate = str(content or "").strip()
        fenced = _JSON_FENCE.search(candidate)
        if fenced:
            candidate = fenced.group(1).strip()
        try:
            payload = json.loads(candidate)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("planner response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("planner response must be a JSON object")
    return payload


def _plan_from_response(
    brief: ResearchBrief,
    response: dict[str, Any],
) -> ResearchPlan:
    if response.get("tool_calls"):
        raise ValueError("Research Planner cannot emit tool calls")
    payload = _payload_from_content(response.get("content"))
    if "core_questions" not in payload and "queries" not in payload:
        raise ValueError("planner response must include core_questions")

    descriptions = normalize_sub_queries(
        payload.get("core_questions", payload.get("queries")),
        brief.question,
    )[:_MAX_DYNAMIC_QUESTIONS]
    questions = tuple(
        CoreQuestion.create(
            description,
            required=True,
            priority="high",
            origin="dynamic_plan",
        )
        for description in descriptions
    )

    outline = _string_tuple(payload.get("report_outline")) or _DEFAULT_OUTLINE
    source_guidance = _unique_strings([
        f"Original question: {brief.question}",
        f"Research objective: {brief.objective}",
        *brief.constraints,
        *_string_tuple(payload.get("source_guidance")),
        "Reject sources that do not directly mention or support the assigned topic; generic title matches are noise.",
    ])
    work_hints = _string_tuple(payload.get("work_hints"))
    return ResearchPlan.create(
        brief_revision=brief.revision,
        core_questions=questions,
        report_outline=outline,
        source_guidance=source_guidance,
        work_hints=work_hints,
    )


def deterministic_plan_fallback(
    brief: ResearchBrief,
    *,
    reason: str = "planner_output_invalid_after_repair",
) -> ResearchPlan:
    """Build the stable Brief-only plan used after one failed repair."""
    return ResearchPlan.create(
        brief_revision=brief.revision,
        core_questions=_required_questions(brief),
        report_outline=_DEFAULT_OUTLINE,
        source_guidance=_unique_strings([
            f"Original question: {brief.question}",
            f"Research objective: {brief.objective}",
            *brief.constraints,
            "Reject sources that do not directly mention or support the assigned topic; generic title matches are noise.",
        ]),
        work_hints=(),
        fallback_reason=reason,
    )


async def plan_research(brief: ResearchBrief, policy: Any) -> ResearchPlan:
    """Call the same policy without tools, repair structure once, then fall back."""
    messages = _planner_prompt(brief)
    try:
        response = await call_policy(policy, messages, [])
    except Exception:
        return deterministic_plan_fallback(brief, reason="planner_policy_unavailable")
    try:
        return _plan_from_response(brief, response)
    except (TypeError, ValueError) as exc:
        repair_messages = [
            *messages,
            {
                "role": "assistant",
                "content": str(response.get("content") or "")[:12000],
            },
            {
                "role": "user",
                "content": (
                    "Repair only the JSON structure. Return exactly one object with "
                    "core_questions, report_outline, source_guidance, and work_hints "
                    f"arrays. Do not call tools. Validation error: {type(exc).__name__}: {exc}"
                ),
            },
        ]
        try:
            repaired = await call_policy(policy, repair_messages, [])
            return _plan_from_response(brief, repaired)
        except Exception:
            return deterministic_plan_fallback(brief)
