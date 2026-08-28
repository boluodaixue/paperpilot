from __future__ import annotations

import hashlib
import multiprocessing
import os
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

import src.research.vault_writer as vault_writer_module
from src.research.memory import _legacy_snapshot
from src.research.vault_write_queue import VaultWriteQueue
from src.research.vault_writer import (
    VaultWriteCommandError,
    VaultWriter,
    build_directory_create_command,
    build_file_bundle_command,
    canonical_command_hash,
)

_MEMORY_ID = "M-writer-test"
_DIRECTORIES = tuple(
    f"Memories/{_MEMORY_ID}/{name}" for name in ("reports", "evidence", "sources", "notes", "imports", "attachments")
)


class _Crash(BaseException):
    pass


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _queue(tmp_path: Path, vault: Path, *, scope: str = "test-vault") -> VaultWriteQueue:
    vault.mkdir(parents=True, exist_ok=True)
    return VaultWriteQueue(tmp_path / "runtime.sqlite3", vault_scope=scope)


def _enqueue(queue: VaultWriteQueue, blob: bytes, operation_type: str, key: str):
    return queue.enqueue(
        idempotency_key=key,
        operation_type=operation_type,
        memory_id=_MEMORY_ID,
        command_blob=blob,
        command_hash=canonical_command_hash(blob),
        origin_thread_id="thread-writer-test",
    )


def _note_blob(old_home: bytes = b"old-home\n") -> bytes:
    new_home = b"new-home-with-note\n"
    return build_file_bundle_command(
        operation_type="memory_note",
        memory_id=_MEMORY_ID,
        anchor_path=f"Memories/{_MEMORY_ID}/Home.md",
        targets=(
            {
                "path": f"Memories/{_MEMORY_ID}/notes/Note-one.md",
                "content": b"note-one\n",
                "expected_mode": "absent",
            },
            {
                "path": f"Memories/{_MEMORY_ID}/Home.md",
                "content": new_home,
                "expected_mode": "hash",
                "expected_hash": _digest(old_home),
            },
        ),
        input_hashes={"answer": _digest(b"answer")},
        expected_home_hash=_digest(old_home),
        result={
            "home_path": f"Memories/{_MEMORY_ID}/Home.md",
            "target_path": f"Memories/{_MEMORY_ID}/notes/Note-one.md",
        },
    )


def _addition_rollback_blob() -> bytes:
    report_path = f"Memories/{_MEMORY_ID}/reports/Report-one.md"
    return build_file_bundle_command(
        operation_type="research_bundle",
        memory_id=_MEMORY_ID,
        anchor_path=report_path,
        targets=(
            {
                "path": f"Memories/{_MEMORY_ID}/evidence/Evidence-one.md",
                "content": b"new-evidence",
                "expected_mode": "absent",
            },
            {
                "path": f"Memories/{_MEMORY_ID}/sources/Source-one.md",
                "content": b"new-source",
                "expected_mode": "hash",
                "expected_hash": _digest(b"old-source"),
            },
            {
                "path": report_path,
                "content": b"new-report",
                "expected_mode": "absent",
            },
        ),
        result={"report_path": report_path},
    )


def _seed_note_vault(vault: Path, old_home: bytes = b"old-home\n") -> None:
    memory = vault / "Memories" / _MEMORY_ID
    for relative in _DIRECTORIES:
        (vault / relative).mkdir(parents=True, exist_ok=True)
    (memory / "Home.md").write_bytes(old_home)


def _takeover(queue: VaultWriteQueue):
    lease = queue.claim_writer(
        lease_seconds=30,
        owner=f"takeover-{time.time_ns()}",
        now=time.time() + 60,
    )
    assert lease is not None
    return lease


def _make_directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (NotImplementedError, OSError) as symlink_error:
        if os.name != "nt":
            pytest.skip(f"symbolic links are unavailable: {symlink_error}")
    junction = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if junction.returncode != 0:
        pytest.skip("symbolic links and junctions are unavailable")


def _install_exchange_simulation(
    monkeypatch: pytest.MonkeyPatch,
    before_exchange,
) -> None:
    def windows_exchange(target: Path, replacement: Path, backup: Path) -> None:
        before_exchange(target)
        old = target.read_bytes()
        new = replacement.read_bytes()
        target.write_bytes(new)
        backup.write_bytes(old)
        replacement.unlink()

    def posix_exchange(target: Path, replacement: Path) -> None:
        before_exchange(target)
        old = target.read_bytes()
        new = replacement.read_bytes()
        target.write_bytes(new)
        replacement.write_bytes(old)

    monkeypatch.setattr(vault_writer_module, "_windows_replace_file", windows_exchange)
    monkeypatch.setattr(vault_writer_module, "_atomic_exchange", posix_exchange)


def _process_crash_worker(
    vault: str,
    database: str,
    scope: str,
    failpoint: str,
) -> None:
    queue = VaultWriteQueue(database, vault_scope=scope)
    lease = queue.claim_writer(lease_seconds=30, owner="crashing-writer")
    if lease is None:
        os._exit(71)

    def crash(name: str) -> None:
        if name == failpoint:
            os._exit(77)

    writer = VaultWriter(vault, queue, failpoint=crash)
    writer.run_once(lease)
    os._exit(0)


def _concurrent_idempotent_worker(
    vault: str,
    database: str,
    scope: str,
    blob: bytes,
    start_event,
) -> None:
    queue = VaultWriteQueue(database, vault_scope=scope, poll_interval_seconds=0.01)
    start_event.wait(10)
    job = queue.enqueue(
        idempotency_key="shared-idempotent-note",
        operation_type="memory_note",
        memory_id=_MEMORY_ID,
        command_blob=blob,
        command_hash=canonical_command_hash(blob),
        origin_thread_id="shared-origin",
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        current = queue.get(job.job_id)
        if current is not None and current.terminal:
            return
        lease = queue.claim_writer(lease_seconds=1, owner=f"worker-{os.getpid()}")
        if lease is not None:
            try:
                VaultWriter(vault, queue, job_lease_seconds=1).recover(
                    lease,
                    job_ids=(job.job_id,),
                )
            finally:
                queue.release_writer(lease)
        time.sleep(0.01)
    raise TimeoutError("concurrent writer did not converge")


def test_command_is_canonical_and_home_snapshot_is_explicit() -> None:
    blob = _note_blob()
    assert canonical_command_hash(blob) == _digest(blob)
    with pytest.raises(VaultWriteCommandError, match="expected_home_hash"):
        build_file_bundle_command(
            operation_type="memory_note",
            memory_id=_MEMORY_ID,
            anchor_path=f"Memories/{_MEMORY_ID}/Home.md",
            targets=(
                {
                    "path": f"Memories/{_MEMORY_ID}/notes/Note-missing-home-hash.md",
                    "content": b"note",
                    "expected_mode": "absent",
                },
                {
                    "path": f"Memories/{_MEMORY_ID}/Home.md",
                    "content": b"new",
                    "expected_mode": "hash",
                    "expected_hash": _digest(b"old"),
                },
            ),
            expected_home_hash=None,
            result={"home_path": f"Memories/{_MEMORY_ID}/Home.md"},
        )
    with pytest.raises(VaultWriteCommandError, match="private writer"):
        build_file_bundle_command(
            operation_type="research_bundle",
            memory_id=_MEMORY_ID,
            anchor_path="Memories/.paperpilot-writer/escape.md",
            targets=(
                {
                    "path": "Memories/.paperpilot-writer/escape.md",
                    "content": b"bad",
                    "expected_mode": "absent",
                },
            ),
            result={"report_path": "bad"},
        )
    for escaped in (
        "Memories/M-other/sources/Source-bad.md",
        ".obsidian/plugins/paperpilot.md",
    ):
        with pytest.raises(VaultWriteCommandError, match="Memory boundary"):
            build_file_bundle_command(
                operation_type="research_bundle",
                memory_id=_MEMORY_ID,
                anchor_path=f"Memories/{_MEMORY_ID}/reports/Report-safe.md",
                targets=(
                    {
                        "path": escaped,
                        "content": b"bad",
                        "expected_mode": "absent",
                    },
                    {
                        "path": f"Memories/{_MEMORY_ID}/reports/Report-safe.md",
                        "content": b"report",
                        "expected_mode": "absent",
                    },
                ),
                result={"report_path": f"Memories/{_MEMORY_ID}/reports/Report-safe.md"},
            )
    with pytest.raises(VaultWriteCommandError, match="selected managed Memory root"):
        build_directory_create_command(
            operation_type="create_memory",
            memory_id=_MEMORY_ID,
            anchor_directory="Memories/M-other",
            directories=tuple(
                f"Memories/M-other/{name}"
                for name in (
                    "reports",
                    "evidence",
                    "sources",
                    "notes",
                    "imports",
                    "attachments",
                )
            ),
            files=({"path": "Memories/M-other/Home.md", "content": b"bad"},),
            result={"memory_id": _MEMORY_ID},
        )


def test_file_bundle_success_and_queue_idempotency(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _seed_note_vault(vault)
    queue = _queue(tmp_path, vault)
    blob = _note_blob()
    job = _enqueue(queue, blob, "memory_note", "note:one")
    lease = queue.claim_writer(lease_seconds=30, owner="writer-one")
    assert lease is not None
    terminal = VaultWriter(vault, queue).run_once(lease)
    assert terminal is not None and terminal.status == "succeeded"
    assert (vault / f"Memories/{_MEMORY_ID}/Home.md").read_bytes() == b"new-home-with-note\n"
    assert (vault / f"Memories/{_MEMORY_ID}/notes/Note-one.md").read_bytes() == b"note-one\n"
    assert _enqueue(queue, blob, "memory_note", "note:one") == terminal
    assert not (vault / "Memories" / ".paperpilot-writer" / "jobs" / job.job_id).exists()


@pytest.mark.parametrize(
    "failpoint",
    (
        "after_stage_file:0",
        "after_prepared",
        "after_publish_target:1",
        "before_anchor",
        "after_replace_exchange:0",
        "after_anchor",
        "after_linearized",
        "after_completed",
        "after_db_success",
    ),
)
def test_hard_crash_phases_recover_to_complete_new_state(
    tmp_path: Path,
    failpoint: str,
) -> None:
    vault = tmp_path / "vault"
    _seed_note_vault(vault)
    scope = f"scope-{failpoint}"
    queue = _queue(tmp_path, vault, scope=scope)
    blob = _note_blob()
    job = _enqueue(queue, blob, "memory_note", f"note:{failpoint}")

    process = multiprocessing.get_context("spawn").Process(
        target=_process_crash_worker,
        args=(str(vault), queue.db_path, scope, failpoint),
    )
    process.start()
    process.join(20)
    assert process.exitcode == 77

    lease = _takeover(queue)
    VaultWriter(vault, queue).recover(lease)
    terminal = queue.get(job.job_id)
    assert terminal is not None and terminal.status == "succeeded"
    assert (vault / f"Memories/{_MEMORY_ID}/Home.md").read_bytes() == b"new-home-with-note\n"
    assert (vault / f"Memories/{_MEMORY_ID}/notes/Note-one.md").read_bytes() == b"note-one\n"


def test_two_processes_share_one_writer_and_one_idempotent_publication(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _seed_note_vault(vault)
    scope = "two-process-idempotent"
    queue = _queue(tmp_path, vault, scope=scope)
    blob = _note_blob()
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    processes = [
        context.Process(
            target=_concurrent_idempotent_worker,
            args=(str(vault), queue.db_path, scope, blob, start),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(20)

    assert [process.exitcode for process in processes] == [0, 0]
    jobs = queue.list()
    assert len(jobs) == 1 and jobs[0].status == "succeeded"
    assert (vault / f"Memories/{_MEMORY_ID}/notes/Note-one.md").read_bytes() == b"note-one\n"
    assert (vault / f"Memories/{_MEMORY_ID}/Home.md").read_bytes() == b"new-home-with-note\n"


def test_external_home_edit_is_preserved_and_known_leaf_is_rolled_back(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _seed_note_vault(vault)
    queue = _queue(tmp_path, vault)
    job = _enqueue(queue, _note_blob(), "memory_note", "note:external-home")
    lease = queue.claim_writer(lease_seconds=30, owner="writer-one")
    assert lease is not None

    def crash(name: str) -> None:
        if name == "after_publish_target:1":
            raise _Crash

    with pytest.raises(_Crash):
        VaultWriter(vault, queue, failpoint=crash).run_once(lease)
    home = vault / f"Memories/{_MEMORY_ID}/Home.md"
    note = vault / f"Memories/{_MEMORY_ID}/notes/Note-one.md"
    assert note.exists() and home.read_bytes() == b"old-home\n"
    home.write_bytes(b"obsidian-home\n")
    queue.release_writer(lease)
    takeover = _takeover(queue)
    recovered = VaultWriter(vault, queue).recover(takeover)
    assert recovered and recovered[0].status == "conflict"
    assert home.read_bytes() == b"obsidian-home\n"
    assert not note.exists()


@pytest.mark.parametrize("action", ("modify", "delete"))
def test_after_anchor_external_leaf_action_is_preserved_as_conflict(
    tmp_path: Path,
    action: str,
) -> None:
    vault = tmp_path / "vault"
    _seed_note_vault(vault)
    queue = _queue(tmp_path, vault)
    job = _enqueue(queue, _note_blob(), "memory_note", f"note:after-anchor:{action}")
    lease = queue.claim_writer(lease_seconds=30, owner="writer-one")
    assert lease is not None
    note = vault / f"Memories/{_MEMORY_ID}/notes/Note-one.md"
    home = vault / f"Memories/{_MEMORY_ID}/Home.md"

    def external_action(name: str) -> None:
        if name != "after_anchor":
            return
        if action == "modify":
            note.write_bytes(b"external-note-after-anchor\n")
        else:
            note.unlink()

    terminal = VaultWriter(vault, queue, failpoint=external_action).run_once(lease)

    assert terminal is not None and terminal.status == "conflict"
    assert terminal.error_code == "vault_conflict_quarantined"
    assert home.read_bytes() == b"new-home-with-note\n"
    if action == "modify":
        assert note.read_bytes() == b"external-note-after-anchor\n"
    else:
        assert not note.exists()
    assert (vault / "Memories" / ".paperpilot-writer" / "jobs" / job.job_id).is_dir()


def test_recovery_with_new_anchor_never_recreates_deleted_leaf(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _seed_note_vault(vault)
    queue = _queue(tmp_path, vault)
    job = _enqueue(queue, _note_blob(), "memory_note", "note:recover-deleted-leaf")
    lease = queue.claim_writer(lease_seconds=30, owner="writer-one")
    assert lease is not None
    note = vault / f"Memories/{_MEMORY_ID}/notes/Note-one.md"

    def delete_and_crash(name: str) -> None:
        if name == "after_anchor":
            note.unlink()
            raise _Crash

    with pytest.raises(_Crash):
        VaultWriter(vault, queue, failpoint=delete_and_crash).run_once(lease)
    queue.release_writer(lease)

    terminal = VaultWriter(vault, queue).recover(_takeover(queue))[0]

    assert terminal.status == "conflict"
    assert terminal.error_code == "vault_conflict_quarantined"
    assert not note.exists()
    assert (vault / "Memories" / ".paperpilot-writer" / "jobs" / job.job_id).is_dir()


@pytest.mark.parametrize("target_name", ("home", "note"))
@pytest.mark.parametrize("action", ("modify", "delete"))
def test_after_linearized_external_target_action_never_succeeds(
    tmp_path: Path,
    target_name: str,
    action: str,
) -> None:
    vault = tmp_path / "vault"
    _seed_note_vault(vault)
    queue = _queue(tmp_path, vault)
    job = _enqueue(
        queue,
        _note_blob(),
        "memory_note",
        f"note:after-linearized:{target_name}:{action}",
    )
    lease = queue.claim_writer(lease_seconds=30, owner="writer-one")
    assert lease is not None
    home = vault / f"Memories/{_MEMORY_ID}/Home.md"
    note = vault / f"Memories/{_MEMORY_ID}/notes/Note-one.md"
    target = home if target_name == "home" else note
    external = f"external-{target_name}-after-linearized\n".encode()

    def external_action(name: str) -> None:
        if name != "after_linearized":
            return
        if action == "modify":
            target.write_bytes(external)
        else:
            target.unlink()

    terminal = VaultWriter(vault, queue, failpoint=external_action).run_once(lease)

    assert terminal is not None and terminal.status == "conflict"
    assert terminal.error_code == "vault_conflict_quarantined"
    if action == "modify":
        assert target.read_bytes() == external
    else:
        assert not target.exists()
    other = note if target_name == "home" else home
    assert other.exists()
    assert (vault / "Memories" / ".paperpilot-writer" / "jobs" / job.job_id).is_dir()


def test_second_leaf_conflict_rolls_first_leaf_back_before_terminal(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    memory = vault / "Memories" / _MEMORY_ID
    (memory / "sources").mkdir(parents=True)
    (memory / "evidence").mkdir()
    (memory / "reports").mkdir()
    source = memory / "sources" / "Source-one.md"
    evidence = memory / "evidence" / "Evidence-one.md"
    source.write_bytes(b"old-source")
    evidence.write_bytes(b"foreign-evidence")
    blob = build_file_bundle_command(
        operation_type="research_bundle",
        memory_id=_MEMORY_ID,
        anchor_path=f"Memories/{_MEMORY_ID}/reports/Report-one.md",
        targets=(
            {
                "path": f"Memories/{_MEMORY_ID}/sources/Source-one.md",
                "content": b"new-source",
                "expected_mode": "hash",
                "expected_hash": _digest(b"old-source"),
            },
            {
                "path": f"Memories/{_MEMORY_ID}/evidence/Evidence-one.md",
                "content": b"new-evidence",
                "expected_mode": "hash",
                "expected_hash": _digest(b"expected-evidence"),
            },
            {
                "path": f"Memories/{_MEMORY_ID}/reports/Report-one.md",
                "content": b"new-report",
                "expected_mode": "absent",
            },
        ),
        input_hashes={"research_result": _digest(b"result")},
        result={"report_path": f"Memories/{_MEMORY_ID}/reports/Report-one.md"},
    )
    queue = _queue(tmp_path, vault)
    job = _enqueue(queue, blob, "research_bundle", "research:leaf-conflict")
    lease = queue.claim_writer(lease_seconds=30, owner="writer-one")
    assert lease is not None
    terminal = VaultWriter(vault, queue).run_once(lease)
    assert terminal is not None and terminal.status == "conflict"
    # Preparation discovers the second target before publication, so no leaf is
    # changed. The complementary publication-window case is covered below.
    assert source.read_bytes() == b"old-source"
    assert evidence.read_bytes() == b"foreign-evidence"
    assert queue.get(job.job_id).status == "conflict"  # type: ignore[union-attr]


def test_publication_window_second_leaf_conflict_restores_first_leaf(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    memory = vault / "Memories" / _MEMORY_ID
    for name in ("sources", "evidence", "reports"):
        (memory / name).mkdir(parents=True, exist_ok=True)
    source = memory / "sources" / "Source-one.md"
    evidence = memory / "evidence" / "Evidence-one.md"
    source.write_bytes(b"old-source")
    evidence.write_bytes(b"old-evidence")
    report = memory / "reports" / "Report-one.md"
    blob = build_file_bundle_command(
        operation_type="research_bundle",
        memory_id=_MEMORY_ID,
        anchor_path=f"Memories/{_MEMORY_ID}/reports/Report-one.md",
        targets=(
            {
                "path": f"Memories/{_MEMORY_ID}/evidence/Evidence-one.md",
                "content": b"new-evidence",
                "expected_mode": "hash",
                "expected_hash": _digest(b"old-evidence"),
            },
            {
                "path": f"Memories/{_MEMORY_ID}/sources/Source-one.md",
                "content": b"new-source",
                "expected_mode": "hash",
                "expected_hash": _digest(b"old-source"),
            },
            {
                "path": f"Memories/{_MEMORY_ID}/reports/Report-one.md",
                "content": b"new-report",
                "expected_mode": "absent",
            },
        ),
        result={"report_path": f"Memories/{_MEMORY_ID}/reports/Report-one.md"},
    )
    queue = _queue(tmp_path, vault)
    _enqueue(queue, blob, "research_bundle", "research:publish-conflict")
    lease = queue.claim_writer(lease_seconds=30, owner="writer-one")
    assert lease is not None

    def edit_second(name: str) -> None:
        # Sorted order is evidence (0), report anchor (1), source (2). Alter the
        # second non-anchor immediately after evidence was published.
        if name == "after_publish_target:0":
            source.write_bytes(b"obsidian-source")

    terminal = VaultWriter(vault, queue, failpoint=edit_second).run_once(lease)
    assert terminal is not None and terminal.status == "conflict"
    assert evidence.read_bytes() == b"old-evidence"
    assert source.read_bytes() == b"obsidian-source"
    assert not report.exists()
    assert not (vault / "Memories" / ".paperpilot-writer" / "jobs" / terminal.job_id).exists()


def test_rollback_addition_atomically_quarantines_then_restores_foreign(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    memory = vault / "Memories" / _MEMORY_ID
    for name in ("evidence", "sources", "reports"):
        (memory / name).mkdir(parents=True, exist_ok=True)
    evidence = memory / "evidence" / "Evidence-one.md"
    source = memory / "sources" / "Source-one.md"
    source.write_bytes(b"old-source")
    report_path = f"Memories/{_MEMORY_ID}/reports/Report-one.md"
    blob = build_file_bundle_command(
        operation_type="research_bundle",
        memory_id=_MEMORY_ID,
        anchor_path=report_path,
        targets=(
            {
                "path": f"Memories/{_MEMORY_ID}/evidence/Evidence-one.md",
                "content": b"new-evidence",
                "expected_mode": "absent",
            },
            {
                "path": f"Memories/{_MEMORY_ID}/sources/Source-one.md",
                "content": b"new-source",
                "expected_mode": "hash",
                "expected_hash": _digest(b"old-source"),
            },
            {
                "path": report_path,
                "content": b"new-report",
                "expected_mode": "absent",
            },
        ),
        result={"report_path": report_path},
    )
    queue = _queue(tmp_path, vault)
    _enqueue(queue, blob, "research_bundle", "research:unlink-cas")
    lease = queue.claim_writer(lease_seconds=30, owner="writer-one")
    assert lease is not None
    writer = VaultWriter(vault, queue)

    def arm_conflict(name: str) -> None:
        if name == "after_publish_target:0":
            source.write_bytes(b"external-source")
        if name == "after_remove_intent:0":
            replacement = evidence.with_suffix(".external")
            replacement.write_bytes(b"external-evidence-at-remove")
            os.replace(replacement, evidence)

    writer.failpoint = arm_conflict

    terminal = writer.run_once(lease)

    assert terminal is not None and terminal.status == "conflict"
    assert evidence.read_bytes() == b"external-evidence-at-remove"
    assert source.read_bytes() == b"external-source"
    assert not (vault / report_path).exists()


@pytest.mark.parametrize(
    "crashpoint",
    (
        "after_remove_intent:0",
        "after_remove_rename:0",
        "after_remove_check:0",
    ),
)
def test_remove_intent_crash_phases_recover_clean_job_addition(
    tmp_path: Path,
    crashpoint: str,
) -> None:
    vault = tmp_path / "vault"
    memory = vault / "Memories" / _MEMORY_ID
    for name in ("evidence", "sources", "reports"):
        (memory / name).mkdir(parents=True, exist_ok=True)
    evidence = memory / "evidence" / "Evidence-one.md"
    source = memory / "sources" / "Source-one.md"
    source.write_bytes(b"old-source")
    queue = _queue(tmp_path, vault)
    job = _enqueue(
        queue,
        _addition_rollback_blob(),
        "research_bundle",
        f"research:remove-crash:{crashpoint}",
    )
    lease = queue.claim_writer(lease_seconds=30, owner="writer-one")
    assert lease is not None

    def crash(name: str) -> None:
        if name == "after_publish_target:0":
            source.write_bytes(b"external-source")
        if name == crashpoint:
            raise _Crash

    with pytest.raises(_Crash):
        VaultWriter(vault, queue, failpoint=crash).run_once(lease)
    queue.release_writer(lease)

    terminal = VaultWriter(vault, queue).recover(_takeover(queue))[0]

    assert terminal.status == "conflict"
    assert not evidence.exists()
    assert source.read_bytes() == b"external-source"
    assert not (vault / "Memories" / ".paperpilot-writer" / "jobs" / job.job_id).exists()


def test_remove_intent_recovers_crash_after_foreign_no_clobber_restore(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    memory = vault / "Memories" / _MEMORY_ID
    for name in ("evidence", "sources", "reports"):
        (memory / name).mkdir(parents=True, exist_ok=True)
    evidence = memory / "evidence" / "Evidence-one.md"
    source = memory / "sources" / "Source-one.md"
    source.write_bytes(b"old-source")
    queue = _queue(tmp_path, vault)
    job = _enqueue(
        queue,
        _addition_rollback_blob(),
        "research_bundle",
        "research:remove-foreign-restore-crash",
    )
    lease = queue.claim_writer(lease_seconds=30, owner="writer-one")
    assert lease is not None

    def crash(name: str) -> None:
        if name == "after_publish_target:0":
            source.write_bytes(b"external-source")
        if name == "after_remove_intent:0":
            replacement = evidence.with_suffix(".external")
            replacement.write_bytes(b"external-evidence")
            os.replace(replacement, evidence)
        if name == "after_remove_restore:0":
            raise _Crash

    with pytest.raises(_Crash):
        VaultWriter(vault, queue, failpoint=crash).run_once(lease)
    assert evidence.read_bytes() == b"external-evidence"
    queue.release_writer(lease)

    terminal = VaultWriter(vault, queue).recover(_takeover(queue))[0]

    assert terminal.status == "conflict"
    assert evidence.read_bytes() == b"external-evidence"
    assert source.read_bytes() == b"external-source"
    assert not (vault / "Memories" / ".paperpilot-writer" / "jobs" / job.job_id).exists()


def test_remove_restore_preserves_new_canonical_and_quarantined_foreign(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    memory = vault / "Memories" / _MEMORY_ID
    for name in ("evidence", "sources", "reports"):
        (memory / name).mkdir(parents=True, exist_ok=True)
    evidence = memory / "evidence" / "Evidence-one.md"
    source = memory / "sources" / "Source-one.md"
    source.write_bytes(b"old-source")
    queue = _queue(tmp_path, vault)
    job = _enqueue(
        queue,
        _addition_rollback_blob(),
        "research_bundle",
        "research:remove-two-foreign-versions",
    )
    lease = queue.claim_writer(lease_seconds=30, owner="writer-one")
    assert lease is not None

    def external_edits(name: str) -> None:
        if name == "after_publish_target:0":
            source.write_bytes(b"external-source")
        if name == "after_remove_intent:0":
            replacement = evidence.with_suffix(".external")
            replacement.write_bytes(b"external-quarantined")
            os.replace(replacement, evidence)
        if name == "after_remove_check:0":
            evidence.write_bytes(b"external-new-canonical")

    writer = VaultWriter(vault, queue, failpoint=external_edits)
    terminal = writer.run_once(lease)

    assert terminal is not None and terminal.status == "conflict"
    assert terminal.error_code == "vault_conflict_quarantined"
    assert evidence.read_bytes() == b"external-new-canonical"
    artifact = vault / "Memories" / ".paperpilot-writer" / "jobs" / job.job_id / "remove" / "000000.moved"
    assert artifact.read_bytes() == b"external-quarantined"


def test_replace_cas_preserves_latest_across_two_external_edits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    _seed_note_vault(vault)
    queue = _queue(tmp_path, vault)
    job = _enqueue(queue, _note_blob(), "memory_note", "note:repeated-cas")
    lease = queue.claim_writer(lease_seconds=30, owner="writer-one")
    assert lease is not None
    exchanges = 0

    def before_exchange(target: Path) -> None:
        nonlocal exchanges
        if exchanges == 0:
            target.write_bytes(b"external-one\n")
        elif exchanges == 1:
            target.write_bytes(b"external-two\n")
        exchanges += 1

    def windows_exchange(target: Path, replacement: Path, backup: Path) -> None:
        before_exchange(target)
        old = target.read_bytes()
        new = replacement.read_bytes()
        target.write_bytes(new)
        backup.write_bytes(old)
        replacement.unlink()

    def posix_exchange(target: Path, replacement: Path) -> None:
        before_exchange(target)
        old = target.read_bytes()
        new = replacement.read_bytes()
        target.write_bytes(new)
        replacement.write_bytes(old)

    monkeypatch.setattr(vault_writer_module, "_windows_replace_file", windows_exchange)
    monkeypatch.setattr(vault_writer_module, "_atomic_exchange", posix_exchange)

    terminal = VaultWriter(vault, queue).run_once(lease)

    assert terminal is not None and terminal.status == "conflict"
    assert exchanges == 3
    assert (vault / f"Memories/{_MEMORY_ID}/Home.md").read_bytes() == b"external-two\n"
    assert not (vault / f"Memories/{_MEMORY_ID}/notes/Note-one.md").exists()
    assert not (vault / "Memories" / ".paperpilot-writer" / "jobs" / job.job_id).exists()


def test_recovery_inspects_anchor_displaced_after_exchange_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    _seed_note_vault(vault)
    queue = _queue(tmp_path, vault)
    job = _enqueue(queue, _note_blob(), "memory_note", "note:displaced-crash")
    lease = queue.claim_writer(lease_seconds=30, owner="writer-one")
    assert lease is not None
    exchanges = 0

    def edit_once(target: Path) -> None:
        nonlocal exchanges
        if exchanges == 0:
            target.write_bytes(b"external-at-syscall\n")
        exchanges += 1

    _install_exchange_simulation(monkeypatch, edit_once)

    def crash(name: str) -> None:
        if name == "after_replace_exchange:0":
            raise _Crash

    with pytest.raises(_Crash):
        VaultWriter(vault, queue, failpoint=crash).run_once(lease)
    home = vault / f"Memories/{_MEMORY_ID}/Home.md"
    assert home.read_bytes() == b"new-home-with-note\n"
    queue.release_writer(lease)

    terminal = VaultWriter(vault, queue).recover(_takeover(queue))[0]

    assert terminal.status == "conflict"
    assert home.read_bytes() == b"external-at-syscall\n"
    assert not (vault / f"Memories/{_MEMORY_ID}/notes/Note-one.md").exists()
    assert not (vault / "Memories" / ".paperpilot-writer" / "jobs" / job.job_id).exists()


def test_recovery_inspects_non_anchor_displaced_before_report_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    memory = vault / "Memories" / _MEMORY_ID
    for name in ("sources", "reports"):
        (memory / name).mkdir(parents=True, exist_ok=True)
    source = memory / "sources" / "Source-one.md"
    source.write_bytes(b"old-source")
    report_path = f"Memories/{_MEMORY_ID}/reports/Report-one.md"
    blob = build_file_bundle_command(
        operation_type="research_bundle",
        memory_id=_MEMORY_ID,
        anchor_path=report_path,
        targets=(
            {
                "path": f"Memories/{_MEMORY_ID}/sources/Source-one.md",
                "content": b"new-source",
                "expected_mode": "hash",
                "expected_hash": _digest(b"old-source"),
            },
            {
                "path": report_path,
                "content": b"new-report",
                "expected_mode": "absent",
            },
        ),
        result={"report_path": report_path},
    )
    queue = _queue(tmp_path, vault)
    job = _enqueue(queue, blob, "research_bundle", "research:displaced-leaf")
    lease = queue.claim_writer(lease_seconds=30, owner="writer-one")
    assert lease is not None
    exchanges = 0

    def edit_once(target: Path) -> None:
        nonlocal exchanges
        if exchanges == 0:
            target.write_bytes(b"external-source")
        exchanges += 1

    _install_exchange_simulation(monkeypatch, edit_once)

    def crash(name: str) -> None:
        if name == "after_replace_exchange:1":
            raise _Crash

    with pytest.raises(_Crash):
        VaultWriter(vault, queue, failpoint=crash).run_once(lease)
    queue.release_writer(lease)
    terminal = VaultWriter(vault, queue).recover(_takeover(queue))[0]

    assert terminal.status == "conflict"
    assert source.read_bytes() == b"external-source"
    assert not (vault / report_path).exists()
    assert not (vault / "Memories" / ".paperpilot-writer" / "jobs" / job.job_id).exists()


def test_recovery_settles_crash_during_rollback_replace(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    memory = vault / "Memories" / _MEMORY_ID
    for name in ("evidence", "sources", "reports"):
        (memory / name).mkdir(parents=True, exist_ok=True)
    evidence = memory / "evidence" / "Evidence-one.md"
    source = memory / "sources" / "Source-one.md"
    evidence.write_bytes(b"old-evidence")
    source.write_bytes(b"old-source")
    report_path = f"Memories/{_MEMORY_ID}/reports/Report-one.md"
    blob = build_file_bundle_command(
        operation_type="research_bundle",
        memory_id=_MEMORY_ID,
        anchor_path=report_path,
        targets=(
            {
                "path": f"Memories/{_MEMORY_ID}/evidence/Evidence-one.md",
                "content": b"new-evidence",
                "expected_mode": "hash",
                "expected_hash": _digest(b"old-evidence"),
            },
            {
                "path": f"Memories/{_MEMORY_ID}/sources/Source-one.md",
                "content": b"new-source",
                "expected_mode": "hash",
                "expected_hash": _digest(b"old-source"),
            },
            {
                "path": report_path,
                "content": b"new-report",
                "expected_mode": "absent",
            },
        ),
        result={"report_path": report_path},
    )
    queue = _queue(tmp_path, vault)
    _enqueue(queue, blob, "research_bundle", "research:rollback-intent")
    lease = queue.claim_writer(lease_seconds=30, owner="writer-one")
    assert lease is not None
    exchanges = 0

    def crash_on_rollback(name: str) -> None:
        nonlocal exchanges
        if name == "after_replace_exchange:0":
            exchanges += 1
            if exchanges == 2:
                raise _Crash
        if name == "after_publish_target:0":
            source.write_bytes(b"external-source")

    with pytest.raises(_Crash):
        VaultWriter(vault, queue, failpoint=crash_on_rollback).run_once(lease)
    assert evidence.read_bytes() == b"old-evidence"
    queue.release_writer(lease)
    terminal = VaultWriter(vault, queue).recover(_takeover(queue))[0]

    assert terminal.status == "conflict"
    assert evidence.read_bytes() == b"old-evidence"
    assert source.read_bytes() == b"external-source"
    assert not (vault / report_path).exists()


def test_directory_bundle_preserves_empty_directories_and_recovers_after_rename(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    queue = _queue(tmp_path, vault)
    home_path = f"Memories/{_MEMORY_ID}/Home.md"
    blob = build_directory_create_command(
        operation_type="create_memory",
        memory_id=_MEMORY_ID,
        anchor_directory=f"Memories/{_MEMORY_ID}",
        directories=_DIRECTORIES,
        files=({"path": home_path, "content": b"home\n"},),
        input_hashes={"title": _digest(b"Writer Test")},
        result={"memory_id": _MEMORY_ID, "home_path": home_path},
    )
    job = _enqueue(queue, blob, "create_memory", "create:writer-test")
    lease = queue.claim_writer(lease_seconds=30, owner="writer-one")
    assert lease is not None

    def crash(name: str) -> None:
        if name == "after_anchor":
            raise _Crash

    with pytest.raises(_Crash):
        VaultWriter(vault, queue, failpoint=crash).run_once(lease)
    queue.release_writer(lease)
    takeover = _takeover(queue)
    VaultWriter(vault, queue).recover(takeover)
    assert queue.get(job.job_id).status == "succeeded"  # type: ignore[union-attr]
    assert (vault / home_path).read_bytes() == b"home\n"
    for directory in _DIRECTORIES:
        assert (vault / directory).is_dir()


def test_directory_change_after_linearized_is_conflict_not_success(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    queue = _queue(tmp_path, vault)
    home_path = f"Memories/{_MEMORY_ID}/Home.md"
    blob = build_directory_create_command(
        operation_type="create_memory",
        memory_id=_MEMORY_ID,
        anchor_directory=f"Memories/{_MEMORY_ID}",
        directories=_DIRECTORIES,
        files=({"path": home_path, "content": b"home\n"},),
        result={"memory_id": _MEMORY_ID},
    )
    job = _enqueue(queue, blob, "create_memory", "create:after-linearized-edit")
    lease = queue.claim_writer(lease_seconds=30, owner="writer-one")
    assert lease is not None
    home = vault / home_path

    def edit_directory(name: str) -> None:
        if name == "after_linearized":
            home.write_bytes(b"external-home-after-linearized\n")

    terminal = VaultWriter(vault, queue, failpoint=edit_directory).run_once(lease)

    assert terminal is not None and terminal.status == "conflict"
    assert terminal.error_code == "vault_conflict_quarantined"
    assert home.read_bytes() == b"external-home-after-linearized\n"
    assert (vault / "Memories" / ".paperpilot-writer" / "jobs" / job.job_id).is_dir()


@pytest.mark.parametrize("operation_type", ("create_memory", "legacy_copy"))
@pytest.mark.parametrize("failpoint", ("after_tree_mkdir", "after_tree_file:0"))
def test_directory_tree_build_crash_rebuilds_complete_private_tree(
    tmp_path: Path,
    operation_type: str,
    failpoint: str,
) -> None:
    vault = tmp_path / "vault"
    memory_id = f"M-tree-{operation_type.replace('_', '-')}"
    memory_root = f"Memories/{memory_id}"
    directories = tuple(
        f"{memory_root}/{name}"
        for name in (
            "reports",
            "evidence",
            "sources",
            "notes",
            "imports",
            "attachments",
        )
    )
    input_hashes = {}
    if operation_type == "legacy_copy":
        legacy = vault / "reports" / "Legacy.md"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("# legacy\n", encoding="utf-8")
        _, legacy_hash = _legacy_snapshot(vault)
        input_hashes = {"legacy_source": legacy_hash}
    blob = build_directory_create_command(
        operation_type=operation_type,
        memory_id=memory_id,
        anchor_directory=memory_root,
        directories=directories,
        files=({"path": f"{memory_root}/Home.md", "content": b"complete-home\n"},),
        input_hashes=input_hashes,
        result={"memory_id": memory_id},
    )
    scope = f"tree-{operation_type}-{failpoint}"
    queue = _queue(tmp_path, vault, scope=scope)
    job = queue.enqueue(
        idempotency_key=scope,
        operation_type=operation_type,
        memory_id=memory_id,
        command_blob=blob,
        command_hash=canonical_command_hash(blob),
    )
    process = multiprocessing.get_context("spawn").Process(
        target=_process_crash_worker,
        args=(str(vault), queue.db_path, scope, failpoint),
    )
    process.start()
    process.join(20)
    assert process.exitcode == 77
    assert not (vault / memory_root).exists()

    VaultWriter(vault, queue).recover(_takeover(queue))

    assert queue.get(job.job_id).status == "succeeded"  # type: ignore[union-attr]
    assert (vault / memory_root / "Home.md").read_bytes() == b"complete-home\n"
    for directory in directories:
        assert (vault / directory).is_dir()


def test_legacy_source_is_rechecked_immediately_before_directory_anchor(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    legacy = vault / "reports" / "Report-old.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("# legacy old\n", encoding="utf-8")
    _, legacy_hash = _legacy_snapshot(vault)
    target_id = "M-legacy-copy-test"
    target_root = f"Memories/{target_id}"
    directories = tuple(
        f"{target_root}/{name}" for name in ("reports", "evidence", "sources", "notes", "imports", "attachments")
    )
    blob = build_directory_create_command(
        operation_type="legacy_copy",
        memory_id=target_id,
        anchor_directory=target_root,
        directories=directories,
        files=({"path": f"{target_root}/Home.md", "content": b"home\n"},),
        input_hashes={"legacy_source": legacy_hash},
        result={"memory_id": target_id},
    )
    queue = _queue(tmp_path, vault)
    job = queue.enqueue(
        idempotency_key="legacy:copy",
        operation_type="legacy_copy",
        memory_id=target_id,
        command_blob=blob,
        command_hash=canonical_command_hash(blob),
    )
    lease = queue.claim_writer(lease_seconds=30, owner="writer-one")
    assert lease is not None

    def crash(name: str) -> None:
        if name == "after_prepared":
            raise _Crash

    with pytest.raises(_Crash):
        VaultWriter(vault, queue, failpoint=crash).run_once(lease)
    legacy.write_text("# obsidian changed legacy\n", encoding="utf-8")
    queue.release_writer(lease)
    takeover = _takeover(queue)
    recovered = VaultWriter(vault, queue).recover(takeover)
    assert recovered and recovered[0].status == "conflict"
    assert legacy.read_text(encoding="utf-8") == "# obsidian changed legacy\n"
    assert not (vault / target_root).exists()
    assert queue.get(job.job_id).status == "conflict"  # type: ignore[union-attr]


def test_stale_writer_generation_cannot_mutate_after_takeover(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _seed_note_vault(vault)
    queue = _queue(tmp_path, vault)
    _enqueue(queue, _note_blob(), "memory_note", "note:fence")
    old = queue.claim_writer(lease_seconds=30, owner="old-writer")
    assert old is not None
    claimed = queue.claim_next(old, lease_seconds=30)
    assert claimed is not None
    assert queue.release_writer(old)
    new = _takeover(queue)
    with pytest.raises(RuntimeError, match="lease was lost"):
        VaultWriter(vault, queue).execute(claimed, old)
    recovered = VaultWriter(vault, queue).recover(new)
    assert recovered and recovered[0].status == "succeeded"


def test_orphan_incomplete_staging_is_removed_without_touching_vault(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _seed_note_vault(vault)
    queue = _queue(tmp_path, vault)
    orphan = vault / "Memories" / ".paperpilot-writer" / "jobs" / "orphan"
    orphan.mkdir(parents=True)
    (orphan / "stage.tmp").write_bytes(b"private")
    before = (vault / f"Memories/{_MEMORY_ID}/Home.md").read_bytes()
    lease = queue.claim_writer(lease_seconds=30, owner="writer-one")
    assert lease is not None
    VaultWriter(vault, queue).recover(lease)
    assert not orphan.exists()
    assert (vault / f"Memories/{_MEMORY_ID}/Home.md").read_bytes() == before


def test_bounded_recovery_only_executes_selected_snapshot_jobs(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    queue = _queue(tmp_path, vault)
    jobs = []
    for index in range(2):
        memory_id = f"M-bounded-{index}"
        memory_root = f"Memories/{memory_id}"
        directories = tuple(
            f"{memory_root}/{name}"
            for name in (
                "reports",
                "evidence",
                "sources",
                "notes",
                "imports",
                "attachments",
            )
        )
        blob = build_directory_create_command(
            operation_type="create_memory",
            memory_id=memory_id,
            anchor_directory=memory_root,
            directories=directories,
            files=(
                {
                    "path": f"{memory_root}/Home.md",
                    "content": f"home-{index}\n".encode(),
                },
            ),
            result={"memory_id": memory_id},
        )
        jobs.append(
            queue.enqueue(
                idempotency_key=f"bounded:{index}",
                operation_type="create_memory",
                memory_id=memory_id,
                command_blob=blob,
                command_hash=canonical_command_hash(blob),
            )
        )
    orphan = vault / "Memories" / ".paperpilot-writer" / "jobs" / "orphan-bounded"
    orphan.mkdir(parents=True)
    (orphan / "private.tmp").write_bytes(b"private")

    lease = queue.claim_writer(lease_seconds=30, owner="bounded-writer")
    assert lease is not None
    recovered = VaultWriter(vault, queue).recover(
        lease,
        job_ids=(jobs[0].job_id,),
    )

    assert [job.job_id for job in recovered] == [jobs[0].job_id]
    assert queue.get(jobs[0].job_id).status == "succeeded"  # type: ignore[union-attr]
    assert queue.get(jobs[1].job_id).status == "queued"  # type: ignore[union-attr]
    assert (vault / "Memories/M-bounded-0/Home.md").is_file()
    assert not (vault / "Memories/M-bounded-1").exists()
    assert not orphan.exists()


@pytest.mark.parametrize("linked_component", ("private_root", "jobs", "job_root"))
def test_private_writer_links_never_touch_outside_sentinel(
    tmp_path: Path,
    linked_component: str,
) -> None:
    vault = tmp_path / "vault"
    _seed_note_vault(vault)
    queue = _queue(tmp_path, vault)
    job = _enqueue(queue, _note_blob(), "memory_note", f"note:link:{linked_component}")
    outside = tmp_path / f"outside-{linked_component}"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("do-not-touch", encoding="utf-8")
    private_root = vault / "Memories" / ".paperpilot-writer"
    if linked_component == "private_root":
        link = private_root
    elif linked_component == "jobs":
        private_root.mkdir()
        link = private_root / "jobs"
    else:
        (private_root / "jobs").mkdir(parents=True)
        link = private_root / "jobs" / job.job_id
    _make_directory_link(link, outside)

    lease = queue.claim_writer(lease_seconds=30, owner="writer-one")
    assert lease is not None
    terminal = VaultWriter(vault, queue).run_once(lease)

    assert terminal is not None and terminal.status == "conflict"
    assert sentinel.read_text(encoding="utf-8") == "do-not-touch"


def test_directory_anchor_race_with_empty_directory_is_no_replace(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    queue = _queue(tmp_path, vault)
    memory_id = "M-anchor-empty-race"
    memory_root = f"Memories/{memory_id}"
    directories = tuple(
        f"{memory_root}/{name}"
        for name in (
            "reports",
            "evidence",
            "sources",
            "notes",
            "imports",
            "attachments",
        )
    )
    blob = build_directory_create_command(
        operation_type="create_memory",
        memory_id=memory_id,
        anchor_directory=memory_root,
        directories=directories,
        files=({"path": f"{memory_root}/Home.md", "content": b"home\n"},),
        result={"memory_id": memory_id},
    )
    queue.enqueue(
        idempotency_key="anchor-empty-race",
        operation_type="create_memory",
        memory_id=memory_id,
        command_blob=blob,
        command_hash=canonical_command_hash(blob),
    )
    lease = queue.claim_writer(lease_seconds=30, owner="writer-one")
    assert lease is not None
    anchor = vault / memory_root

    def race(name: str) -> None:
        if name == "before_anchor":
            anchor.mkdir(parents=True)

    terminal = VaultWriter(vault, queue, failpoint=race).run_once(lease)

    assert terminal is not None and terminal.status == "conflict"
    assert anchor.is_dir()
    assert list(anchor.iterdir()) == []


def test_dangling_directory_anchor_link_is_never_replaced(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    queue = _queue(tmp_path, vault)
    memory_id = "M-anchor-dangling-link"
    memory_root = f"Memories/{memory_id}"
    anchor = vault / memory_root
    anchor.parent.mkdir(parents=True, exist_ok=True)
    try:
        anchor.symlink_to(tmp_path / "missing-target", target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    directories = tuple(
        f"{memory_root}/{name}"
        for name in (
            "reports",
            "evidence",
            "sources",
            "notes",
            "imports",
            "attachments",
        )
    )
    blob = build_directory_create_command(
        operation_type="create_memory",
        memory_id=memory_id,
        anchor_directory=memory_root,
        directories=directories,
        files=({"path": f"{memory_root}/Home.md", "content": b"home\n"},),
        result={"memory_id": memory_id},
    )
    queue.enqueue(
        idempotency_key="anchor-dangling",
        operation_type="create_memory",
        memory_id=memory_id,
        command_blob=blob,
        command_hash=canonical_command_hash(blob),
    )
    lease = queue.claim_writer(lease_seconds=30, owner="writer-one")
    assert lease is not None

    terminal = VaultWriter(vault, queue).run_once(lease)

    assert terminal is not None and terminal.status == "conflict"
    assert os.path.lexists(anchor)
    assert anchor.is_symlink()


def test_unsafe_poison_job_is_terminal_and_does_not_block_fifo(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _seed_note_vault(vault)
    queue = _queue(tmp_path, vault)
    blob = _note_blob()
    with sqlite3.connect(queue.db_path) as connection:
        connection.execute(
            """
            INSERT INTO vault_write_jobs (
                vault_scope, job_id, idempotency_key, operation_type,
                memory_id, origin_thread_id, command_blob, command_hash,
                status, result_json, error_code, created_at, completed_at,
                lease_owner, lease_generation, lease_until
            ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, 'queued', NULL, NULL,
                      0, NULL, NULL, NULL, NULL)
            """,
            (
                queue.vault_scope,
                "bad/id",
                "poison-unsafe-id",
                "memory_note",
                _MEMORY_ID,
                blob,
                canonical_command_hash(blob),
            ),
        )
    valid = _enqueue(queue, blob, "memory_note", "note:after-poison")
    lease = queue.claim_writer(lease_seconds=30, owner="writer-one")
    assert lease is not None

    poison = VaultWriter(vault, queue).run_once(lease)
    succeeded = VaultWriter(vault, queue).run_once(lease)

    assert poison is not None and poison.status == "failed"
    assert poison.error_code == "invalid_command"
    assert succeeded is not None and succeeded.job_id == valid.job_id
    assert succeeded.status == "succeeded"
