"""Pure S1 workflow recovery classification and reconciliation tests."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.research.runtime_registry import WorkflowRecord
from src.research.workflow_recovery import (
    derive_workflow_status,
    proposal_ttl_expired,
    startup_reconciliation_action,
    terminal_retention_expired,
    workflow_outbox_events,
)


def _record(**changes: object) -> WorkflowRecord:
    values: dict[str, object] = {
        "task_id": "task-a",
        "thread_id": "thread-a",
        "session_id": "session-a",
        "memory_id": "M-A",
        "workflow_type": "research",
        "created_at": 100.0,
        "expires_at": 200.0,
        "lease_owner": None,
        "lease_until": None,
    }
    values.update(changes)
    return WorkflowRecord(**values)  # type: ignore[arg-type]


def _snapshot(
    *,
    workflow_status: str = "running",
    next_nodes: tuple[str, ...] = (),
    interrupts: tuple[object, ...] = (),
    **identity: object,
) -> SimpleNamespace:
    values: dict[str, object] = {
        "thread_id": "thread-a",
        "session_id": "session-a",
        "memory_id": "M-A",
        "workflow_type": "research",
        "workflow_status": workflow_status,
    }
    values.update(identity)
    return SimpleNamespace(values=values, next=next_nodes, interrupts=interrupts)


def test_missing_registry_and_missing_checkpoint_are_not_fabricated() -> None:
    assert derive_workflow_status(None, _snapshot()) == "missing"
    assert derive_workflow_status(_record(), None) == "orphan"
    assert derive_workflow_status(
        _record(), SimpleNamespace(values={}, next=(), interrupts=())
    ) == "orphan"
    assert startup_reconciliation_action(_record(), None, now=150) == "delete_orphan"
    with pytest.raises(ValueError, match="registered workflow"):
        startup_reconciliation_action(None, _snapshot(), now=150)


@pytest.mark.parametrize(
    ("field_name", "wrong_value"),
    [
        ("thread_id", "thread-other"),
        ("session_id", "session-other"),
        ("memory_id", "M-B"),
        ("workflow_type", "memory_note"),
    ],
)
def test_registry_and_checkpoint_identity_must_match(
    field_name: str,
    wrong_value: str,
) -> None:
    with pytest.raises(ValueError, match=field_name):
        derive_workflow_status(_record(), _snapshot(**{field_name: wrong_value}))


def test_interrupt_is_waiting_and_ttl_only_schedules_explicit_expiry() -> None:
    record = _record(expires_at=200.0)
    snapshot = _snapshot(
        workflow_status="waiting_confirmation",
        next_nodes=("review_brief",),
        interrupts=(object(),),
    )
    status = derive_workflow_status(record, snapshot)
    assert status == "waiting_confirmation"
    assert startup_reconciliation_action(record, snapshot, now=199.0) == "keep_waiting"
    assert startup_reconciliation_action(record, snapshot, now=200.0) == "expire_waiting"
    assert not proposal_ttl_expired(record, status, now=199.999)
    assert proposal_ttl_expired(record, status, now=200.0)
    assert not proposal_ttl_expired(record, "running", now=300.0)


def test_pending_node_without_interrupt_is_running_and_resumable() -> None:
    record = _record()
    between_nodes = _snapshot(
        workflow_status="waiting_confirmation",
        next_nodes=("review_brief",),
    )
    assert derive_workflow_status(record, between_nodes) == "running"
    assert (
        startup_reconciliation_action(record, between_nodes, now=150.0)
        == "resume_running"
    )


@pytest.mark.parametrize(
    ("stored_status", "derived_status"),
    [
        ("completed", "completed"),
        ("committed", "completed"),
        ("duplicate", "completed"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
        ("expired", "expired"),
    ],
)
def test_terminal_workflow_statuses_are_normalized_and_kept(
    stored_status: str,
    derived_status: str,
) -> None:
    record = _record()
    snapshot = _snapshot(workflow_status=stored_status)
    assert derive_workflow_status(record, snapshot) == derived_status
    assert startup_reconciliation_action(record, snapshot, now=500.0) == "keep_terminal"


def test_checkpoint_without_a_determinable_phase_is_missing() -> None:
    snapshot = _snapshot(workflow_status="preparing")
    assert derive_workflow_status(_record(), snapshot) == "missing"
    with pytest.raises(ValueError, match="registered workflow"):
        startup_reconciliation_action(_record(), snapshot, now=150.0)


def test_terminal_interrupt_contradiction_is_rejected() -> None:
    snapshot = _snapshot(workflow_status="completed", interrupts=(object(),))
    with pytest.raises(ValueError, match="terminal checkpoint"):
        derive_workflow_status(_record(), snapshot)


def test_terminal_retention_is_a_separate_exact_boundary_decision() -> None:
    assert not terminal_retention_expired(
        "completed", terminal_at=100.0, now=159.999, retention_seconds=60.0
    )
    assert terminal_retention_expired(
        "failed", terminal_at=100.0, now=160.0, retention_seconds=60.0
    )
    assert not terminal_retention_expired(
        "running", terminal_at=100.0, now=1000.0, retention_seconds=60.0
    )
    with pytest.raises(ValueError, match="positive"):
        terminal_retention_expired(
            "completed", terminal_at=100.0, now=160.0, retention_seconds=0.0
        )


def test_research_outbox_events_are_derived_with_stable_failure_code() -> None:
    record = _record()
    snapshot = _snapshot(
        workflow_status="failed",
        confirmed=True,
        failure_code="runtimeerror",
    )
    assert workflow_outbox_events(record, snapshot) == (
        ("confirmed", {}),
        ("failed", {"code": "runtimeerror"}),
    )


def test_memory_outbox_events_share_one_canonical_derivation() -> None:
    record = _record(workflow_type="memory_note")
    snapshot = _snapshot(
        workflow_type="memory_note",
        workflow_status="committed",
        decision="confirm",
    )
    assert workflow_outbox_events(record, snapshot) == (
        ("confirmed", {}),
        ("completed", {}),
    )
