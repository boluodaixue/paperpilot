"""Persistent passive coordination board for homogeneous research agents.

The board records plans, scoped assignments, duplicate work claims, shared
documents, and cross-scope evidence signals.  It never plans, judges research
sufficiency, or reassigns work on its own; those decisions remain with the
agents using it.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..tools.evidence_acquisition import canonicalize_source_url


_SPACE = re.compile(r"\s+")
_ASSIGNMENT_STATUSES = {
    "unclaimed",
    "claimed",
    "queued",
    "researching",
    "waiting_children",
    "completed",
    "blocked",
    "failed",
    "cancelled_due_to_budget",
}
_WORK_STATUSES = {"running", "waiting_children", "completed", "partial", "failed"}
_SIGNAL_STATUSES = {"open", "consumed"}


@dataclass(frozen=True)
class BoardClaim:
    """Result of atomically claiming one assignment, query, or source."""

    acquired: bool
    reason: str
    owner_thread_id: str = ""
    status: str = ""
    artifact_ids: tuple[str, ...] = ()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def normalize_query(query: str) -> str:
    """Normalize a query for exact-work coordination without semantic guessing."""

    return _SPACE.sub(" ", str(query or "").strip()).casefold()


def query_fingerprint(query: str) -> str:
    return hashlib.sha256(normalize_query(query).encode("utf-8")).hexdigest()


def normalize_scope_signature(scope: str, objective: str = "") -> str:
    """Return a stable scope label suitable for recursive assignment dedupe."""

    value = unicodedata.normalize("NFKC", str(scope or objective or ""))
    value = _SPACE.sub(" ", value.strip()).casefold()
    return value


class ResearchBlackboard:
    """SQLite-backed, append-audited coordination state for one or more runs."""

    def __init__(self, database: str | Path) -> None:
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.setup()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database,
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def setup(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS research_board_runs (
                    run_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    requirements_json TEXT NOT NULL,
                    outline_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_board_assignments (
                    run_id TEXT NOT NULL,
                    requirement_id TEXT NOT NULL,
                    owner_thread_id TEXT NOT NULL DEFAULT '',
                    parent_thread_id TEXT NOT NULL DEFAULT '',
                    objective TEXT NOT NULL,
                    status TEXT NOT NULL,
                    lease_until REAL NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (run_id, requirement_id),
                    FOREIGN KEY (run_id) REFERENCES research_board_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS research_board_requirements (
                    run_id TEXT NOT NULL,
                    requirement_id TEXT NOT NULL,
                    description TEXT NOT NULL,
                    required INTEGER NOT NULL DEFAULT 1,
                    requires_external_evidence INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'unsupported',
                    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
                    rationale TEXT NOT NULL DEFAULT '',
                    remaining_gap TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (run_id, requirement_id),
                    FOREIGN KEY (run_id) REFERENCES research_board_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS research_board_assignment_nodes (
                    run_id TEXT NOT NULL,
                    assignment_id TEXT NOT NULL,
                    parent_assignment_id TEXT NOT NULL DEFAULT '',
                    owner_thread_id TEXT NOT NULL DEFAULT '',
                    parent_thread_id TEXT NOT NULL DEFAULT '',
                    requirement_ids_json TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    scope_signature TEXT NOT NULL,
                    reasons_json TEXT NOT NULL DEFAULT '[]',
                    depth INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    lease_until REAL NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (run_id, assignment_id),
                    FOREIGN KEY (run_id) REFERENCES research_board_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS research_board_queries (
                    run_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    query TEXT NOT NULL,
                    requirement_id TEXT NOT NULL,
                    owner_thread_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    artifact_ids_json TEXT NOT NULL DEFAULT '[]',
                    lease_until REAL NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (run_id, fingerprint),
                    FOREIGN KEY (run_id) REFERENCES research_board_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS research_board_sources (
                    run_id TEXT NOT NULL,
                    canonical_url TEXT NOT NULL,
                    owner_thread_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    artifact_id TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    lease_until REAL NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (run_id, canonical_url),
                    FOREIGN KEY (run_id) REFERENCES research_board_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS research_board_signals (
                    run_id TEXT NOT NULL,
                    signal_id TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    discovered_by TEXT NOT NULL,
                    target_requirement_id TEXT NOT NULL,
                    parent_thread_id TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    consumed_by TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (run_id, signal_id),
                    FOREIGN KEY (run_id) REFERENCES research_board_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS research_board_agents (
                    run_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    parent_thread_id TEXT NOT NULL DEFAULT '',
                    depth INTEGER NOT NULL,
                    requirement_ids_json TEXT NOT NULL,
                    current_action TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (run_id, thread_id),
                    FOREIGN KEY (run_id) REFERENCES research_board_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS research_board_budget_pools (
                    run_id TEXT PRIMARY KEY,
                    total_tokens INTEGER NOT NULL,
                    protected_tokens INTEGER NOT NULL,
                    allocated_tokens INTEGER NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES research_board_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS research_board_budget_leases (
                    run_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    parent_thread_id TEXT NOT NULL DEFAULT '',
                    granted_tokens INTEGER NOT NULL,
                    used_tokens INTEGER NOT NULL DEFAULT 0,
                    max_tokens INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (run_id, thread_id),
                    FOREIGN KEY (run_id) REFERENCES research_board_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS research_board_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    actor_thread_id TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_board_evidence (
                    run_id TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    assignment_id TEXT NOT NULL DEFAULT '',
                    parent_assignment_id TEXT NOT NULL DEFAULT '',
                    requirement_id TEXT NOT NULL DEFAULT '',
                    owner_thread_id TEXT NOT NULL DEFAULT '',
                    source_ref TEXT NOT NULL DEFAULT '',
                    locator TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    PRIMARY KEY (run_id, evidence_id),
                    FOREIGN KEY (run_id) REFERENCES research_board_runs(run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_research_board_events_run
                    ON research_board_events(run_id, event_id);
                CREATE INDEX IF NOT EXISTS idx_research_board_signals_target
                    ON research_board_signals(run_id, target_requirement_id, status);
                CREATE INDEX IF NOT EXISTS idx_research_board_assignment_parent
                    ON research_board_assignment_nodes(run_id, parent_assignment_id, depth);
                CREATE INDEX IF NOT EXISTS idx_research_board_assignment_scope
                    ON research_board_assignment_nodes(run_id, parent_assignment_id, scope_signature);
                CREATE INDEX IF NOT EXISTS idx_research_board_evidence_assignment
                    ON research_board_evidence(run_id, assignment_id);
                """
            )
            # Forward-only additive migration for blackboards created before
            # assignment lineage existed. Existing query/source rows remain valid.
            for table, column, definition in (
                ("research_board_queries", "assignment_id", "TEXT NOT NULL DEFAULT ''"),
                ("research_board_queries", "parent_assignment_id", "TEXT NOT NULL DEFAULT ''"),
                ("research_board_sources", "assignment_id", "TEXT NOT NULL DEFAULT ''"),
                ("research_board_sources", "parent_assignment_id", "TEXT NOT NULL DEFAULT ''"),
                ("research_board_agents", "assignment_id", "TEXT NOT NULL DEFAULT ''"),
                ("research_board_agents", "parent_assignment_id", "TEXT NOT NULL DEFAULT ''"),
                ("research_board_assignment_nodes", "created_at", "REAL NOT NULL DEFAULT 0"),
                (
                    "research_board_requirements",
                    "requires_external_evidence",
                    "INTEGER NOT NULL DEFAULT 1",
                ),
            ):
                columns = {
                    row["name"]
                    for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if column not in columns:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def ensure_budget_pool(
        self,
        run_id: str,
        *,
        total_tokens: int,
        protected_tokens: int,
        now: float | None = None,
    ) -> None:
        """Create one immutable global token pool for refundable child leases."""

        total = max(0, int(total_tokens))
        protected = max(0, min(total, int(protected_tokens)))
        timestamp = time.time() if now is None else float(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM research_board_budget_pools WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if row is not None:
                    if (
                        int(row["total_tokens"]) != total
                        or int(row["protected_tokens"]) != protected
                    ):
                        raise ValueError("research budget pool does not match persisted run")
                    connection.commit()
                    return
                connection.execute(
                    """
                    INSERT INTO research_board_budget_pools
                        (run_id, total_tokens, protected_tokens, allocated_tokens,
                         version, updated_at)
                    VALUES (?, ?, ?, 0, 1, ?)
                    """,
                    (run_id, total, protected, timestamp),
                )
                self._event(
                    connection,
                    run_id,
                    "budget_pool_created",
                    "",
                    {
                        "total_tokens": total,
                        "protected_tokens": protected,
                    },
                    timestamp,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def grant_budget_lease(
        self,
        run_id: str,
        *,
        thread_id: str,
        parent_thread_id: str,
        requested_tokens: int,
        max_tokens: int,
        now: float | None = None,
    ) -> int:
        """Grant a resumable initial child lease from the unprotected pool."""

        requested = max(0, int(requested_tokens))
        maximum = max(requested, int(max_tokens))
        timestamp = time.time() if now is None else float(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT * FROM research_board_budget_leases
                    WHERE run_id = ? AND thread_id = ?
                    """,
                    (run_id, thread_id),
                ).fetchone()
                if existing is not None and existing["status"] == "active":
                    connection.commit()
                    return int(existing["granted_tokens"])
                pool = connection.execute(
                    "SELECT * FROM research_board_budget_pools WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if pool is None:
                    raise KeyError("research budget pool is not initialized")
                available = max(
                    0,
                    int(pool["total_tokens"])
                    - int(pool["protected_tokens"])
                    - int(pool["allocated_tokens"]),
                )
                granted = min(requested, maximum, available)
                if granted <= 0:
                    raise RuntimeError("research budget pool cannot fund a child lease")
                connection.execute(
                    """
                    INSERT INTO research_board_budget_leases
                        (run_id, thread_id, parent_thread_id, granted_tokens,
                         used_tokens, max_tokens, status, version, updated_at)
                    VALUES (?, ?, ?, ?, 0, ?, 'active', 1, ?)
                    ON CONFLICT(run_id, thread_id) DO UPDATE SET
                        parent_thread_id = excluded.parent_thread_id,
                        granted_tokens = excluded.granted_tokens,
                        used_tokens = 0,
                        max_tokens = excluded.max_tokens,
                        status = 'active',
                        version = research_board_budget_leases.version + 1,
                        updated_at = excluded.updated_at
                    """,
                    (
                        run_id,
                        thread_id,
                        parent_thread_id,
                        granted,
                        maximum,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    UPDATE research_board_budget_pools
                    SET allocated_tokens = allocated_tokens + ?,
                        version = version + 1, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (granted, timestamp, run_id),
                )
                self._event(
                    connection,
                    run_id,
                    "budget_lease_granted",
                    thread_id,
                    {"granted_tokens": granted, "max_tokens": maximum},
                    timestamp,
                )
                connection.commit()
                return granted
            except Exception:
                connection.rollback()
                raise

    def request_budget_topup(
        self,
        run_id: str,
        *,
        thread_id: str,
        used_tokens: int,
        requested_tokens: int,
        now: float | None = None,
    ) -> int:
        """Increase one active lease while preserving global protected tokens."""

        used = max(0, int(used_tokens))
        requested = max(0, int(requested_tokens))
        timestamp = time.time() if now is None else float(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                lease = connection.execute(
                    """
                    SELECT * FROM research_board_budget_leases
                    WHERE run_id = ? AND thread_id = ?
                    """,
                    (run_id, thread_id),
                ).fetchone()
                if lease is None or lease["status"] != "active":
                    raise KeyError("active research budget lease is unavailable")
                current = int(lease["granted_tokens"])
                maximum = int(lease["max_tokens"])
                pool = connection.execute(
                    "SELECT * FROM research_board_budget_pools WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if pool is None:
                    raise KeyError("research budget pool is unavailable")
                available = max(
                    0,
                    int(pool["total_tokens"])
                    - int(pool["protected_tokens"])
                    - int(pool["allocated_tokens"]),
                )
                added = min(requested, max(0, maximum - current), available)
                new_grant = current + added
                connection.execute(
                    """
                    UPDATE research_board_budget_leases
                    SET granted_tokens = ?, used_tokens = ?, version = version + 1,
                        updated_at = ?
                    WHERE run_id = ? AND thread_id = ?
                    """,
                    (new_grant, used, timestamp, run_id, thread_id),
                )
                if added:
                    connection.execute(
                        """
                        UPDATE research_board_budget_pools
                        SET allocated_tokens = allocated_tokens + ?,
                            version = version + 1, updated_at = ?
                        WHERE run_id = ?
                        """,
                        (added, timestamp, run_id),
                    )
                self._event(
                    connection,
                    run_id,
                    "budget_lease_topped_up" if added else "budget_topup_unavailable",
                    thread_id,
                    {
                        "used_tokens": used,
                        "added_tokens": added,
                        "granted_tokens": new_grant,
                    },
                    timestamp,
                )
                connection.commit()
                return new_grant
            except Exception:
                connection.rollback()
                raise

    def release_budget_lease(
        self,
        run_id: str,
        *,
        thread_id: str,
        used_tokens: int,
        now: float | None = None,
    ) -> int:
        """Return unused child capacity; repeated release is idempotent."""

        used = max(0, int(used_tokens))
        timestamp = time.time() if now is None else float(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                lease = connection.execute(
                    """
                    SELECT * FROM research_board_budget_leases
                    WHERE run_id = ? AND thread_id = ?
                    """,
                    (run_id, thread_id),
                ).fetchone()
                if lease is None:
                    connection.commit()
                    return 0
                if lease["status"] == "released":
                    connection.commit()
                    return 0
                granted = int(lease["granted_tokens"])
                charged = min(granted, used)
                returned = max(0, granted - charged)
                connection.execute(
                    """
                    UPDATE research_board_budget_leases
                    SET used_tokens = ?, status = 'released',
                        version = version + 1, updated_at = ?
                    WHERE run_id = ? AND thread_id = ?
                    """,
                    (charged, timestamp, run_id, thread_id),
                )
                connection.execute(
                    """
                    UPDATE research_board_budget_pools
                    SET allocated_tokens = MAX(0, allocated_tokens - ?),
                        version = version + 1, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (returned, timestamp, run_id),
                )
                self._event(
                    connection,
                    run_id,
                    "budget_lease_released",
                    thread_id,
                    {"used_tokens": charged, "returned_tokens": returned},
                    timestamp,
                )
                connection.commit()
                return returned
            except Exception:
                connection.rollback()
                raise

    def budget_lease(self, run_id: str, thread_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT thread_id, parent_thread_id, granted_tokens, used_tokens,
                       max_tokens, status, version
                FROM research_board_budget_leases
                WHERE run_id = ? AND thread_id = ?
                """,
                (run_id, thread_id),
            ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        run_id: str,
        kind: str,
        actor_thread_id: str,
        payload: Mapping[str, Any],
        now: float,
    ) -> None:
        connection.execute(
            """
            INSERT INTO research_board_events
                (run_id, kind, actor_thread_id, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, kind, actor_thread_id, _json(dict(payload)), now),
        )

    def ensure_plan(
        self,
        run_id: str,
        *,
        plan_id: str,
        objective: str,
        requirements: Iterable[Mapping[str, Any]],
        report_outline: Iterable[str] = (),
        now: float | None = None,
    ) -> None:
        """Create one immutable plan snapshot, rejecting cross-run drift."""

        timestamp = time.time() if now is None else float(now)
        requirements_json = _json(tuple(dict(item) for item in requirements))
        outline_json = _json(tuple(str(item) for item in report_outline))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM research_board_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                expected = (plan_id, objective, requirements_json, outline_json)
                if row is not None:
                    actual = (
                        row["plan_id"],
                        row["objective"],
                        row["requirements_json"],
                        row["outline_json"],
                    )
                    if actual != expected:
                        raise ValueError("research blackboard plan does not match persisted run")
                else:
                    connection.execute(
                        """
                        INSERT INTO research_board_runs
                            (run_id, plan_id, objective, requirements_json, outline_json,
                             created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            plan_id,
                            objective,
                            requirements_json,
                            outline_json,
                            timestamp,
                            timestamp,
                        ),
                    )
                    self._event(
                        connection,
                        run_id,
                        "plan_created",
                        "",
                        {"plan_id": plan_id},
                        timestamp,
                    )
                for requirement in json.loads(requirements_json):
                    requirement_id = str(requirement.get("requirement_id") or "").strip()
                    description = str(requirement.get("description") or "").strip()
                    if not requirement_id or not description:
                        raise ValueError("plan requirements need requirement_id and description")
                    connection.execute(
                        """
                        INSERT INTO research_board_requirements
                            (run_id, requirement_id, description, required,
                             requires_external_evidence, status,
                             evidence_ids_json, rationale, remaining_gap, version, updated_at)
                        VALUES (?, ?, ?, ?, ?, 'unsupported', '[]', '', '', 1, ?)
                        ON CONFLICT(run_id, requirement_id) DO UPDATE SET
                            description = excluded.description,
                            required = excluded.required,
                            requires_external_evidence = excluded.requires_external_evidence,
                            updated_at = excluded.updated_at
                        """,
                        (
                            run_id,
                            requirement_id,
                            description,
                            1 if bool(requirement.get("required", True)) else 0,
                            1 if bool(requirement.get("requires_external_evidence", True)) else 0,
                            timestamp,
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def register_assignment_batch(
        self,
        run_id: str,
        assignments: Iterable[Mapping[str, Any]],
        *,
        actor_thread_id: str,
        lease_seconds: float,
        now: float | None = None,
    ) -> None:
        """Atomically publish all sibling assignments from one accepted fork."""

        timestamp = time.time() if now is None else float(now)
        lease_until = timestamp + max(0.0, float(lease_seconds))
        items = tuple(dict(item) for item in assignments)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for item in items:
                    requirement_id = str(item.get("requirement_id") or "").strip()
                    owner = str(item.get("owner_thread_id") or "").strip()
                    objective = str(item.get("objective") or "").strip()
                    parent = str(item.get("parent_thread_id") or actor_thread_id).strip()
                    status = str(item.get("status") or ("claimed" if owner else "unclaimed"))
                    if not requirement_id or not objective:
                        raise ValueError("assignment requires requirement_id and objective")
                    if status not in _ASSIGNMENT_STATUSES:
                        raise ValueError(f"invalid assignment status: {status}")
                    row = connection.execute(
                        """
                        SELECT owner_thread_id, status, lease_until
                        FROM research_board_assignments
                        WHERE run_id = ? AND requirement_id = ?
                        """,
                        (run_id, requirement_id),
                    ).fetchone()
                    if (
                        row is not None
                        and row["owner_thread_id"]
                        and row["owner_thread_id"] != owner
                        and row["owner_thread_id"] != actor_thread_id
                        and row["status"] not in {"blocked", "failed", "unclaimed"}
                        and float(row["lease_until"]) > timestamp
                    ):
                        raise ValueError(
                            f"active assignment already owns requirement {requirement_id}"
                        )
                    connection.execute(
                        """
                        INSERT INTO research_board_assignments
                            (run_id, requirement_id, owner_thread_id, parent_thread_id,
                             objective, status, lease_until, version, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                        ON CONFLICT(run_id, requirement_id) DO UPDATE SET
                            owner_thread_id = excluded.owner_thread_id,
                            parent_thread_id = excluded.parent_thread_id,
                            objective = excluded.objective,
                            status = excluded.status,
                            lease_until = excluded.lease_until,
                            version = research_board_assignments.version + 1,
                            updated_at = excluded.updated_at
                        """,
                        (
                            run_id,
                            requirement_id,
                            owner,
                            parent,
                            objective,
                            status,
                            lease_until if owner else 0.0,
                            timestamp,
                        ),
                    )
                self._event(
                    connection,
                    run_id,
                    "assignment_batch_registered",
                    actor_thread_id,
                    {"requirements": [item.get("requirement_id") for item in items]},
                    timestamp,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def ensure_root_assignment(
        self,
        run_id: str,
        *,
        assignment_id: str,
        owner_thread_id: str,
        objective: str,
        requirement_ids: Iterable[str],
        now: float | None = None,
    ) -> None:
        """Create the root execution node without assigning requirement ownership."""

        timestamp = time.time() if now is None else float(now)
        requirements = tuple(dict.fromkeys(str(item).strip() for item in requirement_ids if str(item).strip()))
        scope = normalize_scope_signature("root", objective)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT * FROM research_board_assignment_nodes
                    WHERE run_id = ? AND assignment_id = ?
                    """,
                    (run_id, assignment_id),
                ).fetchone()
                if row is not None:
                    expected = (owner_thread_id, objective, _json(requirements), 0)
                    actual = (
                        row["owner_thread_id"], row["objective"],
                        row["requirement_ids_json"], int(row["depth"]),
                    )
                    if actual != expected:
                        raise ValueError("root assignment does not match persisted run")
                    connection.commit()
                    return
                connection.execute(
                    """
                    INSERT INTO research_board_assignment_nodes
                        (run_id, assignment_id, parent_assignment_id, owner_thread_id,
                         parent_thread_id, requirement_ids_json, objective,
                         scope_signature, reasons_json, depth, status, lease_until,
                         version, created_at, updated_at)
                    VALUES (?, ?, '', ?, '', ?, ?, ?, '[]', 0, 'researching', 0, 1, ?, ?)
                    """,
                    (
                        run_id, assignment_id, owner_thread_id, _json(requirements),
                        objective, scope, timestamp, timestamp,
                    ),
                )
                self._event(
                    connection, run_id, "root_assignment_created", owner_thread_id,
                    {"assignment_id": assignment_id, "requirement_ids": requirements},
                    timestamp,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def register_assignment_nodes(
        self,
        run_id: str,
        assignments: Iterable[Mapping[str, Any]],
        *,
        actor_thread_id: str,
        lease_seconds: float,
        max_total_assignments: int | None = None,
        max_children_per_parent: int | None = None,
        max_depth: int | None = None,
        now: float | None = None,
    ) -> dict[str, BoardClaim]:
        """Register recursive child scopes while allowing distinct scopes per requirement."""

        timestamp = time.time() if now is None else float(now)
        lease_until = timestamp + max(0.0, float(lease_seconds))
        items = tuple(dict(item) for item in assignments)
        outcomes: dict[str, BoardClaim] = {}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                known_requirements = {
                    row["requirement_id"]
                    for row in connection.execute(
                        "SELECT requirement_id FROM research_board_requirements WHERE run_id = ?",
                        (run_id,),
                    ).fetchall()
                }
                assignment_count = int(connection.execute(
                    """
                    SELECT COUNT(*) FROM research_board_assignment_nodes
                    WHERE run_id = ?
                    """,
                    (run_id,),
                ).fetchone()[0])
                for item in items:
                    assignment_id = str(item.get("assignment_id") or "").strip()
                    parent_assignment_id = str(item.get("parent_assignment_id") or "").strip()
                    owner = str(item.get("owner_thread_id") or "").strip()
                    parent_thread = str(item.get("parent_thread_id") or actor_thread_id).strip()
                    objective = str(item.get("objective") or "").strip()
                    requirement_ids = tuple(sorted(dict.fromkeys(
                        str(value).strip()
                        for value in item.get("requirement_ids", ())
                        if str(value).strip()
                    )))
                    scope = normalize_scope_signature(
                        str(item.get("scope_signature") or ""), objective
                    )
                    reasons = tuple(dict.fromkeys(
                        str(value).strip() for value in item.get("reasons", ())
                        if str(value).strip()
                    ))
                    status = str(item.get("status") or "claimed")
                    if not assignment_id or not parent_assignment_id or not owner or not objective:
                        raise ValueError(
                            "assignment node requires assignment_id, parent_assignment_id, owner and objective"
                        )
                    if not requirement_ids or set(requirement_ids) - known_requirements:
                        raise ValueError("assignment node references unknown requirements")
                    if status not in _ASSIGNMENT_STATUSES:
                        raise ValueError(f"invalid assignment status: {status}")
                    parent = connection.execute(
                        """
                        SELECT * FROM research_board_assignment_nodes
                        WHERE run_id = ? AND assignment_id = ?
                        """,
                        (run_id, parent_assignment_id),
                    ).fetchone()
                    if parent is None:
                        outcomes[assignment_id] = BoardClaim(False, "unknown_parent_assignment")
                        continue
                    if parent["owner_thread_id"] != actor_thread_id:
                        outcomes[assignment_id] = BoardClaim(
                            False, "parent_not_owned", parent["owner_thread_id"], parent["status"]
                        )
                        continue
                    parent_requirements = set(json.loads(parent["requirement_ids_json"] or "[]"))
                    if not set(requirement_ids).issubset(parent_requirements):
                        outcomes[assignment_id] = BoardClaim(False, "outside_parent_requirements")
                        continue
                    depth = int(parent["depth"]) + 1
                    if max_depth is not None and depth > max(0, int(max_depth)):
                        outcomes[assignment_id] = BoardClaim(False, "fork_depth_limit_reached")
                        continue
                    existing = connection.execute(
                        """
                        SELECT * FROM research_board_assignment_nodes
                        WHERE run_id = ? AND assignment_id = ?
                        """,
                        (run_id, assignment_id),
                    ).fetchone()
                    if existing is not None:
                        if existing["owner_thread_id"] == owner:
                            outcomes[assignment_id] = BoardClaim(
                                True, "owner_resume", owner, existing["status"]
                            )
                        else:
                            outcomes[assignment_id] = BoardClaim(
                                False, "owned", existing["owner_thread_id"], existing["status"]
                            )
                        continue
                    if (
                        max_total_assignments is not None
                        and assignment_count >= max(1, int(max_total_assignments))
                    ):
                        outcomes[assignment_id] = BoardClaim(
                            False, "global_thread_limit_reached"
                        )
                        continue

                    child_count = int(connection.execute(
                        """
                        SELECT COUNT(*) FROM research_board_assignment_nodes
                        WHERE run_id = ? AND parent_assignment_id = ?
                        """,
                        (run_id, parent_assignment_id),
                    ).fetchone()[0])
                    if (
                        max_children_per_parent is not None
                        and child_count >= max(0, int(max_children_per_parent))
                    ):
                        outcomes[assignment_id] = BoardClaim(
                            False, "parent_child_limit_reached"
                        )
                        continue

                    conflict_reason = ""
                    conflicts = connection.execute(
                        """
                        SELECT assignment_id, owner_thread_id, requirement_ids_json,
                               objective, scope_signature, status
                        FROM research_board_assignment_nodes
                        WHERE run_id = ? AND parent_assignment_id = ?
                        """,
                        (run_id, parent_assignment_id),
                    ).fetchall()
                    for sibling in conflicts:
                        sibling_requirements = set(json.loads(sibling["requirement_ids_json"] or "[]"))
                        if (
                            set(requirement_ids) == sibling_requirements
                            and scope == normalize_scope_signature(sibling["scope_signature"])
                            and normalize_scope_signature(objective)
                            == normalize_scope_signature(sibling["objective"])
                        ):
                            conflict_reason = "duplicate_sibling_scope"
                            outcomes[assignment_id] = BoardClaim(
                                False, conflict_reason, sibling["owner_thread_id"], sibling["status"]
                            )
                            break
                    if conflict_reason:
                        continue
                    connection.execute(
                        """
                        INSERT INTO research_board_assignment_nodes
                            (run_id, assignment_id, parent_assignment_id, owner_thread_id,
                         parent_thread_id, requirement_ids_json, objective,
                         scope_signature, reasons_json, depth, status, lease_until,
                         version, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                        """,
                        (
                            run_id, assignment_id, parent_assignment_id, owner,
                            parent_thread, _json(requirement_ids), objective, scope,
                            _json(reasons), depth, status,
                            lease_until if status in {"claimed", "researching"} else 0.0,
                            timestamp,
                            timestamp,
                        ),
                    )
                    outcomes[assignment_id] = BoardClaim(True, "acquired", owner, status)
                    assignment_count += 1

                for assignment_id, claim in outcomes.items():
                    self._event(
                        connection,
                        run_id,
                        "assignment_node_registered" if claim.acquired else "assignment_node_rejected",
                        actor_thread_id,
                        {"assignment_id": assignment_id, "reason": claim.reason},
                        timestamp,
                    )
                connection.commit()
                return outcomes
            except Exception:
                connection.rollback()
                raise

    def update_assignment_node(
        self,
        run_id: str,
        assignment_id: str,
        *,
        owner_thread_id: str,
        status: str,
        lease_seconds: float = 0.0,
        now: float | None = None,
    ) -> None:
        """Update only one execution node; never completes a whole requirement."""

        if status not in _ASSIGNMENT_STATUSES:
            raise ValueError(f"invalid assignment status: {status}")
        timestamp = time.time() if now is None else float(now)
        lease_until = timestamp + max(0.0, float(lease_seconds))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT owner_thread_id FROM research_board_assignment_nodes
                    WHERE run_id = ? AND assignment_id = ?
                    """,
                    (run_id, assignment_id),
                ).fetchone()
                if row is None:
                    raise KeyError(f"unknown assignment node: {assignment_id}")
                if row["owner_thread_id"] != owner_thread_id:
                    raise PermissionError("only the assignment node owner may update it")
                connection.execute(
                    """
                    UPDATE research_board_assignment_nodes
                    SET status = ?, lease_until = ?, version = version + 1, updated_at = ?
                    WHERE run_id = ? AND assignment_id = ?
                    """,
                    (
                        status,
                        lease_until if status in {"claimed", "researching"} else 0.0,
                        timestamp, run_id, assignment_id,
                    ),
                )
                self._event(
                    connection, run_id, "assignment_node_updated", owner_thread_id,
                    {"assignment_id": assignment_id, "status": status}, timestamp,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def assignment_node(self, run_id: str, assignment_id: str) -> dict[str, Any] | None:
        """Read one durable scheduler/ownership record."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT assignment_id, parent_assignment_id, owner_thread_id,
                       parent_thread_id, requirement_ids_json, objective,
                       scope_signature, reasons_json, depth, status, lease_until,
                       version, created_at, updated_at
                FROM research_board_assignment_nodes
                WHERE run_id = ? AND assignment_id = ?
                """,
                (run_id, assignment_id),
            ).fetchone()
        if row is None:
            return None
        payload = dict(row)
        payload["requirement_ids"] = json.loads(payload.pop("requirement_ids_json") or "[]")
        payload["reasons"] = json.loads(payload.pop("reasons_json") or "[]")
        return payload

    def cancel_queued_assignments_due_to_budget(
        self,
        run_id: str,
        *,
        actor_thread_id: str,
        now: float | None = None,
    ) -> tuple[str, ...]:
        """Cancel only never-started work when the shared safety boundary closes."""

        timestamp = time.time() if now is None else float(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    """
                    SELECT assignment_id FROM research_board_assignment_nodes
                    WHERE run_id = ? AND status IN ('queued', 'claimed')
                    ORDER BY created_at, assignment_id
                    """,
                    (run_id,),
                ).fetchall()
                assignment_ids = tuple(row["assignment_id"] for row in rows)
                if assignment_ids:
                    connection.execute(
                        """
                        UPDATE research_board_assignment_nodes
                        SET status = 'cancelled_due_to_budget', lease_until = 0,
                            version = version + 1, updated_at = ?
                        WHERE run_id = ? AND status IN ('queued', 'claimed')
                        """,
                        (timestamp, run_id),
                    )
                    for assignment_id in assignment_ids:
                        self._event(
                            connection,
                            run_id,
                            "queued_assignment_cancelled",
                            actor_thread_id,
                            {"assignment_id": assignment_id, "reason": "budget_boundary"},
                            timestamp,
                        )
                connection.commit()
                return assignment_ids
            except Exception:
                connection.rollback()
                raise

    def update_requirement_coverage(
        self,
        run_id: str,
        requirement_id: str,
        *,
        status: str,
        evidence_ids: Iterable[str] = (),
        rationale: str = "",
        remaining_gap: str = "",
        actor_thread_id: str = "",
        now: float | None = None,
    ) -> None:
        """Persist root-level coverage independently of assignment execution status."""

        if status not in {"unsupported", "weak", "supported", "conflicted"}:
            raise ValueError(f"invalid requirement coverage status: {status}")
        timestamp = time.time() if now is None else float(now)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE research_board_requirements
                SET status = ?, evidence_ids_json = ?, rationale = ?, remaining_gap = ?,
                    version = version + 1, updated_at = ?
                WHERE run_id = ? AND requirement_id = ?
                """,
                (
                    status, _json(tuple(dict.fromkeys(evidence_ids))), rationale,
                    remaining_gap, timestamp, run_id, requirement_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown requirement: {requirement_id}")
            self._event(
                connection, run_id, "requirement_coverage_updated", actor_thread_id,
                {"requirement_id": requirement_id, "status": status}, timestamp,
            )

    def register_evidence(
        self,
        run_id: str,
        *,
        evidence_id: str,
        assignment_id: str,
        parent_assignment_id: str,
        requirement_id: str,
        owner_thread_id: str,
        source_ref: str = "",
        locator: str = "",
        now: float | None = None,
    ) -> None:
        timestamp = time.time() if now is None else float(now)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO research_board_evidence
                    (run_id, evidence_id, assignment_id, parent_assignment_id,
                     requirement_id, owner_thread_id, source_ref, locator, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, evidence_id) DO UPDATE SET
                    assignment_id = excluded.assignment_id,
                    parent_assignment_id = excluded.parent_assignment_id,
                    requirement_id = excluded.requirement_id,
                    owner_thread_id = excluded.owner_thread_id,
                    source_ref = excluded.source_ref,
                    locator = excluded.locator
                """,
                (
                    run_id, evidence_id, assignment_id, parent_assignment_id,
                    requirement_id, owner_thread_id, source_ref, locator, timestamp,
                ),
            )

    def claim_assignment(
        self,
        run_id: str,
        requirement_id: str,
        *,
        owner_thread_id: str,
        lease_seconds: float,
        now: float | None = None,
    ) -> BoardClaim:
        timestamp = time.time() if now is None else float(now)
        lease_until = timestamp + max(0.0, float(lease_seconds))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT * FROM research_board_assignments
                    WHERE run_id = ? AND requirement_id = ?
                    """,
                    (run_id, requirement_id),
                ).fetchone()
                if row is None:
                    claim = BoardClaim(False, "unknown_requirement")
                elif row["status"] == "completed":
                    claim = BoardClaim(False, "completed", row["owner_thread_id"], row["status"])
                elif (
                    row["owner_thread_id"]
                    and row["owner_thread_id"] != owner_thread_id
                    and row["status"] not in {"blocked", "failed", "unclaimed"}
                    and float(row["lease_until"]) > timestamp
                ):
                    claim = BoardClaim(False, "owned", row["owner_thread_id"], row["status"])
                else:
                    reason = "owner_resume" if row["owner_thread_id"] == owner_thread_id else "acquired"
                    connection.execute(
                        """
                        UPDATE research_board_assignments
                        SET owner_thread_id = ?, status = 'claimed', lease_until = ?,
                            version = version + 1, updated_at = ?
                        WHERE run_id = ? AND requirement_id = ?
                        """,
                        (owner_thread_id, lease_until, timestamp, run_id, requirement_id),
                    )
                    claim = BoardClaim(True, reason, owner_thread_id, "claimed")
                self._event(
                    connection,
                    run_id,
                    "assignment_claimed" if claim.acquired else "assignment_claim_skipped",
                    owner_thread_id,
                    {"requirement_id": requirement_id, "reason": claim.reason},
                    timestamp,
                )
                connection.commit()
                return claim
            except Exception:
                connection.rollback()
                raise

    def update_assignment(
        self,
        run_id: str,
        requirement_id: str,
        *,
        owner_thread_id: str,
        status: str,
        lease_seconds: float = 0.0,
        now: float | None = None,
    ) -> None:
        if status not in _ASSIGNMENT_STATUSES:
            raise ValueError(f"invalid assignment status: {status}")
        timestamp = time.time() if now is None else float(now)
        lease_until = timestamp + max(0.0, float(lease_seconds))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT owner_thread_id FROM research_board_assignments
                    WHERE run_id = ? AND requirement_id = ?
                    """,
                    (run_id, requirement_id),
                ).fetchone()
                if row is None:
                    raise KeyError(f"unknown requirement: {requirement_id}")
                if row["owner_thread_id"] not in {"", owner_thread_id}:
                    raise PermissionError("only the assignment owner may update status")
                connection.execute(
                    """
                    UPDATE research_board_assignments
                    SET owner_thread_id = ?, status = ?, lease_until = ?,
                        version = version + 1, updated_at = ?
                    WHERE run_id = ? AND requirement_id = ?
                    """,
                    (
                        owner_thread_id,
                        status,
                        lease_until if status in {"claimed", "researching"} else 0.0,
                        timestamp,
                        run_id,
                        requirement_id,
                    ),
                )
                self._event(
                    connection,
                    run_id,
                    "assignment_updated",
                    owner_thread_id,
                    {"requirement_id": requirement_id, "status": status},
                    timestamp,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _claim_work(
        self,
        *,
        table: str,
        key_column: str,
        key_value: str,
        run_id: str,
        requirement_id: str,
        assignment_id: str,
        parent_assignment_id: str,
        owner_thread_id: str,
        lease_seconds: float,
        now: float,
        insert_columns: tuple[str, ...],
        insert_values: tuple[object, ...],
        kind: str,
    ) -> BoardClaim:
        if table not in {"research_board_queries", "research_board_sources"}:
            raise ValueError("unsupported blackboard work table")
        lease_until = now + max(0.0, float(lease_seconds))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    f"SELECT * FROM {table} WHERE run_id = ? AND {key_column} = ?",
                    (run_id, key_value),
                ).fetchone()
                if row is None:
                    placeholders = ", ".join("?" for _ in insert_columns)
                    connection.execute(
                        f"INSERT INTO {table} ({', '.join(insert_columns)}) VALUES ({placeholders})",
                        insert_values,
                    )
                    claim = BoardClaim(True, "acquired", owner_thread_id, "running")
                elif row["status"] == "completed":
                    artifact_ids = ()
                    if "artifact_ids_json" in row.keys():
                        artifact_ids = tuple(json.loads(row["artifact_ids_json"] or "[]"))
                    elif row["artifact_id"]:
                        artifact_ids = (row["artifact_id"],)
                    claim = BoardClaim(
                        False,
                        "completed",
                        row["owner_thread_id"],
                        row["status"],
                        artifact_ids,
                    )
                elif (
                    row["status"] == "running"
                    and row["owner_thread_id"] != owner_thread_id
                    and float(row["lease_until"]) > now
                ):
                    claim = BoardClaim(False, "running", row["owner_thread_id"], row["status"])
                else:
                    reason = "owner_resume" if row["owner_thread_id"] == owner_thread_id else "lease_reclaimed"
                    connection.execute(
                        f"""
                        UPDATE {table}
                        SET owner_thread_id = ?, status = 'running', lease_until = ?,
                            assignment_id = ?, parent_assignment_id = ?,
                            version = version + 1, updated_at = ?
                        WHERE run_id = ? AND {key_column} = ?
                        """,
                        (
                            owner_thread_id, lease_until, assignment_id,
                            parent_assignment_id, now, run_id, key_value,
                        ),
                    )
                    claim = BoardClaim(True, reason, owner_thread_id, "running")
                self._event(
                    connection,
                    run_id,
                    f"{kind}_claimed" if claim.acquired else f"{kind}_claim_skipped",
                    owner_thread_id,
                    {
                        "key": key_value,
                        "requirement_id": requirement_id,
                        "assignment_id": assignment_id,
                        "parent_assignment_id": parent_assignment_id,
                        "reason": claim.reason,
                    },
                    now,
                )
                connection.commit()
                return claim
            except Exception:
                connection.rollback()
                raise

    def claim_query(
        self,
        run_id: str,
        *,
        requirement_id: str,
        owner_thread_id: str,
        assignment_id: str = "",
        parent_assignment_id: str = "",
        query: str,
        lease_seconds: float,
        now: float | None = None,
    ) -> BoardClaim:
        timestamp = time.time() if now is None else float(now)
        normalized = normalize_query(query)
        if not normalized:
            return BoardClaim(False, "empty_query")
        fingerprint = query_fingerprint(query)
        return self._claim_work(
            table="research_board_queries",
            key_column="fingerprint",
            key_value=fingerprint,
            run_id=run_id,
            requirement_id=requirement_id,
            assignment_id=assignment_id,
            parent_assignment_id=parent_assignment_id,
            owner_thread_id=owner_thread_id,
            lease_seconds=lease_seconds,
            now=timestamp,
            insert_columns=(
                "run_id", "fingerprint", "query", "requirement_id",
                "owner_thread_id", "status", "artifact_ids_json",
                "lease_until", "version", "updated_at", "assignment_id",
                "parent_assignment_id",
            ),
            insert_values=(
                run_id, fingerprint, normalized, requirement_id,
                owner_thread_id, "running", "[]",
                timestamp + max(0.0, float(lease_seconds)), 1, timestamp,
                assignment_id, parent_assignment_id,
            ),
            kind="query",
        )

    def complete_query(
        self,
        run_id: str,
        query: str,
        *,
        owner_thread_id: str,
        artifact_ids: Iterable[str] = (),
        failed: bool = False,
        now: float | None = None,
    ) -> None:
        self._complete_work(
            table="research_board_queries",
            key_column="fingerprint",
            key_value=query_fingerprint(query),
            run_id=run_id,
            owner_thread_id=owner_thread_id,
            status="failed" if failed else "completed",
            values={"artifact_ids_json": _json(tuple(artifact_ids))},
            kind="query",
            now=time.time() if now is None else float(now),
        )

    def claim_source(
        self,
        run_id: str,
        *,
        owner_thread_id: str,
        url: str,
        lease_seconds: float,
        requirement_id: str = "",
        assignment_id: str = "",
        parent_assignment_id: str = "",
        now: float | None = None,
    ) -> BoardClaim:
        timestamp = time.time() if now is None else float(now)
        canonical = canonicalize_source_url(url)
        if not canonical.startswith(("http://", "https://")):
            return BoardClaim(False, "invalid_url")
        return self._claim_work(
            table="research_board_sources",
            key_column="canonical_url",
            key_value=canonical,
            run_id=run_id,
            requirement_id=requirement_id,
            assignment_id=assignment_id,
            parent_assignment_id=parent_assignment_id,
            owner_thread_id=owner_thread_id,
            lease_seconds=lease_seconds,
            now=timestamp,
            insert_columns=(
                "run_id", "canonical_url", "owner_thread_id", "status",
                "artifact_id", "error", "lease_until", "version", "updated_at",
                "assignment_id", "parent_assignment_id",
            ),
            insert_values=(
                run_id, canonical, owner_thread_id, "running", "", "",
                timestamp + max(0.0, float(lease_seconds)), 1, timestamp,
                assignment_id, parent_assignment_id,
            ),
            kind="source",
        )

    def complete_source(
        self,
        run_id: str,
        url: str,
        *,
        owner_thread_id: str,
        artifact_id: str = "",
        error: str = "",
        now: float | None = None,
    ) -> None:
        self._complete_work(
            table="research_board_sources",
            key_column="canonical_url",
            key_value=canonicalize_source_url(url),
            run_id=run_id,
            owner_thread_id=owner_thread_id,
            status="failed" if error else "completed",
            values={"artifact_id": artifact_id, "error": error[:1000]},
            kind="source",
            now=time.time() if now is None else float(now),
        )

    def _complete_work(
        self,
        *,
        table: str,
        key_column: str,
        key_value: str,
        run_id: str,
        owner_thread_id: str,
        status: str,
        values: Mapping[str, object],
        kind: str,
        now: float,
    ) -> None:
        if status not in _WORK_STATUSES:
            raise ValueError(f"invalid work status: {status}")
        allowed = {
            "research_board_queries": {"artifact_ids_json"},
            "research_board_sources": {"artifact_id", "error"},
        }
        if table not in allowed or set(values) - allowed[table]:
            raise ValueError("unsupported blackboard completion fields")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    f"SELECT owner_thread_id FROM {table} WHERE run_id = ? AND {key_column} = ?",
                    (run_id, key_value),
                ).fetchone()
                if row is None:
                    raise KeyError(f"unknown {kind}: {key_value}")
                if row["owner_thread_id"] != owner_thread_id:
                    raise PermissionError(f"only the {kind} owner may complete it")
                assignments = ", ".join(f"{column} = ?" for column in values)
                connection.execute(
                    f"""
                    UPDATE {table}
                    SET status = ?, {assignments}, lease_until = 0,
                        version = version + 1, updated_at = ?
                    WHERE run_id = ? AND {key_column} = ?
                    """,
                    (status, *values.values(), now, run_id, key_value),
                )
                self._event(
                    connection,
                    run_id,
                    f"{kind}_{status}",
                    owner_thread_id,
                    {"key": key_value},
                    now,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def publish_signal(
        self,
        run_id: str,
        *,
        evidence_id: str,
        discovered_by: str,
        target_requirement_id: str,
        parent_thread_id: str = "",
        message: str = "",
        now: float | None = None,
    ) -> str:
        timestamp = time.time() if now is None else float(now)
        signal_id = "signal-" + hashlib.sha256(
            f"{run_id}\n{evidence_id}\n{target_requirement_id}".encode("utf-8")
        ).hexdigest()[:16]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO research_board_signals
                        (run_id, signal_id, evidence_id, discovered_by,
                         target_requirement_id, parent_thread_id, message,
                         status, consumed_by, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'open', '', ?, ?)
                    ON CONFLICT(run_id, signal_id) DO NOTHING
                    """,
                    (
                        run_id,
                        signal_id,
                        evidence_id,
                        discovered_by,
                        target_requirement_id,
                        parent_thread_id,
                        message[:1000],
                        timestamp,
                        timestamp,
                    ),
                )
                self._event(
                    connection,
                    run_id,
                    "cross_scope_signal_published",
                    discovered_by,
                    {
                        "signal_id": signal_id,
                        "evidence_id": evidence_id,
                        "target_requirement_id": target_requirement_id,
                    },
                    timestamp,
                )
                connection.commit()
                return signal_id
            except Exception:
                connection.rollback()
                raise

    def consume_signal(
        self,
        run_id: str,
        signal_id: str,
        *,
        consumer_thread_id: str,
        now: float | None = None,
    ) -> bool:
        timestamp = time.time() if now is None else float(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT status FROM research_board_signals
                    WHERE run_id = ? AND signal_id = ?
                    """,
                    (run_id, signal_id),
                ).fetchone()
                if row is None or row["status"] not in _SIGNAL_STATUSES:
                    consumed = False
                elif row["status"] == "consumed":
                    consumed = False
                else:
                    connection.execute(
                        """
                        UPDATE research_board_signals
                        SET status = 'consumed', consumed_by = ?, updated_at = ?
                        WHERE run_id = ? AND signal_id = ?
                        """,
                        (consumer_thread_id, timestamp, run_id, signal_id),
                    )
                    consumed = True
                self._event(
                    connection,
                    run_id,
                    "cross_scope_signal_consumed" if consumed else "cross_scope_signal_skipped",
                    consumer_thread_id,
                    {"signal_id": signal_id},
                    timestamp,
                )
                connection.commit()
                return consumed
            except Exception:
                connection.rollback()
                raise

    def update_agent(
        self,
        run_id: str,
        *,
        thread_id: str,
        parent_thread_id: str,
        depth: int,
        requirement_ids: Iterable[str],
        assignment_id: str = "",
        parent_assignment_id: str = "",
        status: str,
        current_action: str = "",
        now: float | None = None,
    ) -> None:
        timestamp = time.time() if now is None else float(now)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO research_board_agents
                    (run_id, thread_id, parent_thread_id, depth,
                     requirement_ids_json, current_action, status, updated_at,
                     assignment_id, parent_assignment_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, thread_id) DO UPDATE SET
                    parent_thread_id = excluded.parent_thread_id,
                    depth = excluded.depth,
                    requirement_ids_json = excluded.requirement_ids_json,
                    current_action = excluded.current_action,
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    assignment_id = excluded.assignment_id,
                    parent_assignment_id = excluded.parent_assignment_id
                """,
                (
                    run_id,
                    thread_id,
                    parent_thread_id,
                    int(depth),
                    _json(tuple(dict.fromkeys(str(item) for item in requirement_ids))),
                    current_action[:1000],
                    status,
                    timestamp,
                    assignment_id,
                    parent_assignment_id,
                ),
            )

    def record_event(
        self,
        run_id: str,
        kind: str,
        *,
        actor_thread_id: str = "",
        payload: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> None:
        """Append an observability event without changing coordination state."""

        timestamp = time.time() if now is None else float(now)
        with self._connect() as connection:
            self._event(
                connection,
                run_id,
                kind,
                actor_thread_id,
                payload or {},
                timestamp,
            )

    def snapshot(
        self,
        run_id: str,
        *,
        viewer_thread_id: str,
        own_requirement_ids: Iterable[str] = (),
        own_assignment_id: str = "",
        limit: int = 24,
    ) -> dict[str, Any]:
        """Return a compact, model-safe board view instead of raw event history."""

        own_ids = tuple(dict.fromkeys(str(item) for item in own_requirement_ids))
        with self._connect() as connection:
            run = connection.execute(
                "SELECT * FROM research_board_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                return {}
            assignments = connection.execute(
                """
                SELECT requirement_id, owner_thread_id, parent_thread_id,
                       objective, status, lease_until, version
                FROM research_board_assignments
                WHERE run_id = ? ORDER BY requirement_id
                """,
                (run_id,),
            ).fetchall()
            assignment_nodes = connection.execute(
                """
                SELECT assignment_id, parent_assignment_id, owner_thread_id,
                       parent_thread_id, requirement_ids_json, objective,
                       scope_signature, reasons_json, depth, status, lease_until,
                       version, created_at, updated_at
                FROM research_board_assignment_nodes
                WHERE run_id = ? ORDER BY depth, created_at, assignment_id
                """,
                (run_id,),
            ).fetchall()
            requirement_rows = connection.execute(
                """
                SELECT requirement_id, description, required,
                       requires_external_evidence, status,
                       evidence_ids_json, rationale, remaining_gap, version
                FROM research_board_requirements
                WHERE run_id = ? ORDER BY requirement_id
                """,
                (run_id,),
            ).fetchall()
            queries = connection.execute(
                """
                SELECT fingerprint, query, requirement_id, owner_thread_id,
                       status, artifact_ids_json, assignment_id, parent_assignment_id
                FROM research_board_queries
                WHERE run_id = ? ORDER BY updated_at DESC LIMIT ?
                """,
                (run_id, max(1, int(limit))),
            ).fetchall()
            sources = connection.execute(
                """
                SELECT canonical_url, owner_thread_id, status, artifact_id,
                       assignment_id, parent_assignment_id
                FROM research_board_sources
                WHERE run_id = ? ORDER BY updated_at DESC LIMIT ?
                """,
                (run_id, max(1, int(limit))),
            ).fetchall()
            signal_rows = connection.execute(
                """
                SELECT signal_id, evidence_id, discovered_by,
                       target_requirement_id, parent_thread_id, message, status
                FROM research_board_signals
                WHERE run_id = ?
                ORDER BY created_at LIMIT ?
                """,
                (run_id, max(1, int(limit))),
            ).fetchall()
            evidence_rows = connection.execute(
                """
                SELECT evidence_id, assignment_id, parent_assignment_id,
                       requirement_id, owner_thread_id, source_ref, locator
                FROM research_board_evidence
                WHERE run_id = ? ORDER BY created_at DESC LIMIT ?
                """,
                (run_id, max(1, int(limit))),
            ).fetchall()
        own_set = set(own_ids)
        assignment_payload = [dict(row) for row in assignments]
        node_payload = [
            {
                **dict(row),
                "requirement_ids": json.loads(row["requirement_ids_json"] or "[]"),
                "reasons": json.loads(row["reasons_json"] or "[]"),
                "requirement_id": (
                    json.loads(row["requirement_ids_json"] or "[]")[0]
                    if len(json.loads(row["requirement_ids_json"] or "[]")) == 1
                    else ""
                ),
            }
            for row in assignment_nodes
        ]
        requirement_payload = [
            {
                **dict(row),
                "required": bool(row["required"]),
                "requires_external_evidence": bool(row["requires_external_evidence"]),
                "evidence_ids": json.loads(row["evidence_ids_json"] or "[]"),
            }
            for row in requirement_rows
        ]
        own_node_ids = {
            item["assignment_id"] for item in node_payload
            if item["owner_thread_id"] == viewer_thread_id
            or (own_assignment_id and item["assignment_id"] == own_assignment_id)
        }
        visible_own_assignments = [
            item for item in node_payload if item["assignment_id"] in own_node_ids
        ]
        visible_sibling_assignments = [
            item for item in node_payload
            if item["depth"] > 0
            and item["owner_thread_id"] != viewer_thread_id
            and item["assignment_id"] not in own_node_ids
        ]
        if not node_payload:
            visible_own_assignments = [
                item for item in assignment_payload
                if item["owner_thread_id"] == viewer_thread_id
                or item["requirement_id"] in own_set
            ]
            visible_sibling_assignments = [
                item for item in assignment_payload
                if item["owner_thread_id"]
                and item["owner_thread_id"] != viewer_thread_id
            ]
        return {
            "plan": {
                "plan_id": run["plan_id"],
                "objective": run["objective"],
                "requirements": json.loads(run["requirements_json"]),
                "report_outline": json.loads(run["outline_json"]),
            },
            "viewer": {
                "thread_id": viewer_thread_id,
                "own_requirement_ids": own_ids,
                "own_assignment_id": own_assignment_id,
            },
            "own_assignments": visible_own_assignments,
            "sibling_assignments": visible_sibling_assignments,
            "assignment_tree": node_payload,
            "requirements": requirement_payload,
            "requirement_status": (
                {item["requirement_id"]: item["status"] for item in requirement_payload}
                if node_payload
                else {item["requirement_id"]: item["status"] for item in assignment_payload}
            ),
            "recent_queries": [
                {
                    **dict(row),
                    "artifact_ids": json.loads(row["artifact_ids_json"] or "[]"),
                }
                for row in queries
            ],
            "recent_sources": [dict(row) for row in sources],
            "recent_evidence": [dict(row) for row in evidence_rows],
            "cross_scope_signals": [dict(row) for row in signal_rows if (
                not own_set or row["target_requirement_id"] in own_set
            )],
            "coordination_rules": (
                "Research only your own assignment scope. Multiple assignments may share "
                "a requirement when their scope signatures differ. Reuse completed queries "
                "and sources; do not duplicate an active sibling assignment; preserve "
                "incidental cross-scope Evidence and signal its target requirement."
            ),
        }

    def metrics(self, run_id: str) -> dict[str, int]:
        with self._connect() as connection:
            counts = {
                "assignment_count": connection.execute(
                    "SELECT COUNT(*) FROM research_board_assignment_nodes WHERE run_id = ?",
                    (run_id,),
                ).fetchone()[0],
                "legacy_assignment_count": connection.execute(
                    "SELECT COUNT(*) FROM research_board_assignments WHERE run_id = ?",
                    (run_id,),
                ).fetchone()[0],
                "active_assignment_count": connection.execute(
                    """
                    SELECT COUNT(*) FROM research_board_assignment_nodes
                    WHERE run_id = ? AND status = 'researching'
                    """,
                    (run_id,),
                ).fetchone()[0],
                "queued_assignment_count": connection.execute(
                    """
                    SELECT COUNT(*) FROM research_board_assignment_nodes
                    WHERE run_id = ? AND status = 'queued'
                    """,
                    (run_id,),
                ).fetchone()[0],
                "waiting_assignment_count": connection.execute(
                    """
                    SELECT COUNT(*) FROM research_board_assignment_nodes
                    WHERE run_id = ? AND status = 'waiting_children'
                    """,
                    (run_id,),
                ).fetchone()[0],
                "cancelled_due_to_budget_count": connection.execute(
                    """
                    SELECT COUNT(*) FROM research_board_assignment_nodes
                    WHERE run_id = ? AND status = 'cancelled_due_to_budget'
                    """,
                    (run_id,),
                ).fetchone()[0],
                "requirement_count": connection.execute(
                    "SELECT COUNT(*) FROM research_board_requirements WHERE run_id = ?",
                    (run_id,),
                ).fetchone()[0],
                "evidence_lineage_count": connection.execute(
                    "SELECT COUNT(*) FROM research_board_evidence WHERE run_id = ?",
                    (run_id,),
                ).fetchone()[0],
                "query_count": connection.execute(
                    "SELECT COUNT(*) FROM research_board_queries WHERE run_id = ?",
                    (run_id,),
                ).fetchone()[0],
                "source_count": connection.execute(
                    "SELECT COUNT(*) FROM research_board_sources WHERE run_id = ?",
                    (run_id,),
                ).fetchone()[0],
                "open_signal_count": connection.execute(
                    """
                    SELECT COUNT(*) FROM research_board_signals
                    WHERE run_id = ? AND status = 'open'
                    """,
                    (run_id,),
                ).fetchone()[0],
            }
            pool = connection.execute(
                "SELECT * FROM research_board_budget_pools WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if pool is not None:
                counts.update({
                    "budget_total_tokens": int(pool["total_tokens"]),
                    "budget_protected_tokens": int(pool["protected_tokens"]),
                    "budget_allocated_tokens": int(pool["allocated_tokens"]),
                    "budget_available_tokens": max(
                        0,
                        int(pool["total_tokens"])
                        - int(pool["protected_tokens"])
                        - int(pool["allocated_tokens"]),
                    ),
                    "active_budget_lease_count": connection.execute(
                        """
                        SELECT COUNT(*) FROM research_board_budget_leases
                        WHERE run_id = ? AND status = 'active'
                        """,
                        (run_id,),
                    ).fetchone()[0],
                })
            event_rows = connection.execute(
                """
                SELECT kind, COUNT(*) AS count FROM research_board_events
                WHERE run_id = ? GROUP BY kind
                """,
                (run_id,),
            ).fetchall()
            scheduler_rows = connection.execute(
                """
                SELECT kind, payload_json FROM research_board_events
                WHERE run_id = ? AND kind IN (
                    'scheduler_state', 'queued_assignment_started'
                )
                """,
                (run_id,),
            ).fetchall()
        counts.update({f"event_{row['kind']}": int(row["count"]) for row in event_rows})
        scheduler_payloads = [
            json.loads(row["payload_json"] or "{}") for row in scheduler_rows
        ]
        counts.update({
            "active_agent_peak": max(
                (int(item.get("active_peak", 0)) for item in scheduler_payloads),
                default=0,
            ),
            "queued_assignment_peak": max(
                (int(item.get("queued_peak", 0)) for item in scheduler_payloads),
                default=0,
            ),
            "waiting_parent_peak": max(
                (int(item.get("waiting_peak", 0)) for item in scheduler_payloads),
                default=0,
            ),
            "queue_wait_milliseconds_peak": max(
                (
                    int(float(item.get("queue_wait_seconds", 0.0)) * 1000)
                    for item in scheduler_payloads
                ),
                default=0,
            ),
        })
        return {key: int(value) for key, value in counts.items()}


__all__ = [
    "BoardClaim",
    "ResearchBlackboard",
    "normalize_query",
    "query_fingerprint",
]
