"""Persistent, per-Vault control plane for the single Vault Writer.

The queue owns physical write delivery state, not workflow state or published
knowledge.  ``command_blob`` is deliberately opaque here: callers provide a
versioned command envelope and its SHA-256 digest, while the Vault Writer is
the only component that interprets it.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar


__all__ = [
    "VAULT_WRITE_JOB_STATUSES",
    "VAULT_WRITE_OPERATION_TYPES",
    "VaultWriteJob",
    "VaultWriteQueue",
    "VaultWriterLease",
]


VAULT_WRITE_OPERATION_TYPES = frozenset(
    {
        "create_memory",
        "research_bundle",
        "report_review",
        "memory_note",
        "memory_import",
        "legacy_copy",
    }
)
VAULT_WRITE_JOB_STATUSES = frozenset(
    {"queued", "running", "succeeded", "conflict", "failed"}
)
_TERMINAL_STATUSES = frozenset({"succeeded", "conflict", "failed"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_MAX_TEXT_LENGTH = 1024
_MAX_COMMAND_BYTES = 64 * 1024 * 1024
_MAX_RESULT_JSON_BYTES = 64 * 1024
_T = TypeVar("_T")


@dataclass(frozen=True)
class VaultWriteJob:
    vault_scope: str
    job_id: str
    idempotency_key: str
    operation_type: str
    memory_id: str
    origin_thread_id: str | None
    command_blob: bytes | None
    command_hash: str
    status: str
    result_json: str | None
    error_code: str | None
    created_at: float
    completed_at: float | None
    lease_owner: str | None
    lease_generation: int | None
    lease_until: float | None

    @property
    def result(self) -> dict[str, object] | None:
        if self.result_json is None:
            return None
        value = json.loads(self.result_json)
        if not isinstance(value, dict):  # pragma: no cover - guarded on write
            raise ValueError("Vault Writer result is not an object")
        return value

    @property
    def terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES


@dataclass(frozen=True)
class VaultWriterLease:
    vault_scope: str
    owner: str
    generation: int
    lease_until: float


def _required_text(value: object, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > _MAX_TEXT_LENGTH
    ):
        raise ValueError(
            f"{field_name} must be a non-empty trimmed string of at most "
            f"{_MAX_TEXT_LENGTH} characters"
        )
    return value


def _optional_text(value: object | None, *, field_name: str) -> str | None:
    return None if value is None else _required_text(value, field_name=field_name)


def _finite_time(value: object, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite timestamp")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite timestamp") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field_name} must be a finite timestamp")
    return result


def _lease_duration(value: object) -> float:
    duration = _finite_time(value, field_name="lease_seconds")
    if duration <= 0:
        raise ValueError("lease_seconds must be positive")
    return duration


def _command_bytes(value: object) -> bytes:
    if not isinstance(value, bytes) or not value:
        raise ValueError("command_blob must be non-empty bytes")
    if len(value) > _MAX_COMMAND_BYTES:
        raise ValueError("command_blob exceeds the Vault Writer command limit")
    return value


def _command_digest(value: object) -> str:
    digest = _required_text(value, field_name="command_hash")
    if not _SHA256.fullmatch(digest):
        raise ValueError("command_hash must be a lowercase SHA-256 digest")
    return digest


def _result_json(result: Mapping[str, object]) -> str:
    if not isinstance(result, Mapping):
        raise TypeError("Vault Writer result must be a mapping")
    try:
        encoded = json.dumps(
            dict(result),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Vault Writer result must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > _MAX_RESULT_JSON_BYTES:
        raise ValueError("Vault Writer result exceeds the result size limit")
    return encoded


class VaultWriteQueue:
    """Synchronous SQLite queue isolated to one stable Vault scope."""

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        *,
        vault_scope: str,
        busy_timeout_ms: int = 5000,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int):
            raise ValueError("busy_timeout_ms must be a positive integer")
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be a positive integer")
        interval = _finite_time(
            poll_interval_seconds, field_name="poll_interval_seconds"
        )
        if interval <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self.db_path = os.fspath(db_path)
        if not isinstance(self.db_path, str) or not self.db_path.strip():
            raise ValueError("db_path must be a non-empty path")
        self.vault_scope = _required_text(vault_scope, field_name="vault_scope")
        self.busy_timeout_ms = busy_timeout_ms
        self.poll_interval_seconds = interval
        self._lock = threading.RLock()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._ensure_tables()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            timeout=self.busy_timeout_ms / 1000,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return connection

    def _ensure_tables(self) -> None:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS vault_write_jobs (
                        vault_scope TEXT NOT NULL,
                        job_id TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL,
                        operation_type TEXT NOT NULL CHECK (
                            operation_type IN (
                                'create_memory', 'research_bundle',
                                'report_review', 'memory_note',
                                'memory_import', 'legacy_copy'
                            )
                        ),
                        memory_id TEXT NOT NULL,
                        origin_thread_id TEXT,
                        command_blob BLOB,
                        command_hash TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (
                            status IN (
                                'queued', 'running', 'succeeded',
                                'conflict', 'failed'
                            )
                        ),
                        result_json TEXT,
                        error_code TEXT,
                        created_at REAL NOT NULL,
                        completed_at REAL,
                        lease_owner TEXT,
                        lease_generation INTEGER,
                        lease_until REAL,
                        PRIMARY KEY (vault_scope, job_id),
                        UNIQUE (vault_scope, idempotency_key),
                        CHECK (
                            (status IN ('queued', 'running') AND command_blob IS NOT NULL)
                            OR
                            (status IN ('succeeded', 'conflict', 'failed') AND command_blob IS NULL)
                        ),
                        CHECK (
                            (lease_owner IS NULL AND lease_generation IS NULL AND lease_until IS NULL)
                            OR
                            (lease_owner IS NOT NULL AND lease_generation IS NOT NULL AND lease_until IS NOT NULL)
                        )
                    );
                    CREATE INDEX IF NOT EXISTS idx_vault_write_jobs_claim
                        ON vault_write_jobs(vault_scope, status, created_at, job_id);

                    CREATE TABLE IF NOT EXISTS vault_writer_lease (
                        vault_scope TEXT PRIMARY KEY,
                        lease_owner TEXT,
                        lease_generation INTEGER NOT NULL DEFAULT 0
                            CHECK (lease_generation >= 0),
                        lease_until REAL,
                        CHECK (
                            (lease_owner IS NULL AND lease_until IS NULL)
                            OR
                            (lease_owner IS NOT NULL AND lease_until IS NOT NULL)
                        )
                    );

                    CREATE TABLE IF NOT EXISTS legacy_path_mappings (
                        vault_scope TEXT NOT NULL,
                        migration_id TEXT NOT NULL,
                        source_path TEXT NOT NULL,
                        target_path TEXT NOT NULL,
                        memory_id TEXT NOT NULL,
                        archive_target TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        PRIMARY KEY (vault_scope, source_path),
                        UNIQUE (vault_scope, target_path)
                    );
                    CREATE INDEX IF NOT EXISTS idx_legacy_path_mappings_migration
                        ON legacy_path_mappings(vault_scope, migration_id);
                    """
                )
                connection.execute(
                    "INSERT OR IGNORE INTO vault_writer_lease "
                    "(vault_scope, lease_owner, lease_generation, lease_until) "
                    "VALUES (?, NULL, 0, NULL)",
                    (self.vault_scope,),
                )
                connection.commit()
            finally:
                connection.close()

    @staticmethod
    def _legacy_manifest_paths(content: str) -> tuple[str, ...]:
        """Extract canonical path strings without treating Chat as knowledge."""
        try:
            pointer = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            return ()
        if not isinstance(pointer, Mapping):
            return ()
        manifest = pointer.get("manifest")
        if not isinstance(manifest, Mapping):
            return ()
        values: list[str] = []
        report = manifest.get("report_path")
        if isinstance(report, str):
            values.append(report)
        for key in ("evidence_paths", "source_paths"):
            paths = manifest.get(key, ())
            if isinstance(paths, (list, tuple)):
                values.extend(path for path in paths if isinstance(path, str))
        return tuple(values)

    def legacy_dependencies(
        self,
        source_paths: Mapping[str, str],
    ) -> dict[str, object]:
        """Snapshot affected sessions/manifests for an exact S3 preview."""
        mapping = dict(source_paths)
        if not mapping or any(not isinstance(key, str) or not isinstance(value, str) for key, value in mapping.items()):
            raise ValueError("legacy path mapping must contain string paths")
        with self._lock:
            connection = self._connect()
            try:
                session_table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'session_meta'"
                ).fetchone()
                if session_table is None:
                    raise RuntimeError("session_meta must exist before legacy retirement")
                sessions = tuple(
                    str(row[0])
                    for row in connection.execute(
                        "SELECT session_id FROM session_meta WHERE memory_id = ? ORDER BY session_id",
                        ("M-legacy",),
                    ).fetchall()
                )
                manifests: list[dict[str, object]] = []
                message_table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'chat_messages'"
                ).fetchone()
                if message_table is not None:
                    rows = connection.execute(
                        "SELECT session_id, message_id, content FROM chat_messages "
                        "WHERE kind = 'report' ORDER BY session_id, message_id"
                    ).fetchall()
                    for row in rows:
                        paths = self._legacy_manifest_paths(str(row["content"]))
                        affected = tuple(path for path in paths if path in mapping)
                        if affected:
                            manifests.append(
                                {
                                    "session_id": str(row["session_id"]),
                                    "message_id": str(row["message_id"]),
                                    "legacy_paths": list(affected),
                                    "content_hash": hashlib.sha256(
                                        str(row["content"]).encode("utf-8")
                                    ).hexdigest(),
                                }
                            )
                payload = {
                    "sessions": list(sessions),
                    "manifests": manifests,
                }
                digest = hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                return {**payload, "dependency_hash": digest}
            finally:
                connection.close()

    def resolve_legacy_path(self, source_path: str) -> str | None:
        """Resolve one historical pointer after S3 without rewriting Chat rows."""
        if not isinstance(source_path, str) or not source_path:
            raise ValueError("source_path must be a non-empty string")
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT target_path FROM legacy_path_mappings "
                    "WHERE vault_scope = ? AND source_path = ?",
                    (self.vault_scope, source_path),
                ).fetchone()
                return None if row is None else str(row["target_path"])
            finally:
                connection.close()

    def legacy_archive_target(self, migration_id: str) -> str:
        migration = _required_text(migration_id, field_name="migration_id")
        with self._lock:
            connection = self._connect()
            try:
                rows = connection.execute(
                    "SELECT DISTINCT archive_target FROM legacy_path_mappings "
                    "WHERE vault_scope = ? AND migration_id = ?",
                    (self.vault_scope, migration),
                ).fetchall()
                if not rows:
                    raise FileNotFoundError(f"legacy migration does not exist: {migration}")
                if len(rows) != 1:
                    raise RuntimeError("legacy migration has conflicting archive targets")
                return str(rows[0]["archive_target"])
            finally:
                connection.close()

    def legacy_retirement_complete(
        self,
        migration_id: str,
        memory_id: str,
        path_mapping: Mapping[str, str],
    ) -> bool:
        """Return whether mapping and every former legacy session converged."""
        mapping = dict(path_mapping)
        with self._lock:
            connection = self._connect()
            try:
                rows = connection.execute(
                    "SELECT source_path, target_path, memory_id FROM legacy_path_mappings "
                    "WHERE vault_scope = ? AND migration_id = ? ORDER BY source_path",
                    (self.vault_scope, migration_id),
                ).fetchall()
                stored = {str(row["source_path"]): str(row["target_path"]) for row in rows}
                if stored != mapping or any(str(row["memory_id"]) != memory_id for row in rows):
                    return False
                remaining = connection.execute(
                    "SELECT 1 FROM session_meta WHERE memory_id = 'M-legacy' LIMIT 1"
                ).fetchone()
                return remaining is None
            finally:
                connection.close()

    def commit_legacy_switch(
        self,
        job_id: str,
        lease: VaultWriterLease,
        *,
        migration_id: str,
        memory_id: str,
        archive_target: str,
        path_mapping: Mapping[str, str],
        expected_dependency_hash: str,
        publish: Callable[[], _T],
        now: float | None = None,
    ) -> _T:
        """Publish the managed tree and metadata at one fenced switch point.

        SQLite changes stay invisible until the filesystem publication succeeds.
        A process death after the rename but before COMMIT is recovered from the
        immutable command and the already-published tree.
        """
        identity = _required_text(job_id, field_name="job_id")
        migration = _required_text(migration_id, field_name="migration_id")
        memory = _required_text(memory_id, field_name="memory_id")
        archive = _required_text(archive_target, field_name="archive_target")
        digest = _command_digest(expected_dependency_hash)
        mapping = dict(path_mapping)
        if not mapping or any(not isinstance(key, str) or not isinstance(value, str) for key, value in mapping.items()):
            raise ValueError("legacy path mapping must contain string paths")
        current = time.time() if now is None else _finite_time(now, field_name="now")
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._assert_fence_connection(connection, lease, job_id=identity, now=current)
                existing = connection.execute(
                    "SELECT source_path, target_path, memory_id, archive_target "
                    "FROM legacy_path_mappings WHERE vault_scope = ? AND migration_id = ?",
                    (self.vault_scope, migration),
                ).fetchall()
                if existing:
                    stored = {str(row["source_path"]): str(row["target_path"]) for row in existing}
                    if stored != mapping or any(
                        str(row["memory_id"]) != memory or str(row["archive_target"]) != archive
                        for row in existing
                    ):
                        raise RuntimeError("legacy migration mapping collision")
                    connection.execute(
                        "UPDATE session_meta SET memory_id = ?, updated_at = ? WHERE memory_id = 'M-legacy'",
                        (memory, current),
                    )
                    result = publish()
                    connection.commit()
                    return result
                sessions = [
                    str(row[0])
                    for row in connection.execute(
                        "SELECT session_id FROM session_meta WHERE memory_id = ? ORDER BY session_id",
                        ("M-legacy",),
                    ).fetchall()
                ]
                manifests: list[dict[str, object]] = []
                rows = connection.execute(
                    "SELECT session_id, message_id, content FROM chat_messages "
                    "WHERE kind = 'report' ORDER BY session_id, message_id"
                ).fetchall()
                for row in rows:
                    paths = self._legacy_manifest_paths(str(row["content"]))
                    affected = [path for path in paths if path in mapping]
                    if affected:
                        manifests.append(
                            {
                                "session_id": str(row["session_id"]),
                                "message_id": str(row["message_id"]),
                                "legacy_paths": affected,
                                "content_hash": hashlib.sha256(
                                    str(row["content"]).encode("utf-8")
                                ).hexdigest(),
                            }
                        )
                snapshot = {"sessions": sessions, "manifests": manifests}
                observed = hashlib.sha256(
                    json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                if observed != digest:
                    raise RuntimeError("legacy sessions or manifests changed after preview")
                for source, target in sorted(mapping.items()):
                    connection.execute(
                        "INSERT INTO legacy_path_mappings "
                        "(vault_scope, migration_id, source_path, target_path, memory_id, archive_target, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (self.vault_scope, migration, source, target, memory, archive, current),
                    )
                connection.execute(
                    "UPDATE session_meta SET memory_id = ?, updated_at = ? WHERE memory_id = 'M-legacy'",
                    (memory, current),
                )
                result = publish()
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    @staticmethod
    def _job(row: sqlite3.Row) -> VaultWriteJob:
        values = dict(row)
        blob = values.get("command_blob")
        if blob is not None:
            values["command_blob"] = bytes(blob)
        return VaultWriteJob(**values)

    @staticmethod
    def _lease(row: sqlite3.Row) -> VaultWriterLease:
        return VaultWriterLease(
            vault_scope=str(row["vault_scope"]),
            owner=str(row["lease_owner"]),
            generation=int(row["lease_generation"]),
            lease_until=float(row["lease_until"]),
        )

    @staticmethod
    def stable_job_id(vault_scope: str, idempotency_key: str) -> str:
        scope = _required_text(vault_scope, field_name="vault_scope")
        key = _required_text(idempotency_key, field_name="idempotency_key")
        digest = hashlib.sha256(f"{scope}\0{key}".encode("utf-8")).hexdigest()
        return f"VaultJob-{digest}"

    def enqueue(
        self,
        *,
        idempotency_key: str,
        operation_type: str,
        memory_id: str,
        command_blob: bytes,
        command_hash: str,
        origin_thread_id: str | None = None,
        job_id: str | None = None,
        created_at: float | None = None,
    ) -> VaultWriteJob:
        key = _required_text(idempotency_key, field_name="idempotency_key")
        operation = _required_text(operation_type, field_name="operation_type")
        if operation not in VAULT_WRITE_OPERATION_TYPES:
            raise ValueError(f"unsupported Vault write operation: {operation}")
        memory = _required_text(memory_id, field_name="memory_id")
        origin = _optional_text(origin_thread_id, field_name="origin_thread_id")
        blob = _command_bytes(command_blob)
        digest = _command_digest(command_hash)
        if hashlib.sha256(blob).hexdigest() != digest:
            raise ValueError("command_hash does not match command_blob")
        identity = (
            self.stable_job_id(self.vault_scope, key)
            if job_id is None
            else _required_text(job_id, field_name="job_id")
        )
        if not _SAFE_JOB_ID.fullmatch(identity):
            raise ValueError("job_id must be safe for private Writer state")
        created = time.time() if created_at is None else _finite_time(
            created_at, field_name="created_at"
        )

        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM vault_write_jobs "
                    "WHERE vault_scope = ? AND idempotency_key = ?",
                    (self.vault_scope, key),
                ).fetchone()
                if existing is not None:
                    job = self._job(existing)
                    expected = (identity, operation, memory, origin, digest)
                    actual = (
                        job.job_id,
                        job.operation_type,
                        job.memory_id,
                        job.origin_thread_id,
                        job.command_hash,
                    )
                    if actual != expected:
                        raise ValueError("Vault write idempotency key collision")
                    connection.commit()
                    return job

                collision = connection.execute(
                    "SELECT 1 FROM vault_write_jobs "
                    "WHERE vault_scope = ? AND job_id = ?",
                    (self.vault_scope, identity),
                ).fetchone()
                if collision is not None:
                    raise ValueError("Vault write job_id collision")
                connection.execute(
                    """
                    INSERT INTO vault_write_jobs (
                        vault_scope, job_id, idempotency_key, operation_type,
                        memory_id, origin_thread_id, command_blob, command_hash,
                        status, result_json, error_code, created_at, completed_at,
                        lease_owner, lease_generation, lease_until
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', NULL, NULL, ?,
                              NULL, NULL, NULL, NULL)
                    """,
                    (
                        self.vault_scope,
                        identity,
                        key,
                        operation,
                        memory,
                        origin,
                        sqlite3.Binary(blob),
                        digest,
                        created,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM vault_write_jobs "
                    "WHERE vault_scope = ? AND job_id = ?",
                    (self.vault_scope, identity),
                ).fetchone()
                connection.commit()
                if row is None:  # pragma: no cover - SQLite insert invariant
                    raise RuntimeError("Vault write job could not be read")
                return self._job(row)
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def get(self, job_id: str) -> VaultWriteJob | None:
        identity = _required_text(job_id, field_name="job_id")
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT * FROM vault_write_jobs "
                    "WHERE vault_scope = ? AND job_id = ?",
                    (self.vault_scope, identity),
                ).fetchone()
                return None if row is None else self._job(row)
            finally:
                connection.close()

    def get_by_idempotency_key(self, idempotency_key: str) -> VaultWriteJob | None:
        key = _required_text(idempotency_key, field_name="idempotency_key")
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT * FROM vault_write_jobs "
                    "WHERE vault_scope = ? AND idempotency_key = ?",
                    (self.vault_scope, key),
                ).fetchone()
                return None if row is None else self._job(row)
            finally:
                connection.close()

    def list(self, *, status: str | None = None) -> tuple[VaultWriteJob, ...]:
        if status is not None and status not in VAULT_WRITE_JOB_STATUSES:
            raise ValueError(f"unsupported Vault write status: {status}")
        with self._lock:
            connection = self._connect()
            try:
                if status is None:
                    rows = connection.execute(
                        "SELECT * FROM vault_write_jobs WHERE vault_scope = ? "
                        "ORDER BY created_at, job_id",
                        (self.vault_scope,),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        "SELECT * FROM vault_write_jobs "
                        "WHERE vault_scope = ? AND status = ? "
                        "ORDER BY created_at, job_id",
                        (self.vault_scope, status),
                    ).fetchall()
                return tuple(self._job(row) for row in rows)
            finally:
                connection.close()

    def claim_writer(
        self,
        *,
        lease_seconds: float,
        owner: str | None = None,
        now: float | None = None,
    ) -> VaultWriterLease | None:
        duration = _lease_duration(lease_seconds)
        current = time.time() if now is None else _finite_time(now, field_name="now")
        writer = (
            f"writer-{uuid.uuid4().hex}"
            if owner is None
            else _required_text(owner, field_name="owner")
        )
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE vault_writer_lease
                    SET lease_owner = ?,
                        lease_generation = lease_generation + 1,
                        lease_until = ?
                    WHERE vault_scope = ?
                      AND (lease_owner IS NULL OR lease_until <= ?)
                    """,
                    (writer, current + duration, self.vault_scope, current),
                )
                if cursor.rowcount != 1:
                    connection.commit()
                    return None
                row = connection.execute(
                    "SELECT * FROM vault_writer_lease WHERE vault_scope = ?",
                    (self.vault_scope,),
                ).fetchone()
                connection.commit()
                if row is None:  # pragma: no cover - schema invariant
                    raise RuntimeError("Vault Writer lease could not be read")
                return self._lease(row)
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def renew_writer(
        self,
        lease: VaultWriterLease,
        *,
        lease_seconds: float,
        now: float | None = None,
    ) -> VaultWriterLease | None:
        self._validate_lease_scope(lease)
        duration = _lease_duration(lease_seconds)
        current = time.time() if now is None else _finite_time(now, field_name="now")
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE vault_writer_lease SET lease_until = ?
                    WHERE vault_scope = ? AND lease_owner = ?
                      AND lease_generation = ? AND lease_until >= ?
                    """,
                    (
                        current + duration,
                        self.vault_scope,
                        lease.owner,
                        lease.generation,
                        current,
                    ),
                )
                if cursor.rowcount != 1:
                    connection.commit()
                    return None
                row = connection.execute(
                    "SELECT * FROM vault_writer_lease WHERE vault_scope = ?",
                    (self.vault_scope,),
                ).fetchone()
                connection.commit()
                assert row is not None
                return self._lease(row)
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def release_writer(self, lease: VaultWriterLease) -> bool:
        self._validate_lease_scope(lease)
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE vault_writer_lease
                    SET lease_owner = NULL, lease_until = NULL
                    WHERE vault_scope = ? AND lease_owner = ?
                      AND lease_generation = ?
                    """,
                    (self.vault_scope, lease.owner, lease.generation),
                )
                connection.commit()
                return cursor.rowcount == 1
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _validate_lease_scope(self, lease: VaultWriterLease) -> None:
        if not isinstance(lease, VaultWriterLease):
            raise TypeError("lease must be a VaultWriterLease")
        if lease.vault_scope != self.vault_scope:
            raise ValueError("Vault Writer lease belongs to another Vault scope")

    def _assert_fence_connection(
        self,
        connection: sqlite3.Connection,
        lease: VaultWriterLease,
        *,
        job_id: str | None,
        now: float,
    ) -> None:
        self._validate_lease_scope(lease)
        writer = connection.execute(
            "SELECT lease_owner, lease_generation, lease_until "
            "FROM vault_writer_lease WHERE vault_scope = ?",
            (self.vault_scope,),
        ).fetchone()
        if (
            writer is None
            or writer["lease_owner"] != lease.owner
            or int(writer["lease_generation"]) != lease.generation
            or writer["lease_until"] is None
            or float(writer["lease_until"]) < now
        ):
            raise RuntimeError("Vault Writer lease was lost")
        if job_id is None:
            return
        job = connection.execute(
            "SELECT status, lease_owner, lease_generation, lease_until "
            "FROM vault_write_jobs WHERE vault_scope = ? AND job_id = ?",
            (self.vault_scope, job_id),
        ).fetchone()
        if job is None:
            raise FileNotFoundError(f"Vault write job does not exist: {job_id}")
        if (
            job["status"] != "running"
            or job["lease_owner"] != lease.owner
            or int(job["lease_generation"]) != lease.generation
            or job["lease_until"] is None
            or float(job["lease_until"]) < now
        ):
            raise RuntimeError("Vault write job lease was lost")

    def assert_fence(
        self,
        lease: VaultWriterLease,
        *,
        job_id: str | None = None,
        now: float | None = None,
    ) -> None:
        current = time.time() if now is None else _finite_time(now, field_name="now")
        identity = None if job_id is None else _required_text(job_id, field_name="job_id")
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._assert_fence_connection(
                    connection, lease, job_id=identity, now=current
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def run_fenced(
        self,
        job_id: str,
        lease: VaultWriterLease,
        action: Callable[[], _T],
        *,
        now: float | None = None,
    ) -> _T:
        """Run one filesystem linearization step while holding SQLite fencing."""
        if not callable(action):
            raise TypeError("action must be callable")
        identity = _required_text(job_id, field_name="job_id")
        current = time.time() if now is None else _finite_time(now, field_name="now")
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._assert_fence_connection(
                    connection, lease, job_id=identity, now=current
                )
                result = action()
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def claim_next(
        self,
        lease: VaultWriterLease,
        *,
        lease_seconds: float,
        job_id: str | None = None,
        now: float | None = None,
    ) -> VaultWriteJob | None:
        requested = (
            None if job_id is None else _required_text(job_id, field_name="job_id")
        )
        duration = _lease_duration(lease_seconds)
        current = time.time() if now is None else _finite_time(now, field_name="now")
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._assert_fence_connection(
                    connection, lease, job_id=None, now=current
                )
                running = connection.execute(
                    "SELECT * FROM vault_write_jobs "
                    "WHERE vault_scope = ? AND status = 'running' "
                    "ORDER BY created_at, job_id",
                    (self.vault_scope,),
                ).fetchall()
                if len(running) > 1:
                    raise RuntimeError("multiple running Vault write jobs detected")
                if running:
                    row = running[0]
                    if requested is not None and row["job_id"] != requested:
                        connection.commit()
                        return None
                else:
                    if requested is None:
                        row = connection.execute(
                            "SELECT * FROM vault_write_jobs "
                            "WHERE vault_scope = ? AND status = 'queued' "
                            "ORDER BY created_at, job_id LIMIT 1",
                            (self.vault_scope,),
                        ).fetchone()
                    else:
                        row = connection.execute(
                            "SELECT * FROM vault_write_jobs "
                            "WHERE vault_scope = ? AND job_id = ? "
                            "AND status = 'queued'",
                            (self.vault_scope, requested),
                        ).fetchone()
                if row is None:
                    connection.commit()
                    return None
                identity = str(row["job_id"])
                connection.execute(
                    """
                    UPDATE vault_write_jobs
                    SET status = 'running', lease_owner = ?,
                        lease_generation = ?, lease_until = ?
                    WHERE vault_scope = ? AND job_id = ?
                    """,
                    (
                        lease.owner,
                        lease.generation,
                        current + duration,
                        self.vault_scope,
                        identity,
                    ),
                )
                claimed = connection.execute(
                    "SELECT * FROM vault_write_jobs "
                    "WHERE vault_scope = ? AND job_id = ?",
                    (self.vault_scope, identity),
                ).fetchone()
                connection.commit()
                assert claimed is not None
                return self._job(claimed)
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def renew_job(
        self,
        job_id: str,
        lease: VaultWriterLease,
        *,
        lease_seconds: float,
        now: float | None = None,
    ) -> VaultWriteJob | None:
        identity = _required_text(job_id, field_name="job_id")
        duration = _lease_duration(lease_seconds)
        current = time.time() if now is None else _finite_time(now, field_name="now")
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._assert_fence_connection(
                    connection, lease, job_id=identity, now=current
                )
                until = current + duration
                connection.execute(
                    "UPDATE vault_writer_lease SET lease_until = ? "
                    "WHERE vault_scope = ? AND lease_owner = ? "
                    "AND lease_generation = ?",
                    (until, self.vault_scope, lease.owner, lease.generation),
                )
                connection.execute(
                    "UPDATE vault_write_jobs SET lease_until = ? "
                    "WHERE vault_scope = ? AND job_id = ?",
                    (until, self.vault_scope, identity),
                )
                row = connection.execute(
                    "SELECT * FROM vault_write_jobs "
                    "WHERE vault_scope = ? AND job_id = ?",
                    (self.vault_scope, identity),
                ).fetchone()
                connection.commit()
                assert row is not None
                return self._job(row)
            except RuntimeError:
                connection.rollback()
                return None
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _terminal_transition(
        self,
        job_id: str,
        lease: VaultWriterLease,
        *,
        status: str,
        result_json: str | None,
        error_code: str | None,
        now: float | None,
    ) -> VaultWriteJob:
        identity = _required_text(job_id, field_name="job_id")
        completed = time.time() if now is None else _finite_time(now, field_name="now")
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM vault_write_jobs "
                    "WHERE vault_scope = ? AND job_id = ?",
                    (self.vault_scope, identity),
                ).fetchone()
                if existing is None:
                    raise FileNotFoundError(
                        f"Vault write job does not exist: {identity}"
                    )
                current_job = self._job(existing)
                if current_job.terminal:
                    if (
                        current_job.status != status
                        or current_job.result_json != result_json
                        or current_job.error_code != error_code
                    ):
                        raise ValueError("Vault write terminal result collision")
                    connection.commit()
                    return current_job
                self._assert_fence_connection(
                    connection, lease, job_id=identity, now=completed
                )
                connection.execute(
                    """
                    UPDATE vault_write_jobs
                    SET command_blob = NULL, status = ?, result_json = ?,
                        error_code = ?, completed_at = ?, lease_owner = NULL,
                        lease_generation = NULL, lease_until = NULL
                    WHERE vault_scope = ? AND job_id = ?
                    """,
                    (
                        status,
                        result_json,
                        error_code,
                        completed,
                        self.vault_scope,
                        identity,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM vault_write_jobs "
                    "WHERE vault_scope = ? AND job_id = ?",
                    (self.vault_scope, identity),
                ).fetchone()
                connection.commit()
                assert row is not None
                return self._job(row)
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def complete(
        self,
        job_id: str,
        lease: VaultWriterLease,
        result: Mapping[str, object],
        *,
        now: float | None = None,
    ) -> VaultWriteJob:
        return self._terminal_transition(
            job_id,
            lease,
            status="succeeded",
            result_json=_result_json(result),
            error_code=None,
            now=now,
        )

    def conflict(
        self,
        job_id: str,
        lease: VaultWriterLease,
        error_code: str,
        *,
        now: float | None = None,
    ) -> VaultWriteJob:
        code = _required_text(error_code, field_name="error_code")
        if not _SAFE_ERROR_CODE.fullmatch(code):
            raise ValueError("error_code must be a bounded safe code")
        return self._terminal_transition(
            job_id,
            lease,
            status="conflict",
            result_json=None,
            error_code=code,
            now=now,
        )

    def fail(
        self,
        job_id: str,
        lease: VaultWriterLease,
        error_code: str,
        *,
        now: float | None = None,
    ) -> VaultWriteJob:
        code = _required_text(error_code, field_name="error_code")
        if not _SAFE_ERROR_CODE.fullmatch(code):
            raise ValueError("error_code must be a bounded safe code")
        return self._terminal_transition(
            job_id,
            lease,
            status="failed",
            result_json=None,
            error_code=code,
            now=now,
        )

    def wait(
        self,
        job_id: str,
        *,
        timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> VaultWriteJob:
        identity = _required_text(job_id, field_name="job_id")
        if timeout is not None:
            limit = _finite_time(timeout, field_name="timeout")
            if limit < 0:
                raise ValueError("timeout must be non-negative")
        else:
            limit = None
        interval = self.poll_interval_seconds if poll_interval is None else _finite_time(
            poll_interval, field_name="poll_interval"
        )
        if interval <= 0:
            raise ValueError("poll_interval must be positive")
        deadline = None if limit is None else time.monotonic() + limit
        while True:
            job = self.get(identity)
            if job is None:
                raise FileNotFoundError(f"Vault write job does not exist: {identity}")
            if job.terminal:
                return job
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Vault write job did not finish: {identity}")
                time.sleep(min(interval, remaining))
            else:
                time.sleep(interval)
