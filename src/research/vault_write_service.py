"""Synchronous product facade for durable single-Writer Vault mutations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from .memory import MarkdownMemoryStore, MemoryWriteConflictError
from .memory_write_plans import (
    MemoryWritePlan,
    build_create_memory_plan,
    build_legacy_copy_plan,
    build_memory_import_plan,
    build_memory_note_plan,
    build_report_review_plan,
    build_research_bundle_plan,
    build_tool_artifact_plan,
    create_memory_request_hash,
    report_review_request_hash,
    research_bundle_request_hash,
    tool_artifact_content,
)
from .models import (
    ExecutionIdentity,
    MemoryDescriptor,
    MemoryImportProposal,
    MemoryManifest,
    MemoryNoteProposal,
    ResearchBrief,
    ResearchResult,
)
from .vault import LEGACY_MEMORY_ID, validate_memory_id
from .vault_write_queue import VaultWriteJob, VaultWriteQueue, VaultWriterLease
from .vault_writer import VaultWriter

__all__ = ["VaultWriteService"]


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class _WriterHeartbeat:
    """Keep the global lease and its one running job alive during sync I/O."""

    def __init__(
        self,
        queue: VaultWriteQueue,
        lease: VaultWriterLease,
        lease_seconds: float,
    ) -> None:
        self.queue = queue
        self.lease = lease
        self.lease_seconds = lease_seconds
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name=f"vault-writer-heartbeat-{lease.generation}",
            daemon=True,
        )

    def _run(self) -> None:
        interval = max(0.01, min(5.0, self.lease_seconds / 3.0))
        while not self.stop.wait(interval):
            try:
                writer = self.queue.renew_writer(
                    self.lease,
                    lease_seconds=self.lease_seconds,
                )
                if writer is None:
                    self.lost.set()
                    return
                running = self.queue.list(status="running")
                if len(running) > 1:
                    self.lost.set()
                    return
                if len(running) == 1:
                    renewed = self.queue.renew_job(
                        running[0].job_id,
                        self.lease,
                        lease_seconds=self.lease_seconds,
                    )
                    if renewed is None:
                        current = self.queue.get(running[0].job_id)
                        if current is None or not current.terminal:
                            self.lost.set()
                            return
            except Exception:
                self.lost.set()
                return

    def __enter__(self) -> _WriterHeartbeat:
        self.thread.start()
        return self

    def verify(self) -> None:
        if self.lost.is_set():
            raise RuntimeError("Vault Writer lease was lost")
        self.queue.assert_fence(self.lease)

    def __exit__(self, *_args: object) -> None:
        self.stop.set()
        self.thread.join(timeout=max(1.0, min(10.0, self.lease_seconds)))


class VaultWriteService:
    """Plan, enqueue, drive, and reconstruct existing Memory Store results.

    Managed writes always use the persistent queue.  The legacy root-only
    ``persist_research(memory_id=None)`` remains a low-level compatibility seam;
    product callers must provide a managed Memory and therefore use the Writer.
    """

    def __init__(
        self,
        memory_store: MarkdownMemoryStore,
        queue: VaultWriteQueue,
        writer: VaultWriter,
        *,
        lease_seconds: float = 60.0,
        coordination_interval_seconds: float = 0.1,
        wait_timeout_seconds: float = 300.0,
        startup_timeout_seconds: float = 60.0,
        legacy_archive_root: str | os.PathLike[str] | None = None,
        allow_legacy_copy_only: bool = False,
    ) -> None:
        if writer.queue is not queue:
            raise ValueError("Vault Writer and service must share one queue")
        if writer.root != memory_store.root:
            raise ValueError("Vault Writer and Memory Store roots do not match")
        self.memory_store = memory_store
        self.queue = queue
        self.writer = writer
        self.lease_seconds = float(lease_seconds)
        self.coordination_interval_seconds = float(coordination_interval_seconds)
        self.wait_timeout_seconds = float(wait_timeout_seconds)
        self.startup_timeout_seconds = float(startup_timeout_seconds)
        self.legacy_archive_root = (
            None if legacy_archive_root is None else Path(legacy_archive_root).resolve(strict=False)
        )
        self.allow_legacy_copy_only = bool(allow_legacy_copy_only)
        if self.lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if self.coordination_interval_seconds <= 0:
            raise ValueError("coordination_interval_seconds must be positive")
        if self.wait_timeout_seconds <= 0:
            raise ValueError("wait_timeout_seconds must be positive")
        if self.startup_timeout_seconds <= 0:
            raise ValueError("startup_timeout_seconds must be positive")

    def _safe_archive_root(self) -> Path:
        root = self.legacy_archive_root
        if root is None:
            raise ValueError("research.legacy_archive_root must be explicitly configured before legacy migration")
        try:
            resolved = root.resolve(strict=True)
        except OSError as exc:
            raise ValueError("configured legacy archive root must already exist") from exc
        if not resolved.is_dir() or resolved.is_symlink():
            raise ValueError("configured legacy archive root must be a real directory")
        vault = self.memory_store.root.resolve(strict=True)
        if resolved == vault or resolved.is_relative_to(vault):
            raise ValueError("legacy archive root must be outside the active Vault")
        current = resolved
        while True:
            if current.is_symlink() or bool(getattr(current.lstat(), "st_file_attributes", 0) & 0x400):
                raise ValueError("legacy archive root cannot traverse a link or reparse point")
            if current == current.parent:
                break
            current = current.parent
        return resolved

    def _legacy_archive_inventory(self) -> dict[str, str | None]:
        inventory: dict[str, str | None] = {}
        root = self.memory_store.root.resolve(strict=True)
        for name in ("reports", "evidence", "sources"):
            directory = root / name
            if not directory.exists():
                continue
            if not directory.is_dir() or directory.is_symlink():
                raise ValueError(f"legacy root is not a safe directory: {name}")
            inventory[f"{name}/"] = None
            for current_root, directories, files in os.walk(directory, followlinks=False):
                current = Path(current_root)
                for child in sorted(directories):
                    path = current / child
                    if path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
                        raise ValueError("legacy archive cannot contain linked entries")
                    inventory[f"{path.relative_to(root).as_posix()}/"] = None
                for child in sorted(files):
                    path = current / child
                    if path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
                        raise ValueError("legacy archive cannot contain linked entries")
                    inventory[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        return dict(sorted(inventory.items()))

    def prepare_legacy_memory_migration(
        self,
        title: str,
        memory_id: str | None = None,
    ) -> dict[str, object]:
        """Add the S3 retirement impact and recoverable archive to W6 preview."""
        if self.legacy_archive_root is None and self.allow_legacy_copy_only:
            return self.memory_store.prepare_legacy_memory_migration(title, memory_id)
        proposal = self.memory_store.prepare_legacy_memory_migration(title, memory_id)
        files = proposal.get("files")
        if not isinstance(files, tuple):
            raise ValueError("legacy migration proposal files are invalid")
        path_mapping = {
            str(item["source_path"]): str(item["target_path"]) for item in files if isinstance(item, Mapping)
        }
        dependencies = self.queue.legacy_dependencies(path_mapping)
        if self.legacy_archive_root is None:
            proposal["retirement"] = {
                "affected_sessions": dependencies["sessions"],
                "affected_manifests": dependencies["manifests"],
                "archive_inventory": self._legacy_archive_inventory(),
                "archive_target": None,
                "blocked_reason": ("research.legacy_archive_root must be explicitly configured before confirmation"),
                "dependency_hash": dependencies["dependency_hash"],
                "path_mapping": path_mapping,
            }
            return proposal
        archive_root = self._safe_archive_root()
        archive_target = (
            archive_root / self.queue.vault_scope / f"{proposal['target_memory_id']}-{proposal['proposal_id']}"
        )
        if archive_target.exists():
            raise FileExistsError(f"legacy archive target already exists: {archive_target}")
        proposal["retirement"] = {
            "archive_target": str(archive_target),
            "archive_inventory": self._legacy_archive_inventory(),
            "affected_sessions": dependencies["sessions"],
            "affected_manifests": dependencies["manifests"],
            "dependency_hash": dependencies["dependency_hash"],
            "path_mapping": path_mapping,
        }
        return proposal

    def resolve_memory_path(self, relative_path: str) -> str:
        return self.queue.resolve_legacy_path(relative_path) or relative_path

    def prepare_legacy_archive_cleanup(self, migration_id: str) -> dict[str, object]:
        """Return the exact external archive deletion list; never delete here."""
        root = self._safe_archive_root()
        target = Path(self.queue.legacy_archive_target(migration_id)).resolve(strict=True)
        scope = (root / self.queue.vault_scope).resolve(strict=False)
        if target.parent != scope or not target.is_dir() or target.is_symlink():
            raise ValueError("legacy archive target is outside its configured scope")
        metadata_path = target / ".paperpilot-archive.json"
        metadata = metadata_path.read_bytes()
        try:
            decoded = json.loads(metadata.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("legacy archive metadata is invalid") from exc
        if not isinstance(decoded, Mapping) or decoded.get("migration_id") != migration_id:
            raise ValueError("legacy archive metadata identity does not match")
        inventory: dict[str, str | None] = {}
        for path in target.rglob("*"):
            relative = path.relative_to(target).as_posix()
            if path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
                raise ValueError("legacy archive cleanup cannot traverse linked entries")
            inventory[relative + ("/" if path.is_dir() else "")] = (
                None if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
            )
        delete_paths = sorted(inventory, reverse=True)
        delete_paths.append("./")
        payload = {
            "archive_target": str(target),
            "delete_paths": delete_paths,
            "metadata_hash": hashlib.sha256(metadata).hexdigest(),
            "inventory": dict(sorted(inventory.items())),
            "migration_id": migration_id,
        }
        token = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        return {**payload, "confirmation_token": token}

    def delete_legacy_archive(
        self,
        migration_id: str,
        *,
        confirmation_token: str,
    ) -> dict[str, object]:
        """Permanently remove only an unchanged archive after a second confirmation."""
        preview = self.prepare_legacy_archive_cleanup(migration_id)
        if confirmation_token != preview["confirmation_token"]:
            raise MemoryWriteConflictError("legacy archive cleanup preview changed")
        target = Path(str(preview["archive_target"]))
        shutil.rmtree(target)
        return {
            "status": "deleted",
            "migration_id": migration_id,
            "archive_target": str(target),
            "deleted_paths": preview["delete_paths"],
        }

    def _claim_writer(self) -> VaultWriterLease | None:
        return self.queue.claim_writer(
            owner=f"service-{uuid.uuid4().hex}",
            lease_seconds=self.lease_seconds,
        )

    def _drive_as_writer(
        self,
        lease: VaultWriterLease,
        *,
        job_ids: tuple[str, ...],
        deadline: float | None,
    ) -> tuple[VaultWriteJob, ...]:
        completed: list[VaultWriteJob] = []
        try:
            with _WriterHeartbeat(self.queue, lease, self.lease_seconds) as heartbeat:
                if not job_ids:
                    heartbeat.verify()
                    completed.extend(self.writer.recover(lease, job_ids=()))
                for job_id in job_ids:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError("Vault Writer drive timed out between jobs")
                    heartbeat.verify()
                    current = self.queue.get(job_id)
                    if current is None:
                        raise RuntimeError("Vault write job disappeared")
                    if current.terminal:
                        continue
                    completed.extend(self.writer.recover(lease, job_ids=(job_id,)))
                    heartbeat.verify()
        except TimeoutError:
            raise
        except Exception as exc:
            raise RuntimeError("Vault Writer execution failed; durable command was retained") from exc
        return tuple(completed)

    def _try_drive(
        self,
        *,
        target_job_id: str | None,
        deadline: float | None = None,
        job_ids: tuple[str, ...] | None = None,
    ) -> tuple[VaultWriteJob, ...] | None:
        if job_ids is None:
            if target_job_id is None:
                job_ids = tuple(job.job_id for job in self.queue.list() if not job.terminal)
            else:
                job_ids = self._target_prefix_snapshot(target_job_id)
        lease = self._claim_writer()
        if lease is None:
            return None
        try:
            return self._drive_as_writer(
                lease,
                job_ids=job_ids,
                deadline=deadline,
            )
        finally:
            self.queue.release_writer(lease)

    def _target_prefix_snapshot(self, target_job_id: str) -> tuple[str, ...]:
        """Freeze the finite nonterminal prefix needed to make target runnable.

        A stale running job blocks every targeted claim even when it sorts after
        the target because an earlier waiter once claimed out of FIFO order. Put
        that unique running job first, followed by queued jobs through target.
        Jobs enqueued after this snapshot are deliberately excluded.
        """
        pending = tuple(job for job in self.queue.list() if not job.terminal)
        try:
            target_index = next(index for index, job in enumerate(pending) if job.job_id == target_job_id)
        except StopIteration:
            target = self.queue.get(target_job_id)
            if target is None:
                raise RuntimeError("Vault write job disappeared") from None
            return () if target.terminal else (target_job_id,)
        prefix = list(pending[: target_index + 1])
        running = [job for job in pending if job.status == "running"]
        if len(running) > 1:
            raise RuntimeError("multiple running Vault write jobs detected")
        if running:
            prefix = [
                running[0],
                *(job for job in prefix if job.job_id != running[0].job_id),
            ]
        return tuple(job.job_id for job in prefix)

    def _await_terminal(self, job_id: str) -> VaultWriteJob:
        deadline = time.monotonic() + self.wait_timeout_seconds
        initial = self.queue.get(job_id)
        if initial is None:
            raise RuntimeError("Vault write job disappeared")
        if initial.terminal:
            return initial
        job_ids = self._target_prefix_snapshot(job_id)
        while True:
            job = self.queue.get(job_id)
            if job is None:
                raise RuntimeError("Vault write job disappeared")
            if job.terminal:
                return job
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Vault write did not converge before timeout: {job_id}")
            self._try_drive(
                target_job_id=job_id,
                deadline=deadline,
                job_ids=job_ids,
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Vault write did not converge before timeout: {job_id}")
            try:
                return self.queue.wait(
                    job_id,
                    timeout=min(self.coordination_interval_seconds, remaining),
                    poll_interval=min(
                        self.queue.poll_interval_seconds,
                        self.coordination_interval_seconds,
                    ),
                )
            except TimeoutError:
                # The current owner may still be healthy.  Re-check until its
                # lease expires, then this process can atomically take over.
                continue

    @staticmethod
    def _successful_result(job: VaultWriteJob) -> dict[str, object]:
        if job.status == "conflict":
            raise MemoryWriteConflictError(f"Vault write conflict: {job.error_code or 'vault_conflict'}")
        if job.status == "failed":
            raise RuntimeError(f"Vault write failed: {job.error_code or 'writer_failure'}")
        if job.status != "succeeded" or job.result is None:
            raise RuntimeError("Vault write did not produce a successful result")
        return job.result

    @staticmethod
    def _persisted_request_hash(job: VaultWriteJob) -> str | None:
        result = job.result
        if result is not None:
            value = result.get("request_hash")
            if isinstance(value, str) and _SHA256.fullmatch(value):
                return value
        if job.command_blob is None:
            return None
        try:
            payload = json.loads(job.command_blob)
            value = payload["input_hashes"]["request"]
        except (KeyError, TypeError, json.JSONDecodeError, UnicodeError):
            return None
        return value if isinstance(value, str) and _SHA256.fullmatch(value) else None

    def _existing_for_plan(
        self,
        plan: MemoryWritePlan,
        *,
        request_hash: str,
    ) -> VaultWriteJob:
        existing = self.queue.get_by_idempotency_key(plan.idempotency_key)
        if existing is None:
            raise RuntimeError("Vault write idempotency collision row disappeared")
        expected_metadata = (
            plan.job_id,
            plan.operation_type,
            plan.memory_id,
            plan.origin_thread_id,
        )
        actual_metadata = (
            existing.job_id,
            existing.operation_type,
            existing.memory_id,
            existing.origin_thread_id,
        )
        if actual_metadata != expected_metadata:
            raise ValueError("Vault write idempotency key collision")
        if existing.status in {"conflict", "failed"}:
            # A terminal error is never a successful reuse. Preserve its public
            # classification even though terminal rows intentionally discarded
            # the opaque command bytes.
            return existing
        if self._persisted_request_hash(existing) != request_hash:
            raise ValueError("Vault write idempotency key collision")
        return existing

    def _submit(
        self,
        plan: MemoryWritePlan,
        *,
        request_hash: str | None = None,
    ) -> dict[str, object]:
        try:
            job = self.queue.enqueue(**plan.enqueue_kwargs())
        except ValueError as exc:
            if request_hash is None or str(exc) != "Vault write idempotency key collision":
                raise
            job = self._existing_for_plan(plan, request_hash=request_hash)
        return self._successful_result(self._await_terminal(job.job_id))

    @staticmethod
    def _manifest(value: Mapping[str, object]) -> MemoryManifest:
        report_path = value.get("report_path")
        evidence_paths = value.get("evidence_paths", [])
        source_paths = value.get("source_paths", [])
        if (
            not isinstance(report_path, str)
            or not isinstance(evidence_paths, list)
            or not all(isinstance(path, str) for path in evidence_paths)
            or not isinstance(source_paths, list)
            or not all(isinstance(path, str) for path in source_paths)
        ):
            raise RuntimeError("Vault Writer returned an invalid research manifest")
        return MemoryManifest(
            report_path=report_path,
            evidence_paths=tuple(evidence_paths),
            source_paths=tuple(source_paths),
        )

    def create_memory(
        self,
        title: str,
        memory_id: str | None = None,
        *,
        origin_thread_id: str | None = None,
    ) -> MemoryDescriptor:
        identity = memory_id or f"M-{uuid.uuid4().hex}"
        validate_memory_id(identity)
        request_hash = create_memory_request_hash(memory_id=identity, title=title)
        plan = build_create_memory_plan(
            memory_id=identity,
            title=title,
            created_at=self.memory_store._timestamp(),
            origin_thread_id=origin_thread_id,
        )
        result = self._submit(plan, request_hash=request_hash)
        if result.get("memory_id") != identity:
            raise RuntimeError("Vault Writer returned an invalid Memory locator")
        return self.memory_store.get_memory(identity)

    def persist_research(
        self,
        brief: ResearchBrief,
        result: ResearchResult,
        identity: ExecutionIdentity,
        *,
        memory_id: str | None = None,
        created_at: str | None = None,
        report_body_markdown: str | None = None,
    ) -> tuple[str, MemoryManifest]:
        if memory_id is None:
            return self.memory_store.persist_research(
                brief, result, identity, report_body_markdown=report_body_markdown
            )
        if memory_id == LEGACY_MEMORY_ID:
            raise ValueError("M-legacy is read-only and cannot accept research output")
        request_hash = research_bundle_request_hash(
            brief,
            result,
            identity,
            memory_id=memory_id,
            report_body_markdown=report_body_markdown,
        )
        plan = build_research_bundle_plan(
            self.memory_store,
            brief,
            result,
            identity,
            memory_id=memory_id,
            created_at=created_at or self.memory_store._timestamp(),
            report_body_markdown=report_body_markdown,
        )
        payload = self._submit(plan, request_hash=request_hash)
        manifest = self._manifest(payload)
        return self.memory_store.read_text(manifest.report_path), manifest

    def persist_tool_artifact(
        self,
        artifact_id: str,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        result: Any,
        origin_thread_id: str,
    ) -> dict[str, object]:
        """Publish one full raw tool result through the fenced Vault Writer."""
        plan = build_tool_artifact_plan(
            artifact_id,
            tool_name=tool_name,
            arguments=arguments,
            result=result,
            origin_thread_id=origin_thread_id,
        )
        expected = tool_artifact_content(
            artifact_id,
            tool_name=tool_name,
            arguments=arguments,
            result=result,
        )
        expected_hash = hashlib.sha256(expected).hexdigest()
        payload = self._submit(plan)
        artifact_path = payload.get("artifact_path")
        if (
            payload.get("artifact_id") != artifact_id
            or not isinstance(artifact_path, str)
            or payload.get("content_hash") != expected_hash
            or payload.get("size_bytes") != len(expected)
        ):
            raise RuntimeError("Vault Writer returned an invalid tool artifact receipt")
        try:
            published = self.memory_store.read_text(artifact_path).encode("utf-8")
        except FileNotFoundError as exc:
            raise RuntimeError("tool artifact disappeared after Writer success") from exc
        if published != expected:
            raise RuntimeError("tool artifact bytes failed post-write verification")
        return dict(payload)

    def replace_report(
        self,
        report_path: str,
        markdown: str,
        *,
        memory_id: str | None = None,
        original_markdown: str | None = None,
        manifest: MemoryManifest | None = None,
        origin_thread_id: str | None = None,
    ) -> None:
        pure = PurePosixPath(str(report_path))
        if len(pure.parts) == 2 and pure.parts[0] == "reports":
            self.memory_store.replace_report(report_path, markdown)
            return
        if (
            pure.as_posix() != report_path
            or len(pure.parts) != 4
            or pure.parts[0] != "Memories"
            or pure.parts[2] != "reports"
        ):
            raise ValueError("managed report_path is invalid")
        selected_memory = pure.parts[1]
        if memory_id is not None and memory_id != selected_memory:
            raise ValueError("report path belongs to a different Memory")
        actual = self.memory_store.read_text(report_path)
        current = actual if original_markdown is None else original_markdown
        if current == markdown and actual == markdown:
            return
        report_manifest = manifest or MemoryManifest(report_path=report_path)
        plan = build_report_review_plan(
            self.memory_store,
            memory_id=selected_memory,
            original_markdown=current,
            revised_markdown=markdown,
            manifest=report_manifest,
            origin_thread_id=origin_thread_id,
        )
        existing = self.queue.get_by_idempotency_key(plan.idempotency_key)
        if existing is None and actual != current:
            raise MemoryWriteConflictError("report changed after review input was captured")
        request_hash = report_review_request_hash(
            memory_id=selected_memory,
            report_path=report_path,
            original_markdown=current,
            revised_markdown=markdown,
            manifest=report_manifest,
        )
        payload = self._submit(plan, request_hash=request_hash)
        revised_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        if (
            payload.get("report_path") != report_path
            or payload.get("request_hash") != request_hash
            or payload.get("revised_hash") != revised_hash
        ):
            raise RuntimeError("Vault Writer returned an invalid report review receipt")
        try:
            published = self.memory_store.read_text(report_path)
        except FileNotFoundError as exc:
            raise MemoryWriteConflictError("report review target disappeared after Writer success") from exc
        if published != markdown:
            raise MemoryWriteConflictError("report changed after the report review was committed")

    def commit_memory_note(
        self,
        proposal: MemoryNoteProposal,
        *,
        origin_thread_id: str | None = None,
    ) -> dict[str, str]:
        value = self._submit(
            build_memory_note_plan(
                self.memory_store,
                proposal,
                origin_thread_id=origin_thread_id,
            )
        )
        required = ("memory_id", "target_path", "home_path", "wikilink")
        if any(not isinstance(value.get(key), str) for key in required):
            raise RuntimeError("Vault Writer returned an invalid note result")
        return {key: str(value[key]) for key in required}

    def commit_memory_import(
        self,
        proposal: MemoryImportProposal,
        *,
        origin_thread_id: str | None = None,
    ) -> dict[str, object]:
        value = self._submit(
            build_memory_import_plan(
                self.memory_store,
                proposal,
                origin_thread_id=origin_thread_id,
            )
        )
        required = (
            "status",
            "memory_id",
            "attachment_path",
            "import_path",
            "note_path",
            "home_path",
            "wikilinks",
        )
        if any(key not in value for key in required):
            raise RuntimeError("Vault Writer returned an invalid import result")
        links = value["wikilinks"]
        if not isinstance(links, list) or not all(isinstance(link, str) for link in links):
            raise RuntimeError("Vault Writer returned invalid import WikiLinks")
        return {**value, "wikilinks": tuple(links)}

    def commit_legacy_memory_migration(
        self,
        proposal: Mapping[str, object],
        *,
        origin_thread_id: str | None = None,
    ) -> MemoryDescriptor:
        plan = build_legacy_copy_plan(
            self.memory_store,
            proposal,
            origin_thread_id=origin_thread_id,
        )
        value = self._submit(plan)
        if value.get("memory_id") != plan.memory_id:
            raise RuntimeError("Vault Writer returned an invalid Memory locator")
        return self.memory_store.get_memory(plan.memory_id)

    def startup_recover(
        self,
        *,
        wait: bool = True,
        timeout: float | None = None,
    ) -> tuple[VaultWriteJob, ...]:
        """Converge the startup queue snapshot before a product process opens.

        ``wait=False`` retains an explicit opportunistic probe for diagnostics.
        Product lifecycles use the default blocking behavior.
        """
        if wait:
            return self.startup_recover_and_drain(timeout=timeout)
        limit = self.startup_timeout_seconds if timeout is None else float(timeout)
        if limit <= 0:
            raise ValueError("startup timeout must be positive")
        deadline = time.monotonic() + limit
        lease = self._claim_writer()
        if lease is None:
            return ()
        try:
            return self._drive_as_writer(
                lease,
                job_ids=tuple(job.job_id for job in self.queue.list() if not job.terminal),
                deadline=deadline,
            )
        finally:
            self.queue.release_writer(lease)

    def startup_recover_and_drain(
        self,
        *,
        timeout: float | None = None,
    ) -> tuple[VaultWriteJob, ...]:
        """Block until jobs present at entry are terminal and journals converge.

        Jobs enqueued after the initial snapshot are not added to the convergence
        set, so concurrent request traffic cannot make startup wait forever.
        """
        limit = self.startup_timeout_seconds if timeout is None else float(timeout)
        if limit <= 0:
            raise ValueError("startup timeout must be positive")
        # Later enqueues are intentionally absent from this finite selection.
        initial_ids = tuple(job.job_id for job in self.queue.list() if not job.terminal)
        deadline = time.monotonic() + limit
        completed: list[VaultWriteJob] = []
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("Vault Writer startup convergence timed out")
            lease = self._claim_writer()
            if lease is None:
                time.sleep(
                    min(
                        self.coordination_interval_seconds,
                        max(0.0, deadline - time.monotonic()),
                    )
                )
                continue
            try:
                # This bounded recovery entrypoint also cleans safe orphan and
                # terminal journals, but can claim only the frozen snapshot IDs.
                completed.extend(
                    self._drive_as_writer(
                        lease,
                        job_ids=initial_ids,
                        deadline=deadline,
                    )
                )
                unresolved: list[str] = []
                for job_id in initial_ids:
                    current = self.queue.get(job_id)
                    if current is None:
                        raise RuntimeError("startup Vault write job disappeared")
                    if not current.terminal:
                        unresolved.append(job_id)
                if unresolved:
                    raise RuntimeError("startup Vault write jobs did not reach terminal state")
                return tuple(completed)
            except Exception as exc:
                if isinstance(exc, (RuntimeError, TimeoutError)):
                    raise
                raise RuntimeError("Vault Writer startup recovery failed; durable commands were retained") from exc
            finally:
                self.queue.release_writer(lease)

    def drain(self) -> tuple[VaultWriteJob, ...]:
        """Drive every currently queued/recoverable job when leadership is free."""
        return (
            self._try_drive(
                target_job_id=None,
                deadline=time.monotonic() + self.wait_timeout_seconds,
            )
            or ()
        )
