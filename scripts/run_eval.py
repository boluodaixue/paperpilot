#!/usr/bin/env python3
"""Run PaperPilot's structured research workflow against supported benchmarks."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import re
import sys
import time
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evaluation.benchmarks.hotpotqa import HotpotQABenchmark
from evaluation.benchmarks.memory_wiki import evaluate_memory_wiki
from evaluation.benchmarks.research_bench import ResearchBench
from evaluation.judge import LLMJudge
from evaluation.report import EvaluationReport
from src.models.model_router import ModelRouter
from src.research.checkpoint_serde import paperpilot_in_memory_saver
from src.research.models import ResearchWorkflowResult
from src.research.runtime import (
    build_research_runtime,
    load_config,
    open_research_runtime,
    setup_logging,
)


async def extract_grounded_hotpotqa_answer(
    report: str,
    question: str,
    config: dict[str, Any],
) -> str:
    """Compress one report into a short answer without tools or gold labels."""
    model_config = config.get("model", {})
    backend_mapping = model_config.get("backend_mapping", {})
    backend = backend_mapping.get(
        "judge",
        backend_mapping.get("research", model_config.get("backend", "vllm")),
    )
    policy = ModelRouter.create_backend(
        backend,
        temperature=0.0,
        max_tokens=128,
    )
    bounded_report = (
        report if len(report) <= 12000 else f"{report[:6000]}\n\n[...REPORT MIDDLE OMITTED...]\n\n{report[-6000:]}"
    )
    messages = [
        {
            "role": "system",
            "content": (
                "You are a strict extractive QA formatter. Answer only from the "
                "supplied report. Return one minimal entity/date/number/yes-no "
                "answer with no label or explanation. If the report does not "
                "support an answer, return exactly INSUFFICIENT_EVIDENCE."
            ),
        },
        {
            "role": "user",
            "content": f"QUESTION:\n{question}\n\nREPORT:\n{bounded_report}",
        },
    ]
    response = await asyncio.to_thread(policy, messages)
    candidate = HotpotQABenchmark._clean_answer_candidate(str(response.get("content") or ""))
    candidate = re.sub(
        r"^(?:short\s+answer|answer|答案)\s*[:：-]\s*",
        "",
        candidate,
        flags=re.IGNORECASE,
    ).strip()
    if candidate.upper() == "INSUFFICIENT_EVIDENCE":
        return ""
    return candidate


def smoke_evaluation_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return an explicit low-cost configuration for real chain smoke tests."""
    result = copy.deepcopy(config)
    limits = result.setdefault("research", {}).setdefault("limits", {})
    limits.update(
        {
            "max_iterations": 8,
            "max_tool_calls": 12,
            "max_children": 0,
            "max_fork_depth": 0,
            "max_total_threads": 1,
            "max_total_tool_calls": 12,
            "max_elapsed_seconds": 120.0,
            "max_total_tokens": 30000,
            "max_retries_per_action": 1,
            "max_total_retries": 1,
        }
    )
    return result


def evaluation_config_with_tool_budget(
    config: dict[str, Any],
    max_total_tool_calls: int | None,
    max_tool_calls: int | None = None,
    max_total_tokens: int | None = None,
    max_elapsed_seconds: float | None = None,
) -> dict[str, Any]:
    """Return a copy with optional evaluation-only resource budgets."""
    result = copy.deepcopy(config)
    limits = result.setdefault("research", {}).setdefault("limits", {})
    if max_tool_calls is not None:
        limits["max_tool_calls"] = max_tool_calls
    if max_total_tool_calls is not None:
        limits["max_total_tool_calls"] = max_total_tool_calls
    if max_total_tokens is not None:
        limits["max_total_tokens"] = max_total_tokens
    if max_elapsed_seconds is not None:
        limits["max_elapsed_seconds"] = max_elapsed_seconds
    return result


def configured_tool_budget(config: dict[str, Any]) -> int:
    """Resolve the effective global tool budget recorded in evaluation output."""
    return int(
        config.get("research", {})
        .get("limits", {})
        .get("max_total_tool_calls", 36)
    )


def configured_local_tool_budget(config: dict[str, Any]) -> int:
    """Resolve the per-Agent tool budget recorded in evaluation output."""
    return int(config.get("research", {}).get("limits", {}).get("max_tool_calls", 12))


def configured_token_budget(config: dict[str, Any]) -> int:
    """Resolve the global token budget recorded in evaluation output."""
    return int(config.get("research", {}).get("limits", {}).get("max_total_tokens", 300000))


def configured_elapsed_budget(config: dict[str, Any]) -> float:
    """Resolve the per-question research wall-clock budget."""
    return float(
        config.get("research", {}).get("limits", {}).get("max_elapsed_seconds", 300.0)
    )


def configured_finalization_grace(config: dict[str, Any]) -> float:
    """Resolve the Root-only time added after the research deadline."""
    return float(
        config.get("research", {})
        .get("limits", {})
        .get("root_finalization_grace_seconds", 0.0)
    )


def judge_backend(config: dict[str, Any]) -> str:
    """Resolve the external evaluation backend without changing AgentGraph routing."""
    model = config.get("model", {})
    return str(
        model.get("backend_mapping", {}).get("judge", model.get("backend", "deepseek"))
    )


def judge_sampling_kwargs(config: dict[str, Any]) -> dict[str, Any]:
    """Resolve Judge-only sampling without changing Research AgentGraph policy."""
    model = config.get("model", {})
    backend = judge_backend(config)
    sampling = model.get("backend_sampling", {})
    result = dict(sampling.get(backend, {}))
    result.update(sampling.get("modules", {}).get("judge", {}))
    return result


def add_llm_judge_metrics(
    detail: dict[str, Any],
    judge_result: dict[str, Any],
) -> None:
    """Attach Judge output while keeping the ResearchBench rule score identifiable."""
    rule_score = float(detail.get("composite_score", 0.0))
    detail["rule_composite_score"] = rule_score
    detail["llm_judge"] = judge_result
    if "error" in judge_result:
        detail["judge_status"] = "failed"
        detail["judge_average"] = None
        detail["rule_judge_composite_score"] = None
        return
    judge_average = float(judge_result.get("average", 0.0))
    detail["judge_status"] = "valid"
    detail["judge_average"] = judge_average
    detail["rule_judge_composite_score"] = round(
        0.6 * rule_score + 0.4 * (judge_average / 10.0),
        6,
    )


@asynccontextmanager
async def evaluation_runtime(
    config: dict[str, Any],
    checkpoint_db_path: str | None,
):
    """Use durable evaluation checkpoints when requested by a long real run."""
    if checkpoint_db_path:
        durable_config = isolated_evaluation_config(config, checkpoint_db_path)
        async with open_research_runtime(
            checkpoint_db_path, config=durable_config
        ) as runtime:
            yield runtime
        return
    runtime = build_research_runtime(
        config=config,
        checkpointer=paperpilot_in_memory_saver(),
    )
    try:
        yield runtime
    finally:
        await runtime.close()


def isolated_evaluation_config(
    config: dict[str, Any],
    checkpoint_db_path: str,
) -> dict[str, Any]:
    """Keep durable benchmark artifacts isolated while preserving DB resume."""
    isolated = copy.deepcopy(config)
    root = Path(checkpoint_db_path).resolve().parent
    isolated.setdefault("research", {})["vault_root"] = str(root / "vault")
    isolated.setdefault("runtime", {})["retrieval_db_path"] = str(
        root / "retrieval.db"
    )
    isolated.setdefault("chat", {})["db_path"] = str(root / "chat.db")
    return isolated


async def run_or_resume_evaluation_workflow(
    runtime: Any,
    question: str,
    *,
    thread_id: str,
) -> ResearchWorkflowResult:
    """Resume a durable ResearchBench item instead of restarting its research."""
    state = await runtime.get_state(thread_id)
    existing = state.get("workflow_result") if state else None
    if isinstance(existing, ResearchWorkflowResult):
        return existing
    if state:
        if state.get("confirmed"):
            final = await runtime.continue_research(thread_id)
        else:
            status = str(state.get("workflow_status") or "")
            if status == "waiting_confirmation":
                final = await runtime.review(thread_id, "confirm")
            else:
                await runtime.continue_research(thread_id)
                final = await runtime.review(thread_id, "confirm")
    else:
        await runtime.start(question, thread_id=thread_id)
        final = await runtime.review(thread_id, "confirm")
    result = final.get("workflow_result")
    if not isinstance(result, ResearchWorkflowResult):
        raise RuntimeError("evaluation workflow ended without a structured result")
    return result


def workflow_metrics(result: ResearchWorkflowResult) -> dict[str, Any]:
    """Expose measured workflow counters; never infer a confidence score."""
    research = result.research_result
    source_refs = {item.source_ref for item in research.evidence if item.source_ref}
    status = getattr(research.status, "value", research.status)
    metrics = {
        "research_brief": asdict(result.brief),
        "status": str(status),
        "research_status": str(status),
        "termination_reason": (
            research.termination_reason.value
            if research.termination_reason is not None
            else None
        ),
        "output_status": research.output_status.value,
        "stop_reason": research.stop_reason,
        "rcs": research_completion_score(result),
        "evidence_count": len(research.evidence),
        "source_count": len(source_refs),
        "thread_count": research.thread_count,
        "tool_calls_used": research.tool_calls_used,
        "estimated_tokens_used": research.estimated_tokens_used,
        "retries_used": research.retries_used,
        "iterations": research.iterations,
        "unresolved_count": len(research.unresolved),
        "repair_applied": research.repair_applied,
        "repair_actions": list(research.repair_actions),
        "report_manifest": result.memory_manifest.report_path,
        "evidence_manifests": list(result.memory_manifest.evidence_paths),
        "source_manifests": list(result.memory_manifest.source_paths),
        "shared_comparison": result.shared_comparison,
        "structured_report": result.structured_report,
        "root_agent_report": result.root_agent_report,
        "shared_selected_evidence_count": result.shared_selected_evidence_count,
        "coordination_metrics": dict(result.coordination_metrics),
    }
    if result.research_architecture == "supervisor_v2":
        metrics["v2"] = v2_structure_metrics(result)
    if result.shared_comparison or (
        result.structured_report and not result.root_agent_report
    ):
        metrics["shared_structure"] = v2_structure_metrics(result)
    return metrics


def v2_structure_metrics(result: ResearchWorkflowResult) -> dict[str, Any]:
    """Expose deterministic V2 structure and citation gates."""
    from evaluation.metrics.rule_based import RuleBasedMetrics

    challenge_count = len(result.challenges)
    accepted = sum(
        item.get("status") in {"accepted", "resolved", "unresolved_disclosed"}
        for item in result.challenges
    )
    resolved = sum(item.get("status") == "resolved" for item in result.challenges)
    disclosed = sum(
        item.get("status") == "unresolved_disclosed" for item in result.challenges
    )
    invalid_citations = sum(
        item.get("category") in {"invalid", "locator"}
        and item.get("status", "pending") not in {"repaired", "removed"}
        for item in result.citation_issues
    )
    issue_spans = tuple(
        (
            str(item.get("claim_text") or "").strip(),
            str(item.get("category") or ""),
        )
        for item in result.citation_issues
        if str(item.get("claim_text") or "").strip()
    )
    citation_issue_conflicts = sum(
        first_category != second_category
        and {first_category, second_category} == {"invalid", "missing"}
        and (first_text in second_text or second_text in first_text)
        for index, (first_text, first_category) in enumerate(issue_spans)
        for second_text, second_category in issue_spans[index + 1:]
    )
    raw_url_count = len(re.findall(r"https?://", result.report_markdown, re.IGNORECASE))
    audit_log_leak_count = len(re.findall(
        r"Citation audit removed or downgraded:",
        result.report_markdown,
        re.IGNORECASE,
    ))
    requirement_rows = tuple(result.evidence_requirement_coverage)
    supported_requirements = sum(
        item.get("status") == "supported" for item in requirement_rows
    )
    weighted_requirement_coverage = sum(
        1.0 if item.get("status") == "supported"
        else 0.5 if item.get("status") == "weak"
        else 0.0
        for item in requirement_rows
    )
    primary_requirements = tuple(
        item for item in requirement_rows if item.get("primary_source_required")
    )
    supported_primary_requirements = sum(
        item.get("status") == "supported" and item.get("primary_source_present")
        for item in primary_requirements
    )
    audit_removed = sum(
        item.get("status") in {"removed", "repaired"}
        for item in result.citation_issues
    )
    return {
        "core_question_assignment_rate": (
            result.assigned_core_question_count / result.core_question_count
            if result.core_question_count else 0.0
        ),
        "worker_duplicate_rate": (
            1.0 - result.unique_worker_packet_count / result.worker_packet_count
            if result.worker_packet_count else 0.0
        ),
        "source_open_ratio": (
            result.source_open_count / result.source_candidate_count
            if result.source_candidate_count else 0.0
        ),
        "search_to_open_rate": (
            result.source_open_count / result.source_candidate_count
            if result.source_candidate_count else 0.0
        ),
        "duplicate_source_rate": (
            result.duplicate_source_count / result.source_candidate_count
            if result.source_candidate_count else 0.0
        ),
        "acquisition_call_count": result.acquisition_call_count,
        "evidence_per_acquisition": (
            len(result.research_result.evidence) / result.acquisition_call_count
            if result.acquisition_call_count else 0.0
        ),
        "challenge_acceptance_rate": accepted / challenge_count if challenge_count else 1.0,
        "challenge_resolution_rate": resolved / accepted if accepted else 1.0,
        "unresolved_challenge_disclosure_rate": (
            disclosed / (accepted - resolved) if accepted > resolved else 1.0
        ),
        "material_claim_citation_coverage": RuleBasedMetrics.material_claim_citation_coverage(
            result.report_markdown
        ),
        "invalid_citation_count": int(invalid_citations),
        "raw_url_count_before_render": raw_url_count,
        "citation_issue_conflict_count": citation_issue_conflicts,
        "audit_log_leak_count": audit_log_leak_count,
        "reportable_claim_rejection_count": result.reportable_claim_rejection_count,
        "claim_entailment_pass_rate": (
            result.entailed_assessment_count / result.support_assessment_count
            if result.support_assessment_count else 0.0
        ),
        "verified_claim_yield": (
            result.verified_claim_count / result.candidate_claim_count
            if result.candidate_claim_count else 0.0
        ),
        "evidence_requirement_coverage_rate": (
            supported_requirements / len(requirement_rows)
            if requirement_rows else 0.0
        ),
        "weighted_evidence_requirement_coverage_rate": (
            weighted_requirement_coverage / len(requirement_rows)
            if requirement_rows else 0.0
        ),
        "primary_source_requirement_coverage_rate": (
            supported_primary_requirements / len(primary_requirements)
            if primary_requirements else 1.0
        ),
        "composer_claim_survival_rate": (
            result.composer_claim_count / result.verified_claim_count
            if result.verified_claim_count else 0.0
        ),
        "citation_audit_removal_rate": (
            audit_removed / result.composer_claim_count
            if result.composer_claim_count else 0.0
        ),
        "verified_claims_in_report": result.composer_claim_count,
        "finalization_reserve_tokens": result.finalization_token_reserve,
        "supplemental_wave_count": result.supplemental_wave_count,
        "repair_applied": result.repair_applied,
        "repair_actions": list(result.repair_actions),
    }


def v2_canary_gate(detail: dict[str, Any]) -> tuple[bool, tuple[str, ...]]:
    """Apply the documented one-question gate before any sample expansion."""
    failures: list[str] = []
    metrics = detail.get("v2") if isinstance(detail.get("v2"), dict) else {}
    if detail.get("output_status") != "valid":
        failures.append("output_status_not_valid")
    research_status = detail.get("research_status", detail.get("status"))
    if research_status is not None and research_status != "completed":
        failures.append("research_status_not_completed")
    if detail.get("termination_reason") == "budget_forced":
        failures.append("budget_forced")
    if float(metrics.get("core_question_assignment_rate", 0.0)) < 1.0:
        failures.append("core_question_assignment_below_100pct")
    if float(metrics.get("material_claim_citation_coverage", 0.0)) < 0.8:
        failures.append("material_claim_citation_coverage_below_80pct")
    if int(metrics.get("invalid_citation_count", 0)) != 0:
        failures.append("invalid_citations_present")
    if int(metrics.get("raw_url_count_before_render", 0)) != 0:
        failures.append("raw_urls_present")
    if int(metrics.get("citation_issue_conflict_count", 0)) != 0:
        failures.append("citation_issue_conflicts_present")
    if int(metrics.get("audit_log_leak_count", 0)) != 0:
        failures.append("citation_audit_log_leaked")
    if int(metrics.get("finalization_reserve_tokens", 0)) <= 0:
        failures.append("finalization_reserve_missing")
    judge_average = detail.get("judge_average")
    if not isinstance(judge_average, (int, float)) or float(judge_average) < 5.0:
        failures.append("judge_average_below_5")
    return not failures, tuple(failures)


def research_completion_score(result: ResearchWorkflowResult) -> dict[str, float]:
    """Return post-run RCS dimensions; this function is never used for routing."""
    research = result.research_result
    coverage = tuple(research.coverage)
    total = len(coverage)
    if total:
        supported = sum(item.status.value == "supported" for item in coverage)
        evidence_points = sum(
            {
                "supported": 1.0,
                "weak": 0.5,
                "conflicted": 0.25,
                "unsupported": 0.0,
            }[item.status.value]
            for item in coverage
        )
        conflicted = sum(item.status.value == "conflicted" for item in coverage)
        incomplete_ids = {
            item.requirement_id
            for item in coverage
            if item.status.value != "supported"
        }
        disclosed_ids = {item.requirement_id for item in research.critical_gaps}
        objective_coverage = supported / total
        evidence_sufficiency = evidence_points / total
        conflict_resolution = 1.0 - (conflicted / total)
        uncertainty_calibration = (
            1.0
            if not incomplete_ids
            else len(incomplete_ids & disclosed_ids) / len(incomplete_ids)
        )
    else:
        objective_coverage = 0.0
        evidence_sufficiency = 0.0
        conflict_resolution = 0.0
        uncertainty_calibration = 0.0
    denominator = max(1, total * 4)
    research_efficiency = objective_coverage / (
        1.0 + research.tool_calls_used / denominator
    )
    return {
        "objective_coverage": round(objective_coverage, 6),
        "evidence_sufficiency": round(evidence_sufficiency, 6),
        "conflict_resolution": round(conflict_resolution, 6),
        "uncertainty_calibration": round(uncertainty_calibration, 6),
        "research_efficiency": round(research_efficiency, 6),
    }


def researchbench_summary(
    report: EvaluationReport,
    *,
    budget: int,
    local_budget: int,
    token_budget: int,
    elapsed_budget: float = 300.0,
    finalization_grace: float = 0.0,
) -> dict[str, Any]:
    """Aggregate comparison-ready ResearchBench metrics without inventing data."""
    details = report.details
    scores = [detail["composite_score"] for detail in details if "composite_score" in detail]
    successful = [detail for detail in details if "error" not in detail]
    rcs_details = [detail["rcs"] for detail in successful if "rcs" in detail]
    judge_details = [
        detail for detail in successful if detail.get("judge_status") == "valid"
    ]

    def counts(field: str, *, default: str = "unknown") -> dict[str, int]:
        return dict(Counter(str(detail.get(field) or default) for detail in details))

    return {
        "budget": budget,
        "local_tool_budget": local_budget,
        "token_budget": token_budget,
        "elapsed_budget_seconds": elapsed_budget,
        "root_finalization_grace_seconds": finalization_grace,
        "average_composite": sum(scores) / len(scores) if scores else 0.0,
        "average_rule_composite": sum(scores) / len(scores) if scores else 0.0,
        "average_judge": (
            sum(float(detail["judge_average"]) for detail in judge_details)
            / len(judge_details)
            if judge_details
            else None
        ),
        "average_rule_judge_composite": (
            sum(float(detail["rule_judge_composite_score"]) for detail in judge_details)
            / len(judge_details)
            if judge_details
            else None
        ),
        "judge_success": len(judge_details),
        "judge_failed": sum(
            detail.get("judge_status") == "failed" for detail in successful
        ),
        "num_success": len(successful),
        "num_failed": len(details) - len(successful),
        "elapsed_seconds": sum(detail.get("elapsed_seconds", 0.0) for detail in details),
        "research_elapsed_seconds": sum(
            detail.get("research_elapsed_seconds", 0.0) for detail in details
        ),
        "rule_evaluation_elapsed_seconds": sum(
            detail.get("rule_evaluation_elapsed_seconds", 0.0) for detail in details
        ),
        "judge_elapsed_seconds": sum(
            detail.get("judge_elapsed_seconds", 0.0) for detail in details
        ),
        "evidence_count": sum(detail.get("evidence_count", 0) for detail in details),
        "source_count": sum(detail.get("source_count", 0) for detail in details),
        "thread_count": sum(detail.get("thread_count", 0) for detail in details),
        "tool_calls_used": sum(detail.get("tool_calls_used", 0) for detail in details),
        "estimated_tokens_used": sum(
            detail.get("estimated_tokens_used", 0) for detail in details
        ),
        "iterations": sum(detail.get("iterations", 0) for detail in details),
        "unresolved_count": sum(detail.get("unresolved_count", 0) for detail in details),
        "retries_used": sum(detail.get("retries_used", 0) for detail in details),
        "research_status_counts": counts("research_status"),
        "termination_reason_counts": counts("termination_reason", default="not_available"),
        "output_status_counts": counts("output_status", default="not_available"),
        "average_rcs": {
            dimension: (
                sum(item.get(dimension, 0.0) for item in rcs_details) / len(rcs_details)
                if rcs_details
                else 0.0
            )
            for dimension in (
                "objective_coverage",
                "evidence_sufficiency",
                "conflict_resolution",
                "uncertainty_calibration",
                "research_efficiency",
            )
        },
    }


async def _evaluate_research_bench(
    num_questions: int,
    domain: str | None,
    config: dict[str, Any],
    question_ids: list[str] | None = None,
    stratified: bool = False,
    use_llm_judge: bool = False,
    checkpoint_db_path: str | None = None,
    progress_output_dir: str | None = None,
) -> EvaluationReport:
    logger = logging.getLogger("run_eval")
    bench = ResearchBench()
    questions = bench.get_questions(
        domain=domain,
        n=None if question_ids else num_questions,
        question_ids=question_ids,
        stratified=stratified,
    )
    report = EvaluationReport(name="ResearchBench_Evaluation", num_questions=len(questions))
    budget = configured_tool_budget(config)
    local_budget = configured_local_tool_budget(config)
    token_budget = configured_token_budget(config)
    elapsed_budget = configured_elapsed_budget(config)
    finalization_grace = configured_finalization_grace(config)
    judge = (
        LLMJudge(
            backend=judge_backend(config),
            **judge_sampling_kwargs(config),
        )
        if use_llm_judge
        else None
    )
    async with evaluation_runtime(config, checkpoint_db_path) as runtime:
        for index, question in enumerate(questions, 1):
            question_id = question["id"]
            logger.info("[%s/%s] Evaluating %s", index, len(questions), question_id)
            started = time.monotonic()
            research_elapsed = 0.0
            rule_elapsed = 0.0
            judge_elapsed = 0.0
            try:
                thread_id = f"researchbench-{question_id}"
                research_started = time.monotonic()
                workflow = await run_or_resume_evaluation_workflow(
                    runtime,
                    question["query"],
                    thread_id=thread_id,
                )
                research_elapsed = time.monotonic() - research_started
                rule_started = time.monotonic()
                detail = bench.evaluate_report(workflow.report_markdown, question_id)
                rule_elapsed = time.monotonic() - rule_started
                detail.update(workflow_metrics(workflow))
                detail["budget"] = budget
                detail["local_tool_budget"] = local_budget
                detail["token_budget"] = token_budget
                detail["elapsed_budget_seconds"] = elapsed_budget
                detail["root_finalization_grace_seconds"] = finalization_grace
                detail["checkpoint_thread_id"] = thread_id
                detail["checkpoint_db_path"] = checkpoint_db_path
                if judge is not None:
                    judge_started = time.monotonic()
                    judge_result = await asyncio.to_thread(
                        judge.score_single,
                        workflow.report_markdown,
                        question["query"],
                        question.get("ground_truth"),
                    )
                    judge_elapsed = time.monotonic() - judge_started
                    add_llm_judge_metrics(detail, judge_result)
                detail["research_elapsed_seconds"] = research_elapsed
                detail["rule_evaluation_elapsed_seconds"] = rule_elapsed
                detail["judge_elapsed_seconds"] = judge_elapsed
                detail["elapsed_seconds"] = time.monotonic() - started
                report.add_detail(detail)
            except Exception as exc:
                logger.warning("Evaluation failed for %s: %s", question_id, exc)
                report.add_detail(
                    {
                        "question_id": question_id,
                        "status": "failed",
                        "error": str(exc),
                        "composite_score": 0.0,
                        "budget": budget,
                        "local_tool_budget": local_budget,
                        "token_budget": token_budget,
                        "elapsed_budget_seconds": elapsed_budget,
                        "root_finalization_grace_seconds": finalization_grace,
                        "research_elapsed_seconds": research_elapsed,
                        "rule_evaluation_elapsed_seconds": rule_elapsed,
                        "judge_elapsed_seconds": judge_elapsed,
                        "elapsed_seconds": time.monotonic() - started,
                    }
                )
            if progress_output_dir:
                report.set_summary(
                    researchbench_summary(
                        report,
                        budget=budget,
                        local_budget=local_budget,
                        token_budget=token_budget,
                        elapsed_budget=elapsed_budget,
                        finalization_grace=finalization_grace,
                    )
                )
                report.save(
                    progress_output_dir,
                    filename="ResearchBench_Evaluation_progress.json",
                )

    report.set_summary(
        researchbench_summary(
            report,
            budget=budget,
            local_budget=local_budget,
            token_budget=token_budget,
            elapsed_budget=elapsed_budget,
            finalization_grace=finalization_grace,
        )
    )
    return report


def evaluate_research_bench(
    num_questions: int,
    domain: str | None,
    config: dict[str, Any],
    question_ids: list[str] | None = None,
    stratified: bool = False,
    use_llm_judge: bool = False,
    checkpoint_db_path: str | None = None,
    progress_output_dir: str | None = None,
) -> EvaluationReport:
    return asyncio.run(
        _evaluate_research_bench(
            num_questions,
            domain,
            config,
            question_ids=question_ids,
            stratified=stratified,
            use_llm_judge=use_llm_judge,
            checkpoint_db_path=checkpoint_db_path,
            progress_output_dir=progress_output_dir,
        )
    )


async def _evaluate_hotpotqa(
    num_questions: int,
    config: dict[str, Any],
    use_mock: bool,
    seed: int,
) -> EvaluationReport:
    logger = logging.getLogger("run_eval")
    bench = HotpotQABenchmark(use_mock=use_mock)
    questions = bench.get_samples(n=num_questions, shuffle=True, seed=seed)
    report = EvaluationReport(name="HotpotQA_PaperPilot_Evaluation", num_questions=len(questions))
    budget = configured_tool_budget(config)
    local_budget = configured_local_tool_budget(config)
    token_budget = configured_token_budget(config)
    predictions: list[dict[str, Any]] = []
    runtime = build_research_runtime(
        config=config,
        checkpointer=paperpilot_in_memory_saver(),
    )
    try:
        for index, question in enumerate(questions, 1):
            query = question["query"]
            gold = question["expected_answer"]
            started = time.monotonic()
            answer_extraction_error: str | None = None
            try:
                workflow = await runtime.run_auto_confirmed(
                    query,
                    thread_id=runtime.new_thread_id(),
                )
                report_text = workflow.report_markdown
                prediction = bench.extract_short_answer(report_text, question=query)
                extraction_method = bench.short_answer_extraction_method(report_text, question=query)
                if not extraction_method.startswith("explicit_"):
                    fallback_method = extraction_method
                    try:
                        prediction = await extract_grounded_hotpotqa_answer(
                            report_text,
                            query,
                            config,
                        )
                        extraction_method = "grounded_model_extraction" if prediction else "grounded_model_refusal"
                    except Exception as exc:
                        answer_extraction_error = f"{type(exc).__name__}: {exc}"
                        logger.warning(
                            "Grounded answer extraction failed for item %s: %s",
                            index,
                            exc,
                        )
                        extraction_method = f"{fallback_method}_after_model_failure"
                structured = workflow_metrics(workflow)
            except Exception as exc:
                logger.warning("Evaluation failed for item %s: %s", index, exc)
                report_text = ""
                prediction = ""
                extraction_method = "research_failed"
                structured = {"status": "failed", "error": str(exc)}

            depth = bench.evaluate_report(report_text, gold) if report_text else {}
            predictions.append(
                {
                    "query_id": index,
                    "prediction": prediction,
                    "gold": gold,
                    "report": report_text,
                    "depth_metrics": depth,
                }
            )
            report.add_detail(
                {
                    "query_id": index,
                    "query": query,
                    "prediction": prediction,
                    "gold": gold,
                    "extraction_method": extraction_method,
                    "answer_extraction_error": answer_extraction_error,
                    "depth_metrics": depth,
                    "budget": budget,
                    "local_tool_budget": local_budget,
                    "token_budget": token_budget,
                    "elapsed_seconds": time.monotonic() - started,
                    **structured,
                }
            )
    finally:
        await runtime.close()

    summary = bench.evaluate(predictions, metrics=["em", "f1", "pass@1"])
    summary.update(
        {
            "budget": budget,
            "local_tool_budget": local_budget,
            "token_budget": token_budget,
            "num_success": sum(detail.get("status") != "failed" for detail in report.details),
            "num_failed": sum(detail.get("status") == "failed" for detail in report.details),
            "evidence_count": sum(detail.get("evidence_count", 0) for detail in report.details),
            "thread_count": sum(detail.get("thread_count", 0) for detail in report.details),
            "tool_calls_used": sum(detail.get("tool_calls_used", 0) for detail in report.details),
            "estimated_tokens_used": sum(detail.get("estimated_tokens_used", 0) for detail in report.details),
            "retries_used": sum(detail.get("retries_used", 0) for detail in report.details),
        }
    )
    report.set_summary(summary)
    return report


def evaluate_hotpotqa(
    num_questions: int,
    config: dict[str, Any],
    use_mock: bool = False,
    seed: int = 42,
) -> EvaluationReport:
    return asyncio.run(_evaluate_hotpotqa(num_questions, config, use_mock, seed))


def main() -> None:
    parser = argparse.ArgumentParser(description="PaperPilot benchmark evaluation")
    parser.add_argument(
        "--benchmark",
        choices=["research_bench", "hotpotqa", "memory_wiki"],
        required=True,
    )
    parser.add_argument("--num-questions", "--num_questions", type=int, default=20)
    parser.add_argument("--domain", default=None)
    parser.add_argument(
        "--question-ids",
        nargs="+",
        default=None,
        help="Fixed ResearchBench question IDs in evaluation order",
    )
    parser.add_argument(
        "--stratified",
        action="store_true",
        help="Select ResearchBench questions by deterministic domain round-robin",
    )
    parser.add_argument("--use-mock", "--use_mock", action="store_true")
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Deterministic HotpotQA sample seed (default: 42)",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Use explicit low-cost limits for a real end-to-end chain check",
    )
    parser.add_argument(
        "--max-tool-calls",
        type=int,
        default=None,
        help="Evaluation-only per-Agent tool-call budget override",
    )
    parser.add_argument(
        "--max-total-tool-calls",
        type=int,
        default=None,
        help="Evaluation-only global tool-call budget override",
    )
    parser.add_argument(
        "--max-total-tokens",
        type=int,
        default=None,
        help="Evaluation-only global token budget override",
    )
    parser.add_argument(
        "--max-elapsed-seconds",
        type=float,
        default=None,
        help="Evaluation-only per-question wall-clock budget override",
    )
    parser.add_argument(
        "--llm-judge",
        action="store_true",
        help="Run the configured external LLM Judge for every generated ResearchBench report",
    )
    parser.add_argument(
        "--checkpoint-db",
        default=None,
        help="Persistent SQLite checkpoint path for resumable long evaluations",
    )
    parser.add_argument("--config", default=None)
    parser.add_argument("--output-dir", "--output_dir", default="outputs/evaluation")
    parser.add_argument(
        "--log-level",
        "--log_level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()
    if args.max_tool_calls is not None and args.max_tool_calls < 1:
        parser.error("--max-tool-calls must be at least 1")
    if args.max_total_tool_calls is not None and args.max_total_tool_calls < 1:
        parser.error("--max-total-tool-calls must be at least 1")
    if args.max_total_tokens is not None and args.max_total_tokens < 1:
        parser.error("--max-total-tokens must be at least 1")
    if args.max_elapsed_seconds is not None and args.max_elapsed_seconds <= 0:
        parser.error("--max-elapsed-seconds must be positive")
    if args.smoke and (
        args.max_tool_calls is not None
        or args.max_total_tool_calls is not None
        or args.max_total_tokens is not None
        or args.max_elapsed_seconds is not None
    ):
        parser.error("--smoke cannot be combined with tool-budget overrides")
    if args.benchmark != "research_bench" and (args.question_ids or args.stratified or args.domain):
        parser.error("--question-ids, --stratified and --domain apply only to research_bench")
    if args.question_ids and (args.stratified or args.domain):
        parser.error("--question-ids cannot be combined with --stratified or --domain")
    setup_logging(args.log_level)
    config = load_config(args.config)
    if args.smoke:
        config = smoke_evaluation_config(config)
    else:
        config = evaluation_config_with_tool_budget(
            config,
            args.max_total_tool_calls,
            max_tool_calls=args.max_tool_calls,
            max_total_tokens=args.max_total_tokens,
            max_elapsed_seconds=args.max_elapsed_seconds,
        )

    if args.benchmark == "memory_wiki":
        report = evaluate_memory_wiki()
    elif args.benchmark == "research_bench":
        report = evaluate_research_bench(
            args.num_questions,
            args.domain,
            config,
            question_ids=args.question_ids,
            stratified=args.stratified,
            use_llm_judge=args.llm_judge,
            checkpoint_db_path=args.checkpoint_db,
            progress_output_dir=args.output_dir,
        )
    else:
        report = evaluate_hotpotqa(
            args.num_questions,
            config,
            use_mock=args.use_mock,
            seed=args.seed,
        )

    path = report.save(args.output_dir)
    logging.getLogger("run_eval").info("Evaluation report saved: %s", path)
    print(json.dumps(report.summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
