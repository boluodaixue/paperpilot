"""Persistent blackboard coordination contracts for homogeneous research."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from src.research.research_blackboard import ResearchBlackboard


def _board(tmp_path) -> ResearchBlackboard:
    board = ResearchBlackboard(tmp_path / "research.sqlite")
    board.ensure_plan(
        "run-1",
        plan_id="plan-1",
        objective="Compare two instruments",
        requirements=(
            {"requirement_id": "R1", "description": "Use of proceeds"},
            {"requirement_id": "R2", "description": "Disclosure"},
            {"requirement_id": "R3", "description": "Investor protection"},
        ),
        report_outline=("Use of proceeds", "Disclosure", "Investor protection"),
        now=100.0,
    )
    return board


def test_plan_is_persistent_and_rejects_run_drift(tmp_path) -> None:
    board = _board(tmp_path)
    reopened = ResearchBlackboard(tmp_path / "research.sqlite")

    snapshot = reopened.snapshot("run-1", viewer_thread_id="root")

    assert snapshot["plan"]["plan_id"] == "plan-1"
    assert len(snapshot["plan"]["requirements"]) == 3
    with pytest.raises(ValueError, match="does not match"):
        reopened.ensure_plan(
            "run-1",
            plan_id="different",
            objective="Compare two instruments",
            requirements=(),
        )


def test_assignment_claim_is_atomic_and_expired_owner_can_be_replaced(tmp_path) -> None:
    board = _board(tmp_path)
    board.register_assignment_batch(
        "run-1",
        ({"requirement_id": "R1", "objective": "Research proceeds"},),
        actor_thread_id="root",
        lease_seconds=30,
        now=100.0,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(
            lambda owner: board.claim_assignment(
                "run-1", "R1", owner_thread_id=owner,
                lease_seconds=30, now=101.0,
            ),
            ("child-a", "child-b"),
        ))

    assert sum(item.acquired for item in claims) == 1
    winner = next(item.owner_thread_id for item in claims if item.acquired)
    replacement = "child-b" if winner == "child-a" else "child-a"
    reclaimed = board.claim_assignment(
        "run-1", "R1", owner_thread_id=replacement,
        lease_seconds=30, now=140.0,
    )
    assert reclaimed.acquired is True
    assert reclaimed.reason == "acquired"


def test_query_ledger_skips_running_and_reuses_completed_work(tmp_path) -> None:
    board = _board(tmp_path)
    first = board.claim_query(
        "run-1",
        requirement_id="R1",
        owner_thread_id="child-a",
        query="  ICMA   Green Bond Principles ",
        lease_seconds=60,
        now=100.0,
    )
    duplicate = board.claim_query(
        "run-1",
        requirement_id="R2",
        owner_thread_id="child-b",
        query="icma green bond principles",
        lease_seconds=60,
        now=101.0,
    )
    board.complete_query(
        "run-1",
        "ICMA Green Bond Principles",
        owner_thread_id="child-a",
        artifact_ids=("artifact-1",),
        now=102.0,
    )
    reusable = board.claim_query(
        "run-1",
        requirement_id="R2",
        owner_thread_id="child-b",
        query="ICMA Green Bond Principles",
        lease_seconds=60,
        now=103.0,
    )

    assert first.acquired is True
    assert duplicate == duplicate.__class__(False, "running", "child-a", "running", ())
    assert reusable.acquired is False
    assert reusable.reason == "completed"
    assert reusable.artifact_ids == ("artifact-1",)


def test_source_registry_canonicalizes_url_and_reuses_document(tmp_path) -> None:
    board = _board(tmp_path)
    first = board.claim_source(
        "run-1",
        owner_thread_id="child-a",
        requirement_id="R1",
        url="https://Example.com/report/?utm_source=test#page-2",
        lease_seconds=60,
        now=100.0,
    )
    duplicate = board.claim_source(
        "run-1",
        owner_thread_id="child-b",
        requirement_id="R2",
        url="https://example.com/report",
        lease_seconds=60,
        now=101.0,
    )
    board.complete_source(
        "run-1",
        "https://example.com/report",
        owner_thread_id="child-a",
        artifact_id="document-1",
        now=102.0,
    )
    reusable = board.claim_source(
        "run-1",
        owner_thread_id="child-b",
        requirement_id="R2",
        url="https://example.com/report/",
        lease_seconds=60,
        now=103.0,
    )

    assert first.acquired is True
    assert duplicate.reason == "running"
    assert reusable.reason == "completed"
    assert reusable.artifact_ids == ("document-1",)


def test_sibling_manifest_and_cross_scope_signal_are_shared(tmp_path) -> None:
    board = _board(tmp_path)
    board.register_assignment_batch(
        "run-1",
        (
            {
                "requirement_id": "R1",
                "owner_thread_id": "child-a",
                "parent_thread_id": "root",
                "objective": "Research proceeds",
            },
            {
                "requirement_id": "R2",
                "owner_thread_id": "child-b",
                "parent_thread_id": "root",
                "objective": "Research disclosure",
            },
        ),
        actor_thread_id="root",
        lease_seconds=60,
        now=100.0,
    )
    signal_id = board.publish_signal(
        "run-1",
        evidence_id="E-cross",
        discovered_by="child-a",
        target_requirement_id="R2",
        parent_thread_id="root",
        message="The same official document contains a disclosure clause.",
        now=101.0,
    )

    child_view = board.snapshot(
        "run-1",
        viewer_thread_id="child-b",
        own_requirement_ids=("R2",),
    )
    root_view = board.snapshot("run-1", viewer_thread_id="root")

    assert [item["requirement_id"] for item in child_view["own_assignments"]] == ["R2"]
    assert [item["requirement_id"] for item in child_view["sibling_assignments"]] == ["R1"]
    assert child_view["cross_scope_signals"][0]["evidence_id"] == "E-cross"
    assert root_view["cross_scope_signals"][0]["signal_id"] == signal_id
    assert board.consume_signal(
        "run-1", signal_id, consumer_thread_id="child-b", now=102.0
    ) is True
    assert board.consume_signal(
        "run-1", signal_id, consumer_thread_id="child-b", now=103.0
    ) is False


def test_only_assignment_owner_can_finish_active_scope(tmp_path) -> None:
    board = _board(tmp_path)
    board.register_assignment_batch(
        "run-1",
        ({
            "requirement_id": "R3",
            "owner_thread_id": "child-c",
            "objective": "Research investor protection",
        },),
        actor_thread_id="root",
        lease_seconds=60,
        now=100.0,
    )

    with pytest.raises(PermissionError):
        board.update_assignment(
            "run-1", "R3", owner_thread_id="child-a", status="completed", now=101.0
        )
    board.update_assignment(
        "run-1", "R3", owner_thread_id="child-c", status="completed", now=102.0
    )
    assert board.snapshot("run-1", viewer_thread_id="root")["requirement_status"]["R3"] == "completed"


def test_current_owner_can_delegate_scope_to_direct_child(tmp_path) -> None:
    board = _board(tmp_path)
    board.register_assignment_batch(
        "run-1",
        ({
            "requirement_id": "R1",
            "owner_thread_id": "child-a",
            "parent_thread_id": "root",
            "objective": "Research proceeds",
        },),
        actor_thread_id="root",
        lease_seconds=60,
        now=100.0,
    )

    board.register_assignment_batch(
        "run-1",
        ({
            "requirement_id": "R1",
            "owner_thread_id": "grandchild-a",
            "parent_thread_id": "child-a",
            "objective": "Inspect a deep primary-source chain",
        },),
        actor_thread_id="child-a",
        lease_seconds=60,
        now=101.0,
    )

    own = board.snapshot(
        "run-1", viewer_thread_id="grandchild-a", own_requirement_ids=("R1",)
    )["own_assignments"]
    assert own[0]["owner_thread_id"] == "grandchild-a"
    assert own[0]["parent_thread_id"] == "child-a"


def test_sibling_cannot_steal_an_active_assignment(tmp_path) -> None:
    board = _board(tmp_path)
    board.register_assignment_batch(
        "run-1",
        ({
            "requirement_id": "R1",
            "owner_thread_id": "child-a",
            "objective": "Research proceeds",
        },),
        actor_thread_id="root",
        lease_seconds=60,
        now=100.0,
    )

    with pytest.raises(ValueError, match="already owns"):
        board.register_assignment_batch(
            "run-1",
            ({
                "requirement_id": "R1",
                "owner_thread_id": "child-b",
                "objective": "Duplicate proceeds research",
            },),
            actor_thread_id="child-b",
            lease_seconds=60,
            now=101.0,
        )


def test_budget_lease_topup_and_release_preserve_protected_pool(tmp_path) -> None:
    board = _board(tmp_path)
    board.ensure_budget_pool(
        "run-1",
        total_tokens=500000,
        protected_tokens=125000,
        now=100.0,
    )
    first = board.grant_budget_lease(
        "run-1",
        thread_id="child-a",
        parent_thread_id="root",
        requested_tokens=60000,
        max_tokens=125000,
        now=101.0,
    )
    second = board.grant_budget_lease(
        "run-1",
        thread_id="child-b",
        parent_thread_id="root",
        requested_tokens=60000,
        max_tokens=125000,
        now=101.0,
    )
    topped = board.request_budget_topup(
        "run-1",
        thread_id="child-a",
        used_tokens=55000,
        requested_tokens=25000,
        now=102.0,
    )
    returned = board.release_budget_lease(
        "run-1",
        thread_id="child-b",
        used_tokens=20000,
        now=103.0,
    )

    assert first == second == 60000
    assert topped == 85000
    assert returned == 40000
    assert board.budget_lease("run-1", "child-a") == {
        "thread_id": "child-a",
        "parent_thread_id": "root",
        "granted_tokens": 85000,
        "used_tokens": 55000,
        "max_tokens": 125000,
        "status": "active",
        "version": 2,
    }
    metrics = board.metrics("run-1")
    assert metrics["event_budget_lease_topped_up"] == 1
    assert metrics["event_budget_lease_released"] == 1


def test_budget_pool_cannot_spend_parent_and_finalization_reserve(tmp_path) -> None:
    board = _board(tmp_path)
    board.ensure_budget_pool(
        "run-1",
        total_tokens=200000,
        protected_tokens=100000,
        now=100.0,
    )
    first = board.grant_budget_lease(
        "run-1",
        thread_id="child-a",
        parent_thread_id="root",
        requested_tokens=60000,
        max_tokens=125000,
        now=101.0,
    )
    second = board.grant_budget_lease(
        "run-1",
        thread_id="child-b",
        parent_thread_id="root",
        requested_tokens=60000,
        max_tokens=125000,
        now=101.0,
    )

    assert first == 60000
    assert second == 40000
    assert board.request_budget_topup(
        "run-1",
        thread_id="child-a",
        used_tokens=59000,
        requested_tokens=25000,
        now=102.0,
    ) == 60000


def test_assignment_tree_allows_distinct_scopes_under_one_requirement(tmp_path) -> None:
    board = _board(tmp_path)
    board.ensure_root_assignment(
        "run-1",
        assignment_id="assignment-root",
        owner_thread_id="root",
        objective="Compare two instruments",
        requirement_ids=("R1", "R2", "R3"),
        now=100.0,
    )

    outcomes = board.register_assignment_nodes(
        "run-1",
        (
            {
                "assignment_id": "assignment-coupon",
                "parent_assignment_id": "assignment-root",
                "owner_thread_id": "child-coupon",
                "parent_thread_id": "root",
                "requirement_ids": ("R3",),
                "objective": "Inspect coupon and term adjustments",
                "scope_signature": "coupon-term-adjustment",
                "reasons": ("parallel",),
            },
            {
                "assignment_id": "assignment-remedies",
                "parent_assignment_id": "assignment-root",
                "owner_thread_id": "child-remedies",
                "parent_thread_id": "root",
                "requirement_ids": ("R3",),
                "objective": "Inspect default and investor remedies",
                "scope_signature": "default-investor-remedies",
                "reasons": ("parallel",),
            },
        ),
        actor_thread_id="root",
        lease_seconds=60,
        now=101.0,
    )

    assert all(claim.acquired for claim in outcomes.values())
    view = board.snapshot(
        "run-1",
        viewer_thread_id="child-coupon",
        own_requirement_ids=("R3",),
        own_assignment_id="assignment-coupon",
    )
    assert [item["assignment_id"] for item in view["own_assignments"]] == [
        "assignment-coupon"
    ]
    assert "assignment-remedies" in {
        item["assignment_id"] for item in view["sibling_assignments"]
    }


def test_assignment_tree_rejects_exact_duplicate_but_accepts_new_scope(tmp_path) -> None:
    board = _board(tmp_path)
    board.ensure_root_assignment(
        "run-1",
        assignment_id="assignment-root",
        owner_thread_id="root",
        objective="Compare two instruments",
        requirement_ids=("R1",),
        now=100.0,
    )
    first = board.register_assignment_nodes(
        "run-1",
        ({
            "assignment_id": "assignment-first",
            "parent_assignment_id": "assignment-root",
            "owner_thread_id": "child-a",
            "requirement_ids": ("R1",),
            "objective": "Inspect proceeds tracking",
            "scope_signature": "proceeds-tracking",
        },),
        actor_thread_id="root",
        lease_seconds=60,
        now=101.0,
    )
    duplicate = board.register_assignment_nodes(
        "run-1",
        ({
            "assignment_id": "assignment-duplicate",
            "parent_assignment_id": "assignment-root",
            "owner_thread_id": "child-b",
            "requirement_ids": ("R1",),
            "objective": "  INSPECT proceeds tracking ",
            "scope_signature": "proceeds-tracking",
        },),
        actor_thread_id="root",
        lease_seconds=60,
        now=102.0,
    )
    stolen = board.register_assignment_nodes(
        "run-1",
        ({
            "assignment_id": "assignment-stolen",
            "parent_assignment_id": "assignment-first",
            "owner_thread_id": "grandchild-b",
            "requirement_ids": ("R1",),
            "objective": "A sibling attempts to delegate under child A",
            "scope_signature": "unauthorized-subscope",
        },),
        actor_thread_id="child-b",
        lease_seconds=60,
        now=103.0,
    )

    assert first["assignment-first"].acquired is True
    assert duplicate["assignment-duplicate"].reason == "duplicate_sibling_scope"
    assert stolen["assignment-stolen"].reason == "parent_not_owned"

    renamed_retry = board.register_assignment_nodes(
        "run-1",
        ({
            "assignment_id": "assignment-renamed-retry",
            "parent_assignment_id": "assignment-root",
            "owner_thread_id": "child-c",
            "requirement_ids": ("R1",),
            "objective": "Inspect proceeds tracking",
            "scope_signature": "proceeds-tracking-v2-retry",
        },),
        actor_thread_id="root",
        lease_seconds=60,
        now=104.0,
    )
    assert renamed_retry["assignment-renamed-retry"].acquired is True


def test_child_can_delegate_multiple_grandchild_scopes_without_completing_requirement(tmp_path) -> None:
    board = _board(tmp_path)
    board.ensure_root_assignment(
        "run-1",
        assignment_id="assignment-root",
        owner_thread_id="root",
        objective="Compare two instruments",
        requirement_ids=("R3",),
        now=100.0,
    )
    board.register_assignment_nodes(
        "run-1",
        ({
            "assignment_id": "assignment-investor-protection",
            "parent_assignment_id": "assignment-root",
            "owner_thread_id": "child-protection",
            "requirement_ids": ("R3",),
            "objective": "Research investor protection",
            "scope_signature": "investor-protection",
        },),
        actor_thread_id="root",
        lease_seconds=60,
        now=101.0,
    )
    outcomes = board.register_assignment_nodes(
        "run-1",
        (
            {
                "assignment_id": "assignment-default",
                "parent_assignment_id": "assignment-investor-protection",
                "owner_thread_id": "grandchild-default",
                "requirement_ids": ("R3",),
                "objective": "Trace default and acceleration clauses",
                "scope_signature": "default-acceleration",
            },
            {
                "assignment_id": "assignment-remedy",
                "parent_assignment_id": "assignment-investor-protection",
                "owner_thread_id": "grandchild-remedy",
                "requirement_ids": ("R3",),
                "objective": "Trace investor remedies",
                "scope_signature": "investor-remedies",
            },
        ),
        actor_thread_id="child-protection",
        lease_seconds=60,
        now=102.0,
    )
    board.update_assignment_node(
        "run-1",
        "assignment-default",
        owner_thread_id="grandchild-default",
        status="completed",
        now=103.0,
    )

    assert all(claim.acquired for claim in outcomes.values())
    snapshot = board.snapshot("run-1", viewer_thread_id="root")
    assert snapshot["requirement_status"]["R3"] == "unsupported"
    assert {
        item["assignment_id"] for item in snapshot["assignment_tree"]
    } >= {"assignment-default", "assignment-remedy"}


def test_query_source_and_evidence_lineage_survive_reopen(tmp_path) -> None:
    board = _board(tmp_path)
    board.claim_query(
        "run-1",
        requirement_id="R2",
        owner_thread_id="grandchild",
        assignment_id="assignment-disclosure",
        parent_assignment_id="assignment-parent",
        query="official disclosure rules",
        lease_seconds=60,
        now=100.0,
    )
    board.claim_source(
        "run-1",
        owner_thread_id="grandchild",
        requirement_id="R2",
        assignment_id="assignment-disclosure",
        parent_assignment_id="assignment-parent",
        url="https://authority.example/rules",
        lease_seconds=60,
        now=100.0,
    )
    board.register_evidence(
        "run-1",
        evidence_id="E-lineage",
        assignment_id="assignment-disclosure",
        parent_assignment_id="assignment-parent",
        requirement_id="R2",
        owner_thread_id="grandchild",
        source_ref="https://authority.example/rules",
        locator="section 2",
        now=101.0,
    )

    reopened = ResearchBlackboard(tmp_path / "research.sqlite")
    snapshot = reopened.snapshot("run-1", viewer_thread_id="root")
    assert snapshot["recent_queries"][0]["assignment_id"] == "assignment-disclosure"
    assert snapshot["recent_sources"][0]["parent_assignment_id"] == "assignment-parent"
    assert snapshot["recent_evidence"][0]["evidence_id"] == "E-lineage"


def test_assignment_tree_uses_one_atomic_global_thread_cap(tmp_path) -> None:
    board = _board(tmp_path)
    board.ensure_root_assignment(
        "run-1",
        assignment_id="assignment-root",
        owner_thread_id="root",
        objective="Compare two instruments",
        requirement_ids=("R1",),
        now=100.0,
    )
    outcomes = board.register_assignment_nodes(
        "run-1",
        (
            {
                "assignment_id": f"assignment-{index}",
                "parent_assignment_id": "assignment-root",
                "owner_thread_id": f"child-{index}",
                "requirement_ids": ("R1",),
                "objective": f"Distinct scope {index}",
                "scope_signature": f"scope-{index}",
            }
            for index in range(3)
        ),
        actor_thread_id="root",
        lease_seconds=60,
        max_total_assignments=3,
        now=101.0,
    )

    assert outcomes["assignment-0"].acquired is True
    assert outcomes["assignment-1"].acquired is True
    assert outcomes["assignment-2"].reason == "global_thread_limit_reached"
    assert board.metrics("run-1")["assignment_count"] == 3
