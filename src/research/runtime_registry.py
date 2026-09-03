"""Thin SQLite control plane for locating and leasing LangGraph workflows.

The registry deliberately does not store workflow state.  Briefs, proposals,
answers, results, errors, Markdown, and attachment bytes belong exclusively to
the LangGraph checkpoint identified by ``thread_id``.
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
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


__all__ = ["OutboxEvent", "RuntimeRegistry", "WorkflowRecord"]


_WORKFLOW_TYPES = frozenset(
    {"research", "memory_note", "memory_import", "legacy_migration"}
)
_EVENT_PAYLOAD_FIELDS = {
    "confirmed": frozenset(),
    "completed": frozenset(),
    "failed": frozenset({"code"}),
    "cancelled": frozenset({"reason"}),
    "expired": frozenset(),
}
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


@dataclass(frozen=True)
class WorkflowRecord:
    task_id: str
    thread_id: str
    session_id: str
    memory_id: str | None
    workflow_type: str
    created_at: float
    expires_at: float | None
    lease_owner: str | None
    lease_until: float | None


@dataclass(frozen=True)
class OutboxEvent:
    event_id: str
    thread_id: str
    sequence: int
    event_type: str
    payload_json: str

    @property
    def payload(self) -> dict[str, str]:
        value = json.loads(self.payload_json)
        if not isinstance(value, dict):  # pragma: no cover - guarded on write
            raise ValueError("runtime outbox payload is not an object")
        return {str(key): str(item) for key, item in value.items()}


def _required_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty trimmed string")
    return value


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


def _optional_time(value: object | None, *, field_name: str) -> float | None:
    return None if value is None else _finite_time(value, field_name=field_name)


def _safe_payload(event_type: str, payload: Mapping[str, object] | None) -> str:
    allowed = _EVENT_PAYLOAD_FIELDS[event_type]
    values = {} if payload is None else dict(payload)
    if set(values) - allowed:
        raise ValueError(f"{event_type} outbox payload contains non-minimal fields")
    normalized: dict[str, str] = {}
    for key, value in values.items():
        if not isinstance(value, str) or not _SAFE_CODE.fullmatch(value):
            raise ValueError(f"outbox payload field {key!r} must be a safe event code")
        normalized[key] = value
    return json.dumps(normalized, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _default_event_id(thread_id: str, event_type: str) -> str:
    digest = hashlib.sha256(f"{thread_id}\0{event_type}".encode("utf-8")).hexdigest()
    return f"RuntimeEvent-{digest}"


class RuntimeRegistry:
    """Synchronous SQLite registry with no duplicated workflow state."""

    def __init__(self, db_path: str | os.PathLike[str], *, busy_timeout_ms: int = 5000) -> None:
        if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int):
            raise ValueError("busy_timeout_ms must be a positive integer")
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be a positive integer")
        self.db_path = os.fspath(db_path)
        if not isinstance(self.db_path, str) or not self.db_path.strip():
            raise ValueError("db_path must be a non-empty path")
        self.busy_timeout_ms = busy_timeout_ms
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
                session_table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'session_meta'"
                ).fetchone()
                if session_table is None:
                    raise RuntimeError(
                        "session_meta must be initialized before RuntimeRegistry"
                    )
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS runtime_workflows (
                        task_id TEXT PRIMARY KEY,
                        thread_id TEXT NOT NULL UNIQUE,
                        session_id TEXT NOT NULL,
                        memory_id TEXT,
                        workflow_type TEXT NOT NULL CHECK (
                            workflow_type IN (
                                'research', 'memory_note',
                                'memory_import', 'legacy_migration'
                            )
                        ),
                        created_at REAL NOT NULL,
                        expires_at REAL,
                        lease_owner TEXT,
                        lease_until REAL,
                        CHECK ((lease_owner IS NULL) = (lease_until IS NULL)),
                        CHECK (workflow_type = 'research' OR memory_id IS NOT NULL),
                        FOREIGN KEY (session_id)
                            REFERENCES session_meta(session_id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_runtime_workflows_session
                        ON runtime_workflows(session_id, created_at);
                    CREATE INDEX IF NOT EXISTS idx_runtime_workflows_expiry
                        ON runtime_workflows(expires_at);
                    CREATE INDEX IF NOT EXISTS idx_runtime_workflows_lease
                        ON runtime_workflows(lease_until);

                    CREATE TABLE IF NOT EXISTS runtime_outbox (
                        event_id TEXT PRIMARY KEY,
                        thread_id TEXT NOT NULL,
                        sequence INTEGER NOT NULL CHECK (sequence > 0),
                        event_type TEXT NOT NULL CHECK (
                            event_type IN (
                                'confirmed', 'completed', 'failed',
                                'cancelled', 'expired'
                            )
                        ),
                        payload_json TEXT NOT NULL,
                        UNIQUE (thread_id, sequence),
                        FOREIGN KEY (thread_id)
                            REFERENCES runtime_workflows(thread_id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_runtime_outbox_thread
                        ON runtime_outbox(thread_id, sequence);
                    """
                )
                columns = connection.execute(
                    "PRAGMA table_info(runtime_workflows)"
                ).fetchall()
                memory_column = next(
                    (row for row in columns if row[1] == "memory_id"),
                    None,
                )
                if memory_column is not None and int(memory_column[3]) == 1:
                    connection.commit()
                    self._migrate_nullable_research_memory(connection)
                connection.commit()
            finally:
                connection.close()

    @staticmethod
    def _migrate_nullable_research_memory(connection: sqlite3.Connection) -> None:
        """Widen only the research locator identity from Memory-required to optional."""

        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                DROP INDEX IF EXISTS idx_runtime_outbox_thread;
                DROP INDEX IF EXISTS idx_runtime_workflows_session;
                DROP INDEX IF EXISTS idx_runtime_workflows_expiry;
                DROP INDEX IF EXISTS idx_runtime_workflows_lease;

                ALTER TABLE runtime_outbox RENAME TO runtime_outbox_notnull_memory;
                ALTER TABLE runtime_workflows RENAME TO runtime_workflows_notnull_memory;

                CREATE TABLE runtime_workflows (
                    task_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    memory_id TEXT,
                    workflow_type TEXT NOT NULL CHECK (
                        workflow_type IN (
                            'research', 'memory_note',
                            'memory_import', 'legacy_migration'
                        )
                    ),
                    created_at REAL NOT NULL,
                    expires_at REAL,
                    lease_owner TEXT,
                    lease_until REAL,
                    CHECK ((lease_owner IS NULL) = (lease_until IS NULL)),
                    CHECK (workflow_type = 'research' OR memory_id IS NOT NULL),
                    FOREIGN KEY (session_id)
                        REFERENCES session_meta(session_id) ON DELETE CASCADE
                );
                INSERT INTO runtime_workflows
                SELECT * FROM runtime_workflows_notnull_memory;

                CREATE TABLE runtime_outbox (
                    event_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL CHECK (sequence > 0),
                    event_type TEXT NOT NULL CHECK (
                        event_type IN (
                            'confirmed', 'completed', 'failed',
                            'cancelled', 'expired'
                        )
                    ),
                    payload_json TEXT NOT NULL,
                    UNIQUE (thread_id, sequence),
                    FOREIGN KEY (thread_id)
                        REFERENCES runtime_workflows(thread_id) ON DELETE CASCADE
                );
                INSERT INTO runtime_outbox
                SELECT * FROM runtime_outbox_notnull_memory;

                DROP TABLE runtime_outbox_notnull_memory;
                DROP TABLE runtime_workflows_notnull_memory;

                CREATE INDEX idx_runtime_workflows_session
                    ON runtime_workflows(session_id, created_at);
                CREATE INDEX idx_runtime_workflows_expiry
                    ON runtime_workflows(expires_at);
                CREATE INDEX idx_runtime_workflows_lease
                    ON runtime_workflows(lease_until);
                CREATE INDEX idx_runtime_outbox_thread
                    ON runtime_outbox(thread_id, sequence);
                COMMIT;
                """
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _workflow(row: sqlite3.Row) -> WorkflowRecord:
        return WorkflowRecord(**dict(row))

    @staticmethod
    def _event(row: sqlite3.Row) -> OutboxEvent:
        return OutboxEvent(**dict(row))

    def register(
        self,
        *,
        task_id: str,
        thread_id: str,
        session_id: str,
        memory_id: str | None,
        workflow_type: str,
        created_at: float | None = None,
        expires_at: float | None = None,
    ) -> WorkflowRecord:
        task_id = _required_text(task_id, field_name="task_id")
        thread_id = _required_text(thread_id, field_name="thread_id")
        session_id = _required_text(session_id, field_name="session_id")
        workflow_type = _required_text(workflow_type, field_name="workflow_type")
        if workflow_type not in _WORKFLOW_TYPES:
            raise ValueError(f"unsupported workflow_type: {workflow_type}")
        if memory_id is not None:
            memory_id = _required_text(memory_id, field_name="memory_id")
        elif workflow_type != "research":
            raise ValueError("memory_id is required for non-research workflows")
        created = time.time() if created_at is None else _finite_time(
            created_at, field_name="created_at"
        )
        expires = _optional_time(expires_at, field_name="expires_at")
        if expires is not None and expires < created:
            raise ValueError("expires_at cannot be earlier than created_at")

        identity = (task_id, thread_id, session_id, memory_id, workflow_type)
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                session = connection.execute(
                    "SELECT memory_id FROM session_meta WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if session is None:
                    raise ValueError(f"session does not exist: {session_id}")
                bound_memory = session["memory_id"]
                if bound_memory != memory_id and bound_memory is not None:
                    raise ValueError("workflow memory does not match the session binding")

                existing = connection.execute(
                    "SELECT * FROM runtime_workflows "
                    "WHERE task_id = ? OR thread_id = ?",
                    (task_id, thread_id),
                ).fetchone()
                if existing is not None:
                    existing_record = self._workflow(existing)
                    existing_identity = (
                        existing_record.task_id,
                        existing_record.thread_id,
                        existing_record.session_id,
                        existing_record.memory_id,
                        existing_record.workflow_type,
                    )
                    if existing_identity != identity:
                        raise ValueError("runtime workflow identity collision")
                    connection.commit()
                    return existing_record

                connection.execute(
                    """
                    INSERT INTO runtime_workflows (
                        task_id, thread_id, session_id, memory_id,
                        workflow_type, created_at, expires_at,
                        lease_owner, lease_until
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                    """,
                    (*identity, created, expires),
                )
                row = connection.execute(
                    "SELECT * FROM runtime_workflows WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                connection.commit()
                if row is None:  # pragma: no cover - SQLite insert invariant
                    raise RuntimeError("registered workflow could not be read")
                return self._workflow(row)
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def bind_memory(
        self,
        task_id: str,
        memory_id: str,
        *,
        lease_token: str | None = None,
    ) -> WorkflowRecord:
        """Atomically bind one unbound research locator and its session."""

        task_id = _required_text(task_id, field_name="task_id")
        memory_id = _required_text(memory_id, field_name="memory_id")
        if lease_token is not None:
            lease_token = _required_text(lease_token, field_name="lease_token")
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM runtime_workflows WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if row is None:
                    raise ValueError(f"runtime workflow does not exist: {task_id}")
                record = self._workflow(row)
                if record.workflow_type != "research":
                    raise ValueError("only research workflows may bind Memory late")
                if lease_token is not None and record.lease_owner != lease_token:
                    raise ValueError("workflow lease does not authorize Memory binding")
                if record.memory_id is not None:
                    if record.memory_id != memory_id:
                        raise ValueError("workflow is already bound to a different Memory")
                    connection.commit()
                    return record

                session = connection.execute(
                    "SELECT memory_id FROM session_meta WHERE session_id = ?",
                    (record.session_id,),
                ).fetchone()
                if session is None:
                    raise ValueError(f"session does not exist: {record.session_id}")
                bound_memory = session["memory_id"]
                if bound_memory is not None and bound_memory != memory_id:
                    raise ValueError("workflow memory does not match the session binding")
                if bound_memory is None:
                    connection.execute(
                        "UPDATE session_meta SET memory_id = ?, updated_at = ? "
                        "WHERE session_id = ?",
                        (memory_id, time.time(), record.session_id),
                    )
                connection.execute(
                    "UPDATE runtime_workflows SET memory_id = ? WHERE task_id = ?",
                    (memory_id, task_id),
                )
                updated = connection.execute(
                    "SELECT * FROM runtime_workflows WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                connection.commit()
                if updated is None:  # pragma: no cover - SQLite update invariant
                    raise RuntimeError("bound workflow could not be read")
                return self._workflow(updated)
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def get(self, task_id: str) -> WorkflowRecord | None:
        task_id = _required_text(task_id, field_name="task_id")
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT * FROM runtime_workflows WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                return None if row is None else self._workflow(row)
            finally:
                connection.close()

    def get_by_thread(self, thread_id: str) -> WorkflowRecord | None:
        thread_id = _required_text(thread_id, field_name="thread_id")
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT * FROM runtime_workflows WHERE thread_id = ?",
                    (thread_id,),
                ).fetchone()
                return None if row is None else self._workflow(row)
            finally:
                connection.close()

    def list(self, *, session_id: str | None = None) -> tuple[WorkflowRecord, ...]:
        if session_id is not None:
            session_id = _required_text(session_id, field_name="session_id")
        with self._lock:
            connection = self._connect()
            try:
                if session_id is None:
                    rows = connection.execute(
                        "SELECT * FROM runtime_workflows "
                        "ORDER BY created_at, task_id"
                    ).fetchall()
                else:
                    rows = connection.execute(
                        "SELECT * FROM runtime_workflows WHERE session_id = ? "
                        "ORDER BY created_at, task_id",
                        (session_id,),
                    ).fetchall()
                return tuple(self._workflow(row) for row in rows)
            finally:
                connection.close()

    def set_expires_at(self, task_id: str, expires_at: float | None) -> bool:
        task_id = _required_text(task_id, field_name="task_id")
        expires = _optional_time(expires_at, field_name="expires_at")
        with self._lock:
            connection = self._connect()
            try:
                cursor = connection.execute(
                    "UPDATE runtime_workflows SET expires_at = ? WHERE task_id = ?",
                    (expires, task_id),
                )
                connection.commit()
                return cursor.rowcount == 1
            finally:
                connection.close()

    def delete(self, task_id: str) -> bool:
        task_id = _required_text(task_id, field_name="task_id")
        with self._lock:
            connection = self._connect()
            try:
                cursor = connection.execute(
                    "DELETE FROM runtime_workflows WHERE task_id = ?",
                    (task_id,),
                )
                connection.commit()
                return cursor.rowcount == 1
            finally:
                connection.close()

    def delete_for_session(self, session_id: str) -> int:
        session_id = _required_text(session_id, field_name="session_id")
        with self._lock:
            connection = self._connect()
            try:
                cursor = connection.execute(
                    "DELETE FROM runtime_workflows WHERE session_id = ?",
                    (session_id,),
                )
                connection.commit()
                return max(0, cursor.rowcount)
            finally:
                connection.close()

    def claim_lease(
        self,
        task_id: str,
        *,
        lease_seconds: float,
        now: float | None = None,
    ) -> str | None:
        task_id = _required_text(task_id, field_name="task_id")
        duration = _finite_time(lease_seconds, field_name="lease_seconds")
        if duration <= 0:
            raise ValueError("lease_seconds must be positive")
        current = time.time() if now is None else _finite_time(now, field_name="now")
        token = f"lease-{uuid.uuid4().hex}"
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE runtime_workflows
                    SET lease_owner = ?, lease_until = ?
                    WHERE task_id = ?
                      AND (lease_owner IS NULL OR lease_until <= ?)
                    """,
                    (token, current + duration, task_id, current),
                )
                connection.commit()
                return token if cursor.rowcount == 1 else None
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def renew_lease(
        self,
        task_id: str,
        lease_token: str,
        *,
        lease_seconds: float,
        now: float | None = None,
    ) -> bool:
        task_id = _required_text(task_id, field_name="task_id")
        lease_token = _required_text(lease_token, field_name="lease_token")
        duration = _finite_time(lease_seconds, field_name="lease_seconds")
        if duration <= 0:
            raise ValueError("lease_seconds must be positive")
        current = time.time() if now is None else _finite_time(now, field_name="now")
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE runtime_workflows
                    SET lease_until = ?
                    WHERE task_id = ? AND lease_owner = ? AND lease_until >= ?
                    """,
                    (current + duration, task_id, lease_token, current),
                )
                connection.commit()
                return cursor.rowcount == 1
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def release_lease(self, task_id: str, lease_token: str) -> bool:
        task_id = _required_text(task_id, field_name="task_id")
        lease_token = _required_text(lease_token, field_name="lease_token")
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE runtime_workflows
                    SET lease_owner = NULL, lease_until = NULL
                    WHERE task_id = ? AND lease_owner = ?
                    """,
                    (task_id, lease_token),
                )
                connection.commit()
                return cursor.rowcount == 1
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def append_event(
        self,
        thread_id: str,
        event_type: str,
        payload: Mapping[str, object] | None = None,
        *,
        event_id: str | None = None,
    ) -> OutboxEvent:
        thread_id = _required_text(thread_id, field_name="thread_id")
        event_type = _required_text(event_type, field_name="event_type")
        if event_type not in _EVENT_PAYLOAD_FIELDS:
            raise ValueError(f"unsupported event_type: {event_type}")
        safe_json = _safe_payload(event_type, payload)
        identity = (
            _default_event_id(thread_id, event_type)
            if event_id is None
            else _required_text(event_id, field_name="event_id")
        )

        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM runtime_outbox WHERE event_id = ?",
                    (identity,),
                ).fetchone()
                if existing is not None:
                    event = self._event(existing)
                    if (
                        event.thread_id != thread_id
                        or event.event_type != event_type
                        or event.payload_json != safe_json
                    ):
                        raise ValueError("runtime outbox event_id collision")
                    connection.commit()
                    return event

                workflow = connection.execute(
                    "SELECT 1 FROM runtime_workflows WHERE thread_id = ?",
                    (thread_id,),
                ).fetchone()
                if workflow is None:
                    raise ValueError(f"workflow thread does not exist: {thread_id}")
                row = connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 "
                    "FROM runtime_outbox WHERE thread_id = ?",
                    (thread_id,),
                ).fetchone()
                sequence = int(row[0])
                connection.execute(
                    """
                    INSERT INTO runtime_outbox (
                        event_id, thread_id, sequence, event_type, payload_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (identity, thread_id, sequence, event_type, safe_json),
                )
                created = connection.execute(
                    "SELECT * FROM runtime_outbox WHERE event_id = ?",
                    (identity,),
                ).fetchone()
                connection.commit()
                if created is None:  # pragma: no cover - SQLite insert invariant
                    raise RuntimeError("outbox event could not be read")
                return self._event(created)
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def list_events(
        self,
        thread_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[OutboxEvent, ...]:
        thread_id = _required_text(thread_id, field_name="thread_id")
        if isinstance(after_sequence, bool) or not isinstance(after_sequence, int):
            raise ValueError("after_sequence must be a non-negative integer")
        if after_sequence < 0:
            raise ValueError("after_sequence must be a non-negative integer")
        with self._lock:
            connection = self._connect()
            try:
                rows = connection.execute(
                    "SELECT * FROM runtime_outbox "
                    "WHERE thread_id = ? AND sequence > ? ORDER BY sequence",
                    (thread_id, after_sequence),
                ).fetchall()
                return tuple(self._event(row) for row in rows)
            finally:
                connection.close()
