"""Pure recovery classification for checkpointed product workflows.

This module deliberately performs no graph calls and no persistence.  A
LangGraph checkpoint is the only authority for workflow phase; the thin
registry contributes identity, expiry, and scheduling metadata only.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any, Literal

from .runtime_registry import WorkflowRecord


RecoveryStatus = Literal[
    "missing",
    "orphan",
    "waiting_confirmation",
    "running",
    "completed",
    "failed",
    "cancelled",
    "expired",
]
ReconciliationAction = Literal[
    "delete_orphan",
    "resume_running",
    "expire_waiting",
    "keep_terminal",
    "keep_waiting",
]


__all__ = [
    "ReconciliationAction",
    "RecoveryStatus",
    "derive_workflow_status",
    "proposal_ttl_expired",
    "startup_reconciliation_action",
    "terminal_retention_expired",
    "validate_checkpoint_identity",
    "workflow_outbox_events",
]


_COMPLETED_STATE_VALUES = frozenset({"completed", "committed", "duplicate"})
_TERMINAL_STATE_VALUES: dict[str, RecoveryStatus] = {
    **{value: "completed" for value in _COMPLETED_STATE_VALUES},
    "failed": "failed",
    "cancelled": "cancelled",
    "expired": "expired",
}
_TERMINAL_RECOVERY_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "expired"}
)


def _finite_number(value: object, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field_name} must be a finite number")
    return result


def _snapshot_values(snapshot: object) -> Mapping[str, Any]:
    values = getattr(snapshot, "values", None)
    if not isinstance(values, Mapping):
        raise ValueError("checkpoint values must be a mapping")
    return values


def _snapshot_tuple(snapshot: object, name: str) -> tuple[object, ...]:
    value = getattr(snapshot, name, ())
    if value is None:
        return ()
    if not isinstance(value, tuple):
        raise ValueError(f"checkpoint {name} must be a tuple")
    return value


def validate_checkpoint_identity(
    record: WorkflowRecord,
    values: Mapping[str, Any],
) -> None:
    """Reject any registry/checkpoint identity disagreement.

    Identity is checked before phase inference.  Recovery must never resume a
    checkpoint merely because it was found under a registry-owned thread id.
    """

    expected = {
        "thread_id": record.thread_id,
        "session_id": record.session_id,
        "memory_id": record.memory_id,
        "workflow_type": record.workflow_type,
    }
    for field_name, expected_value in expected.items():
        if values.get(field_name) != expected_value:
            raise ValueError(
                f"checkpoint {field_name} does not match the runtime registry"
            )


def derive_workflow_status(
    record: WorkflowRecord | None,
    snapshot: object | None,
) -> RecoveryStatus:
    """Derive one normalized phase without inventing checkpoint state.

    ``missing`` means no registry record is available (or a non-empty
    checkpoint has no determinable phase).  A registry record with no actual
    checkpoint values is an ``orphan`` and must not be presented as a task.
    Interrupts are authoritative for waiting; a pending ``next`` node without
    an interrupt is resumable running work.
    """

    if record is None:
        return "missing"
    if snapshot is None:
        return "orphan"

    values = _snapshot_values(snapshot)
    if not values:
        return "orphan"
    validate_checkpoint_identity(record, values)

    pending = _snapshot_tuple(snapshot, "next")
    interrupts = _snapshot_tuple(snapshot, "interrupts")
    raw_status = values.get("workflow_status")
    terminal = _TERMINAL_STATE_VALUES.get(raw_status) if isinstance(raw_status, str) else None

    if interrupts:
        if terminal is not None:
            raise ValueError("terminal checkpoint cannot contain an interrupt")
        return "waiting_confirmation"
    if pending:
        return "running"
    if terminal is not None:
        return terminal
    return "missing"


def workflow_outbox_events(
    record: WorkflowRecord,
    snapshot: object,
    status: RecoveryStatus | None = None,
) -> tuple[tuple[str, dict[str, str]], ...]:
    """Derive the one canonical durable event sequence from checkpoint State."""
    values = _snapshot_values(snapshot)
    resolved = status or derive_workflow_status(record, snapshot)
    events: list[tuple[str, dict[str, str]]] = []
    if record.workflow_type == "research":
        if values.get("confirmed") is True:
            events.append(("confirmed", {}))
        if resolved == "completed":
            events.append(("completed", {}))
        elif resolved == "cancelled":
            events.append(("cancelled", {"reason": "user_cancelled"}))
        elif resolved == "expired":
            events.append(("expired", {}))
        elif resolved == "failed":
            raw_code = values.get("failure_code")
            code = (
                raw_code
                if isinstance(raw_code, str)
                and re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", raw_code)
                else "research_failed"
            )
            events.append(("failed", {"code": code}))
        return tuple(events)

    if values.get("decision") == "confirm":
        events.append(("confirmed", {}))
    workflow_status = str(values.get("workflow_status") or "")
    if workflow_status in {"committed", "duplicate"}:
        events.append(("completed", {}))
    elif workflow_status == "cancelled":
        events.append(("cancelled", {"reason": "user_cancelled"}))
    elif workflow_status == "expired":
        events.append(("expired", {}))
    elif workflow_status == "failed":
        result = values.get("result")
        raw_code = result.get("error") if isinstance(result, Mapping) else None
        code = (
            raw_code
            if isinstance(raw_code, str)
            and re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", raw_code)
            else "memory_operation_failed"
        )
        events.append(("failed", {"code": code}))
    return tuple(events)


def proposal_ttl_expired(
    record: WorkflowRecord,
    status: RecoveryStatus,
    *,
    now: float,
) -> bool:
    """Return whether a waiting proposal must be resumed with expiry."""

    current = _finite_number(now, field_name="now")
    return (
        status == "waiting_confirmation"
        and record.expires_at is not None
        and current >= record.expires_at
    )


def terminal_retention_expired(
    status: RecoveryStatus,
    *,
    terminal_at: float,
    now: float,
    retention_seconds: float,
) -> bool:
    """Return whether an already-terminal workflow has passed retention."""

    terminal_time = _finite_number(terminal_at, field_name="terminal_at")
    current = _finite_number(now, field_name="now")
    retention = _finite_number(retention_seconds, field_name="retention_seconds")
    if retention <= 0:
        raise ValueError("retention_seconds must be positive")
    return (
        status in _TERMINAL_RECOVERY_STATUSES
        and current >= terminal_time + retention
    )


def startup_reconciliation_action(
    record: WorkflowRecord | None,
    snapshot: object | None,
    *,
    now: float,
) -> ReconciliationAction:
    """Choose a startup action; callers remain responsible for executing it."""

    status = derive_workflow_status(record, snapshot)
    if status == "missing":
        raise ValueError("startup reconciliation requires a registered workflow")
    if record is None:  # pragma: no cover - narrowed by the status check
        raise ValueError("startup reconciliation requires a registered workflow")
    if status == "orphan":
        return "delete_orphan"
    if status == "running":
        return "resume_running"
    if status == "waiting_confirmation":
        return (
            "expire_waiting"
            if proposal_ttl_expired(record, status, now=now)
            else "keep_waiting"
        )
    return "keep_terminal"
