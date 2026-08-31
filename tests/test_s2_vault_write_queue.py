from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from src.research.vault_write_queue import (
    VAULT_WRITE_JOB_STATUSES,
    VAULT_WRITE_OPERATION_TYPES,
    VaultWriteQueue,
)


def _queue(tmp_path: Path, scope: str = "vault-a") -> VaultWriteQueue:
    return VaultWriteQueue(
        tmp_path / "runtime.db",
        vault_scope=scope,
        poll_interval_seconds=0.005,
    )


def _command(value: str = "command") -> tuple[bytes, str]:
    blob = value.encode("utf-8")
    return blob, hashlib.sha256(blob).hexdigest()


def _enqueue(
    queue: VaultWriteQueue,
    key: str = "note:one",
    *,
    value: str = "command",
    created_at: float = 1.0,
):
    blob, digest = _command(value)
    return queue.enqueue(
        idempotency_key=key,
        operation_type="memory_note",
        memory_id="M-one",
        origin_thread_id="memory-note-one",
        command_blob=blob,
        command_hash=digest,
        created_at=created_at,
    )


def test_schema_has_explicit_operations_statuses_and_per_vault_keys(
    tmp_path: Path,
) -> None:
    queue = _queue(tmp_path)

    with sqlite3.connect(queue.db_path) as connection:
        jobs = connection.execute("SELECT sql FROM sqlite_master WHERE name = 'vault_write_jobs'").fetchone()[0]
        lease = connection.execute("SELECT sql FROM sqlite_master WHERE name = 'vault_writer_lease'").fetchone()[0]

    assert VAULT_WRITE_OPERATION_TYPES == {
        "create_memory",
        "research_bundle",
        "report_review",
        "memory_note",
        "memory_import",
        "legacy_copy",
        "tool_artifact",
    }
    assert VAULT_WRITE_JOB_STATUSES == {
        "queued",
        "running",
        "succeeded",
        "conflict",
        "failed",
    }
    assert "PRIMARY KEY (vault_scope, job_id)" in jobs
    assert "UNIQUE (vault_scope, idempotency_key)" in jobs
    assert "vault_scope TEXT PRIMARY KEY" in lease


def test_enqueue_is_stable_and_rejects_idempotency_collisions(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    first = _enqueue(queue)
    repeated = _enqueue(queue, created_at=99)

    assert repeated == first
    assert first.job_id == queue.stable_job_id("vault-a", "note:one")
    assert first.status == "queued"
    assert first.command_blob == b"command"

    blob, digest = _command("different")
    with pytest.raises(ValueError, match="idempotency key collision"):
        queue.enqueue(
            idempotency_key="note:one",
            operation_type="memory_note",
            memory_id="M-one",
            origin_thread_id="memory-note-one",
            command_blob=blob,
            command_hash=digest,
        )


def test_enqueue_validates_command_hash_and_bounded_identity(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    blob, digest = _command()

    with pytest.raises(ValueError, match="does not match"):
        queue.enqueue(
            idempotency_key="one",
            operation_type="memory_note",
            memory_id="M-one",
            command_blob=blob,
            command_hash="0" * 64,
        )
    with pytest.raises(ValueError, match="unsupported"):
        queue.enqueue(
            idempotency_key="one",
            operation_type="delete_vault",
            memory_id="M-one",
            command_blob=blob,
            command_hash=digest,
        )
    with pytest.raises(ValueError, match="non-empty bytes"):
        queue.enqueue(
            idempotency_key="one",
            operation_type="memory_note",
            memory_id="M-one",
            command_blob=b"",
            command_hash=hashlib.sha256(b"").hexdigest(),
        )


@pytest.mark.parametrize(
    "job_id",
    ("bad/id", "../escape", ".leading", "bad\\id", "bad id"),
)
def test_enqueue_rejects_explicit_job_ids_unsafe_for_private_state(
    tmp_path: Path,
    job_id: str,
) -> None:
    queue = _queue(tmp_path)
    blob, digest = _command()

    with pytest.raises(ValueError, match="safe for private Writer state"):
        queue.enqueue(
            job_id=job_id,
            idempotency_key=f"unsafe:{job_id}",
            operation_type="memory_note",
            memory_id="M-one",
            command_blob=blob,
            command_hash=digest,
        )


def test_same_database_isolates_jobs_and_writer_leases_by_vault_scope(
    tmp_path: Path,
) -> None:
    alpha = _queue(tmp_path, "vault-a")
    beta = _queue(tmp_path, "vault-b")
    alpha_job = _enqueue(alpha)
    beta_job = _enqueue(beta)

    assert alpha_job.job_id != beta_job.job_id
    assert alpha.get(beta_job.job_id) is None
    assert beta.get(alpha_job.job_id) is None
    alpha_lease = alpha.claim_writer(owner="alpha", lease_seconds=10, now=1)
    beta_lease = beta.claim_writer(owner="beta", lease_seconds=10, now=1)
    assert alpha_lease is not None
    assert beta_lease is not None
    assert alpha_lease.generation == beta_lease.generation == 1


def test_only_one_writer_lease_and_expiry_increments_generation(tmp_path: Path) -> None:
    first = _queue(tmp_path)
    second = _queue(tmp_path)
    lease_one = first.claim_writer(owner="writer-one", lease_seconds=10, now=100)

    assert lease_one is not None
    assert second.claim_writer(owner="writer-two", lease_seconds=10, now=105) is None
    renewed = first.renew_writer(lease_one, lease_seconds=10, now=105)
    assert renewed is not None
    assert renewed.lease_until == 115
    assert second.claim_writer(owner="writer-two", lease_seconds=10, now=114) is None

    lease_two = second.claim_writer(owner="writer-two", lease_seconds=10, now=115)
    assert lease_two is not None
    assert lease_two.generation == lease_one.generation + 1
    assert first.renew_writer(lease_one, lease_seconds=10, now=115) is None
    with pytest.raises(RuntimeError, match="lease was lost"):
        first.assert_fence(lease_one, now=115)


def test_release_requires_exact_owner_and_generation(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    lease = queue.claim_writer(owner="writer", lease_seconds=10, now=1)
    assert lease is not None

    assert queue.release_writer(lease)
    assert not queue.release_writer(lease)
    replacement = queue.claim_writer(owner="writer", lease_seconds=10, now=2)
    assert replacement is not None
    assert replacement.generation == lease.generation + 1


def test_claim_next_is_fifo_and_enforces_one_running_job(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    later = _enqueue(queue, "later", value="later", created_at=2)
    earlier = _enqueue(queue, "earlier", value="earlier", created_at=1)
    lease = queue.claim_writer(owner="writer", lease_seconds=20, now=10)
    assert lease is not None

    claimed = queue.claim_next(lease, lease_seconds=20, now=10)
    assert claimed is not None
    assert claimed.job_id == earlier.job_id
    assert claimed.status == "running"
    assert claimed.lease_generation == lease.generation

    same = queue.claim_next(lease, lease_seconds=20, now=11)
    assert same is not None
    assert same.job_id == earlier.job_id
    queue.complete(earlier.job_id, lease, {"path": "notes/one.md"}, now=12)
    next_job = queue.claim_next(lease, lease_seconds=20, now=13)
    assert next_job is not None
    assert next_job.job_id == later.job_id


def test_claim_next_can_target_one_recovery_job_without_bypassing_running_job(
    tmp_path: Path,
) -> None:
    queue = _queue(tmp_path)
    first = _enqueue(queue, "first", value="first", created_at=1)
    second = _enqueue(queue, "second", value="second", created_at=2)
    lease = queue.claim_writer(owner="writer", lease_seconds=20, now=10)
    assert lease is not None

    targeted = queue.claim_next(lease, lease_seconds=20, job_id=second.job_id, now=10)
    assert targeted is not None
    assert targeted.job_id == second.job_id
    assert queue.claim_next(lease, lease_seconds=20, job_id=first.job_id, now=11) is None


def test_expired_writer_takes_over_running_job_and_stale_owner_is_fenced(
    tmp_path: Path,
) -> None:
    old_queue = _queue(tmp_path)
    new_queue = _queue(tmp_path)
    job = _enqueue(old_queue)
    old_lease = old_queue.claim_writer(owner="old", lease_seconds=5, now=10)
    assert old_lease is not None
    assert old_queue.claim_next(old_lease, lease_seconds=50, now=10) is not None

    new_lease = new_queue.claim_writer(owner="new", lease_seconds=20, now=15)
    assert new_lease is not None
    reclaimed = new_queue.claim_next(new_lease, lease_seconds=20, now=15)
    assert reclaimed is not None
    assert reclaimed.job_id == job.job_id
    assert reclaimed.lease_owner == "new"
    assert reclaimed.lease_generation == new_lease.generation

    with pytest.raises(RuntimeError, match="lease was lost"):
        old_queue.assert_fence(old_lease, job_id=job.job_id, now=15)
    assert old_queue.renew_job(job.job_id, old_lease, lease_seconds=20, now=15) is None


def test_renew_job_renews_global_and_job_fence_together(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    job = _enqueue(queue)
    lease = queue.claim_writer(owner="writer", lease_seconds=5, now=1)
    assert lease is not None
    queue.claim_next(lease, lease_seconds=5, now=1)

    renewed = queue.renew_job(job.job_id, lease, lease_seconds=10, now=4)
    assert renewed is not None
    assert renewed.lease_until == 14
    queue.assert_fence(lease, job_id=job.job_id, now=13)
    with pytest.raises(RuntimeError, match="lease was lost"):
        queue.assert_fence(lease, job_id=job.job_id, now=15)


def test_run_fenced_holds_database_writer_fence_and_rolls_back_on_error(
    tmp_path: Path,
) -> None:
    queue = _queue(tmp_path)
    job = _enqueue(queue)
    lease = queue.claim_writer(owner="writer", lease_seconds=10, now=1)
    assert lease is not None
    queue.claim_next(lease, lease_seconds=10, now=1)
    calls: list[str] = []

    assert queue.run_fenced(job.job_id, lease, lambda: calls.append("published") or "ok", now=2) == "ok"
    assert calls == ["published"]

    def fail_action() -> None:
        raise OSError("injected")

    with pytest.raises(OSError, match="injected"):
        queue.run_fenced(job.job_id, lease, fail_action, now=2)
    assert queue.get(job.job_id).status == "running"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("method", "expected_status"),
    (("complete", "succeeded"), ("conflict", "conflict"), ("fail", "failed")),
)
def test_terminal_transitions_clear_command_and_are_idempotent(
    tmp_path: Path,
    method: str,
    expected_status: str,
) -> None:
    queue = _queue(tmp_path)
    job = _enqueue(queue)
    lease = queue.claim_writer(owner="writer", lease_seconds=10, now=1)
    assert lease is not None
    queue.claim_next(lease, lease_seconds=10, now=1)

    if method == "complete":
        terminal = queue.complete(job.job_id, lease, {"path": "notes/one.md"}, now=2)
        repeated = queue.complete(job.job_id, lease, {"path": "notes/one.md"}, now=99)
        assert terminal.result == {"path": "notes/one.md"}
    else:
        transition = getattr(queue, method)
        terminal = transition(job.job_id, lease, "content_conflict", now=2)
        repeated = transition(job.job_id, lease, "content_conflict", now=99)
        assert terminal.error_code == "content_conflict"

    assert terminal.status == expected_status
    assert terminal.command_blob is None
    assert terminal.lease_owner is None
    assert repeated == terminal


def test_terminal_result_collision_is_rejected(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    job = _enqueue(queue)
    lease = queue.claim_writer(owner="writer", lease_seconds=10, now=1)
    assert lease is not None
    queue.claim_next(lease, lease_seconds=10, now=1)
    queue.complete(job.job_id, lease, {"path": "one.md"}, now=2)

    with pytest.raises(ValueError, match="terminal result collision"):
        queue.complete(job.job_id, lease, {"path": "other.md"}, now=3)
    with pytest.raises(ValueError, match="terminal result collision"):
        queue.fail(job.job_id, lease, "late_failure", now=3)


def test_wait_polls_until_terminal_and_times_out(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    job = _enqueue(queue)
    lease = queue.claim_writer(owner="writer", lease_seconds=10, now=time.time())
    assert lease is not None
    queue.claim_next(lease, lease_seconds=10)

    def finish() -> None:
        time.sleep(0.02)
        queue.complete(job.job_id, lease, {"path": "notes/one.md"})

    worker = threading.Thread(target=finish)
    worker.start()
    try:
        assert queue.wait(job.job_id, timeout=1).status == "succeeded"
    finally:
        worker.join()

    pending = _enqueue(queue, "pending", value="pending", created_at=2)
    with pytest.raises(TimeoutError, match="did not finish"):
        queue.wait(pending.job_id, timeout=0.01)
    with pytest.raises(FileNotFoundError):
        queue.wait("missing", timeout=0)


def test_invalid_error_codes_and_cross_scope_leases_are_rejected(tmp_path: Path) -> None:
    alpha = _queue(tmp_path, "vault-a")
    beta = _queue(tmp_path, "vault-b")
    job = _enqueue(alpha)
    lease = alpha.claim_writer(owner="writer", lease_seconds=10, now=1)
    assert lease is not None
    alpha.claim_next(lease, lease_seconds=10, now=1)

    with pytest.raises(ValueError, match="bounded safe code"):
        alpha.fail(job.job_id, lease, "Contains spaces", now=2)
    with pytest.raises(ValueError, match="another Vault scope"):
        beta.assert_fence(lease, now=2)
