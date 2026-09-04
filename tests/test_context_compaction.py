from __future__ import annotations

import json

import pytest

from src.research.context_compaction import (
    ContextCompactionError,
    apply_semantic_compaction,
    collapse_verified_working_context,
    context_consolidation_manifest,
    deterministic_context_cleanup,
    microcompact_control_messages,
    semantic_memo_messages,
    snip_consumed_tool_artifacts,
    validate_semantic_memo,
)


def _offloaded(artifact_id: str, preview: str) -> dict[str, str]:
    return {
        "role": "tool",
        "name": "web_search",
        "content": json.dumps(
            {
                "status": "offloaded",
                "artifact_id": artifact_id,
                "artifact_path": f"Artifacts/root/{artifact_id}.json",
                "content_hash": "a" * 64,
                "size_bytes": 20000,
                "evidence_ids": ["E1"],
                "preview": preview,
            }
        ),
    }


def test_l2_snips_only_consumed_verified_artifacts() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "task"},
        _offloaded("artifact-old", "x" * 1200),
        _offloaded("artifact-current", "y" * 1200),
        {"role": "tool", "name": "web_search", "content": "raw unverified payload"},
    ]

    compacted = snip_consumed_tool_artifacts(
        messages,
        consumed_artifact_ids={"artifact-old"},
    )

    old = json.loads(compacted[2]["content"])
    current = json.loads(compacted[3]["content"])
    assert old == {
        "status": "offloaded_consumed",
        "artifact_id": "artifact-old",
        "artifact_path": "Artifacts/root/artifact-old.json",
        "artifact_read_root": "artifact",
        "artifact_read_path": "artifact-old.json",
        "content_hash": "a" * 64,
    }
    assert current["preview"] == "y" * 1200
    assert compacted[4] == messages[4]
    assert messages[2] != compacted[2]


def test_l2_is_idempotent_and_preserves_message_metadata() -> None:
    message = {
        **_offloaded("artifact-old", "preview"),
        "tool_call_id": "call-1",
    }
    once = snip_consumed_tool_artifacts(
        [message],
        consumed_artifact_ids={"artifact-old"},
    )
    twice = snip_consumed_tool_artifacts(
        once,
        consumed_artifact_ids={"artifact-old"},
    )

    assert twice == once
    assert once[0]["tool_call_id"] == "call-1"


def test_l3_keeps_only_the_latest_superseded_control_message() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "task"},
        {"role": "user", "content": "RESEARCH_STATE_DECISION\nold"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1"}]},
        {"role": "tool", "tool_call_id": "call-1", "content": "result"},
        {"role": "user", "content": "RESEARCH_STATE_DECISION\ncurrent"},
    ]

    compacted = microcompact_control_messages(messages)

    assert [item["content"] for item in compacted if item["role"] == "user"] == [
        "task",
        "RESEARCH_STATE_DECISION\ncurrent",
    ]
    assert compacted[2]["role"] == "assistant"
    assert compacted[3]["role"] == "tool"
    assert microcompact_control_messages(compacted) == compacted


def test_l2_cleanup_combines_consumed_artifact_and_control_cleanup() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "task"},
        _offloaded("artifact-old", "preview"),
        {"role": "user", "content": "RESEARCH_STATE_DECISION\nold"},
        {"role": "user", "content": "RESEARCH_STATE_DECISION\ncurrent"},
    ]
    compacted = deterministic_context_cleanup(messages, consumed_artifact_ids={"artifact-old"})
    assert json.loads(compacted[2]["content"])["status"] == "offloaded_consumed"
    assert [
        item["content"]
        for item in compacted
        if str(item.get("content", "")).startswith("RESEARCH_STATE_DECISION")
    ] == ["RESEARCH_STATE_DECISION\ncurrent"]


def test_l3_collapses_verified_old_history_into_a_hashed_state_manifest() -> None:
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "confirmed task"},
    ]
    for index in range(10):
        artifact_id = f"artifact-{index}"
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": f"call-{index}"}],
                },
                {
                    "role": "tool",
                    "tool_call_id": f"call-{index}",
                    "content": json.dumps(
                        {
                            "status": "offloaded_consumed",
                            "artifact_id": artifact_id,
                            "artifact_path": f"Artifacts/root/{artifact_id}.json",
                            "content_hash": "a" * 64,
                        }
                    ),
                },
                {"role": "user", "content": f"RESEARCH_STATE_DECISION\n{index}"},
            ]
        )

    compacted = collapse_verified_working_context(
        messages,
        state_projection={
            "coverage": [{"requirement_id": "R1", "status": "weak"}],
            "evidence": [{"evidence_id": "E1", "artifact_id": "artifact-0"}],
            "attempts": [{"requirement_id": "R1", "outcome": "evidence_found"}],
        },
        max_chars=500,
        keep_recent=4,
    )

    assert compacted[:2] == messages[:2]
    manifest = json.loads(str(compacted[2]["content"]).split("\n", 1)[1])
    assert manifest["version"] == 1
    assert manifest["collapsed_message_count"] > 0
    assert len(manifest["collapsed_sha256"]) == 64
    assert "artifact-0" in manifest["artifact_ids"]
    assert manifest["research_state"]["coverage"][0]["requirement_id"] == "R1"
    assert len(compacted) < len(messages)
    assert (
        collapse_verified_working_context(
            compacted,
            state_projection={},
            max_chars=500,
            keep_recent=4,
        )
        == compacted
    )


def test_l3_does_not_drop_an_unverified_raw_tool_payload() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "raw"}]},
        {"role": "tool", "tool_call_id": "raw", "content": "UNVERIFIED-RAW"},
        *({"role": "user", "content": "later " + ("x" * 100)} for _ in range(8)),
    ]

    compacted = collapse_verified_working_context(
        messages,
        state_projection={},
        max_chars=200,
        keep_recent=2,
    )

    assert any("UNVERIFIED-RAW" in str(item.get("content")) for item in compacted)


def test_l3_state_projection_and_semantic_memo_are_consolidated_together() -> None:
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "task"},
    ]
    for index in range(8):
        messages.extend(
            [
                {"role": "assistant", "content": "reasoning " + ("x" * 80)},
                {
                    "role": "tool",
                    "content": json.dumps(
                        {
                            "status": "offloaded_consumed",
                            "artifact_id": f"artifact-{index}",
                            "artifact_path": f"Artifacts/root/artifact-{index}.json",
                            "content_hash": "a" * 64,
                        }
                    ),
                },
            ]
        )
    projection = {
        "coverage": [{"requirement_id": "R1", "status": "weak"}],
        "evidence": [{"requirement_id": "R1", "evidence_id": "E1", "artifact_id": "artifact-0"}],
    }
    request = semantic_memo_messages(messages, state_projection=projection, max_chars=500, keep_recent=2)
    memo = validate_semantic_memo(
        json.dumps(
            {
                "summary": "E1 supports R1 weakly; comparison remains unresolved.",
                "requirement_ids": ["R1"],
                "evidence_ids": ["E1"],
                "artifact_ids": [f"artifact-{index}" for index in range(7)],
            }
        ),
        request_messages=request,
    )
    compacted = collapse_verified_working_context(
        messages,
        state_projection=projection,
        semantic_memo=memo,
        max_chars=500,
        keep_recent=2,
    )
    manifest = context_consolidation_manifest(compacted)
    assert manifest is not None
    assert manifest["layer"] == "L3"
    assert manifest["research_state"] == projection
    assert manifest["semantic_memo"]["evidence_ids"] == ["E1"]
    assert manifest["manifest_id"].startswith("compression-")


def test_legacy_semantic_helper_requires_lossless_id_acknowledgement() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "task"},
        {
            "role": "user",
            "content": "RESEARCH_CONTEXT_COLLAPSE\n"
            + json.dumps(
                {
                    "artifact_ids": ["artifact-1"],
                    "research_state": {
                        "evidence": [
                            {
                                "evidence_id": "E1",
                                "requirement_id": "R1",
                                "artifact_id": "artifact-1",
                            }
                        ]
                    },
                }
            ),
        },
        {"role": "assistant", "content": "old candidate"},
        {"role": "user", "content": "RESEARCH_STATE_DECISION\ncurrent"},
        {"role": "assistant", "content": "recent"},
    ]
    valid = json.dumps(
        {
            "summary": "R1 remains weak; E1 is the current evidence.",
            "requirement_ids": ["R1"],
            "evidence_ids": ["E1"],
            "artifact_ids": ["artifact-1"],
        }
    )

    compacted = apply_semantic_compaction(messages, valid, keep_recent=2)

    assert compacted[:2] == messages[:2]
    assert compacted[2]["content"].startswith("RESEARCH_SEMANTIC_COMPACTION\n")
    assert compacted[-2:] == messages[-2:]
    with pytest.raises(ContextCompactionError, match="preserve"):
        apply_semantic_compaction(
            messages,
            json.dumps(
                {
                    "summary": "Incomplete.",
                    "requirement_ids": ["R1"],
                    "evidence_ids": [],
                    "artifact_ids": [],
                }
            ),
            keep_recent=2,
        )
