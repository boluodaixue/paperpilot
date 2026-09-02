"""Loss-aware working-context reductions for the Research AgentGraph."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any


class ContextCompactionError(ValueError):
    """A semantic compaction response failed the lossless ID contract."""


def snip_consumed_tool_artifacts(
    messages: Iterable[Mapping[str, Any]],
    *,
    consumed_artifact_ids: set[str],
) -> list[dict[str, Any]]:
    """L2: remove previews only after a verified artifact was assessed."""
    compacted: list[dict[str, Any]] = []
    for raw in messages:
        message = dict(raw)
        if message.get("role") != "tool":
            compacted.append(message)
            continue
        try:
            payload = json.loads(str(message.get("content") or ""))
        except (TypeError, json.JSONDecodeError):
            compacted.append(message)
            continue
        if not isinstance(payload, dict):
            compacted.append(message)
            continue
        artifact_id = str(payload.get("artifact_id") or "")
        if (
            payload.get("status") == "offloaded"
            and artifact_id in consumed_artifact_ids
            and isinstance(payload.get("artifact_path"), str)
            and isinstance(payload.get("content_hash"), str)
        ):
            message["content"] = json.dumps(
                {
                    "status": "offloaded_consumed",
                    "artifact_id": artifact_id,
                    "artifact_path": payload["artifact_path"],
                    "content_hash": payload["content_hash"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        compacted.append(message)
    return compacted


def microcompact_control_messages(
    messages: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """L3: discard only control projections superseded by a newer one."""
    copied = [dict(message) for message in messages]
    control_indexes = [
        index
        for index, message in enumerate(copied)
        if message.get("role") == "user" and str(message.get("content") or "").startswith("RESEARCH_STATE_DECISION\n")
    ]
    if len(control_indexes) <= 1:
        return copied
    remove = set(control_indexes[:-1])
    return [message for index, message in enumerate(copied) if index not in remove]


def _verified_artifact_payload(message: Mapping[str, Any]) -> dict[str, Any] | None:
    if message.get("role") != "tool":
        return None
    try:
        payload = json.loads(str(message.get("content") or ""))
    except (TypeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("status") != "offloaded_consumed"
        or not isinstance(payload.get("artifact_id"), str)
        or not isinstance(payload.get("artifact_path"), str)
        or not isinstance(payload.get("content_hash"), str)
    ):
        return None
    return payload


def collapse_verified_working_context(
    messages: Iterable[Mapping[str, Any]],
    *,
    state_projection: Mapping[str, Any],
    max_chars: int = 32000,
    keep_recent: int = 8,
) -> list[dict[str, Any]]:
    """L4: collapse an old recoverable prefix into a deterministic manifest."""
    copied = [dict(message) for message in messages]
    size = sum(len(str(message.get("content") or "")) for message in copied)
    if size <= max_chars or len(copied) <= 3 + keep_recent:
        return copied
    end = len(copied) - keep_recent
    if end <= 2:
        return copied
    if copied[end].get("role") == "tool":
        end -= 1
    for index in range(2, end):
        if copied[index].get("role") != "tool":
            continue
        if _verified_artifact_payload(copied[index]) is None:
            preceding = index - 1
            if preceding >= 2 and copied[preceding].get("role") == "assistant":
                end = min(end, preceding)
            else:
                end = min(end, index)
            break
    if end <= 2:
        return copied
    collapsed = copied[2:end]
    artifact_ids: list[str] = []
    for message in collapsed:
        payload = _verified_artifact_payload(message)
        if payload is not None:
            artifact_ids.append(str(payload["artifact_id"]))
            continue
        content = str(message.get("content") or "")
        if content.startswith("RESEARCH_CONTEXT_COLLAPSE\n"):
            try:
                previous = json.loads(content.split("\n", 1)[1])
            except (IndexError, TypeError, json.JSONDecodeError):
                previous = {}
            if isinstance(previous, dict):
                artifact_ids.extend(str(item) for item in previous.get("artifact_ids", []) if isinstance(item, str))
    encoded = json.dumps(
        collapsed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    manifest = {
        "version": 1,
        "collapsed_message_count": len(collapsed),
        "collapsed_sha256": hashlib.sha256(encoded).hexdigest(),
        "artifact_ids": list(dict.fromkeys(artifact_ids)),
        "research_state": dict(state_projection),
    }
    marker = {
        "role": "user",
        "content": "RESEARCH_CONTEXT_COLLAPSE\n"
        + json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ),
    }
    return [*copied[:2], marker, *copied[end:]]


def _manifest_ids(messages: Iterable[Mapping[str, Any]]) -> tuple[set[str], set[str], set[str]]:
    requirements: set[str] = set()
    evidence: set[str] = set()
    artifacts: set[str] = set()
    for message in messages:
        payload = _verified_artifact_payload(message)
        if payload is not None:
            artifacts.add(str(payload["artifact_id"]))
        content = str(message.get("content") or "")
        if not content.startswith(("RESEARCH_CONTEXT_COLLAPSE\n", "RESEARCH_SEMANTIC_COMPACTION\n")):
            continue
        try:
            manifest = json.loads(content.split("\n", 1)[1])
        except (IndexError, TypeError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, dict):
            continue
        requirements.update(str(item) for item in manifest.get("requirement_ids", []) if isinstance(item, str))
        evidence.update(str(item) for item in manifest.get("evidence_ids", []) if isinstance(item, str))
        artifacts.update(str(item) for item in manifest.get("artifact_ids", []) if isinstance(item, str))
        state = manifest.get("research_state", {})
        if isinstance(state, dict):
            for item in state.get("evidence", []):
                if not isinstance(item, dict):
                    continue
                if isinstance(item.get("requirement_id"), str):
                    requirements.add(item["requirement_id"])
                if isinstance(item.get("evidence_id"), str):
                    evidence.add(item["evidence_id"])
                if isinstance(item.get("artifact_id"), str) and item["artifact_id"]:
                    artifacts.add(item["artifact_id"])
    return requirements, evidence, artifacts


def semantic_compaction_messages(
    messages: Iterable[Mapping[str, Any]],
    *,
    keep_recent: int = 4,
) -> list[dict[str, str]]:
    """Build the same-policy L5 request without enabling any tools."""
    copied = [dict(message) for message in messages]
    end = len(copied) - keep_recent
    if end <= 2:
        raise ContextCompactionError("no semantic compaction segment is available")
    segment = copied[2:end]
    if any(message.get("role") == "tool" and _verified_artifact_payload(message) is None for message in segment):
        raise ContextCompactionError("semantic compaction cannot replace raw tool payloads")
    requirements, evidence, artifacts = _manifest_ids(segment)
    payload = {
        "messages": segment,
        "required_requirement_ids": sorted(requirements),
        "required_evidence_ids": sorted(evidence),
        "required_artifact_ids": sorted(artifacts),
    }
    return [
        {
            "role": "system",
            "content": (
                "Compact Research Agent working context only. Do not add claims. "
                "Return one JSON object with summary, requirement_ids, evidence_ids, "
                "and artifact_ids; every required ID must be preserved verbatim."
            ),
        },
        {
            "role": "user",
            "content": "AUTO_COMPACT_WORKING_CONTEXT\n" + json.dumps(payload, ensure_ascii=False, default=str),
        },
    ]


def apply_semantic_compaction(
    messages: Iterable[Mapping[str, Any]],
    response_content: str,
    *,
    keep_recent: int = 4,
) -> list[dict[str, Any]]:
    """L5: validate ID preservation before replacing a safe old segment."""
    copied = [dict(message) for message in messages]
    end = len(copied) - keep_recent
    if end <= 2:
        raise ContextCompactionError("no semantic compaction segment is available")
    segment = copied[2:end]
    if any(message.get("role") == "tool" and _verified_artifact_payload(message) is None for message in segment):
        raise ContextCompactionError("semantic compaction cannot replace raw tool payloads")
    required_requirements, required_evidence, required_artifacts = _manifest_ids(segment)
    try:
        payload = json.loads(response_content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ContextCompactionError("semantic compaction must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ContextCompactionError("semantic compaction must be a JSON object")
    summary = str(payload.get("summary") or "").strip()
    if not summary or len(summary) > 8000:
        raise ContextCompactionError("semantic compaction summary is invalid")

    def _ids(name: str) -> set[str]:
        value = payload.get(name)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ContextCompactionError(f"semantic compaction {name} is invalid")
        return set(value)

    returned_requirements = _ids("requirement_ids")
    returned_evidence = _ids("evidence_ids")
    returned_artifacts = _ids("artifact_ids")
    if (
        not required_requirements.issubset(returned_requirements)
        or not required_evidence.issubset(returned_evidence)
        or not required_artifacts.issubset(returned_artifacts)
    ):
        raise ContextCompactionError("semantic compaction did not preserve every required ID")
    encoded = json.dumps(
        segment,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    marker = {
        "role": "user",
        "content": "RESEARCH_SEMANTIC_COMPACTION\n"
        + json.dumps(
            {
                "version": 1,
                "collapsed_message_count": len(segment),
                "collapsed_sha256": hashlib.sha256(encoded).hexdigest(),
                "summary": summary,
                "requirement_ids": sorted(returned_requirements),
                "evidence_ids": sorted(returned_evidence),
                "artifact_ids": sorted(returned_artifacts),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    return [*copied[:2], marker, *copied[end:]]


__all__ = [
    "ContextCompactionError",
    "apply_semantic_compaction",
    "collapse_verified_working_context",
    "microcompact_control_messages",
    "semantic_compaction_messages",
    "snip_consumed_tool_artifacts",
]
