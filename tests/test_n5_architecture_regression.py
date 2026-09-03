"""Regression tests for the post-N5 PaperPilot architecture cleanup."""
from __future__ import annotations

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ROOTS = ("src", "scripts", "web", "evaluation")

REMOVED_SOURCE_DIRS = (
    "src/orchestrator",
    "src/planner",
    "src/agents",
    "src/evidence",
    "src/compressor",
    "src/adversarial",
    "src/evolution",
    "src/core",
)
REMOVED_FILES = (
    "src/core/runner.py",
    "src/core/ablation.py",
    "src/core/judge.py",
    "src/memory/embedder.py",
    "src/memory/memory_store.py",
    "src/memory/long_term.py",
    "src/memory/short_term.py",
    "scripts/run_ablation.py",
    "scripts/run_evolution.py",
    "scripts/backfill_reports.py",
    "scripts/viz_evidence_graph.py",
    "scripts/run_all_experiments.py",
    "evaluation/run_baseline.py",
    "evaluation/analyze_ablation.py",
    "evaluation/metrics.py",
)
REMOVED_IMPORT_PREFIXES = (
    "src.orchestrator",
    "src.planner",
    "src.agents",
    "src.evidence",
    "src.compressor",
    "src.adversarial",
    "src.evolution",
    "src.core.runner",
    "src.core.ablation",
    "src.core.judge",
    "src.memory.embedder",
    "src.memory.memory_store",
    "src.memory.long_term",
    "src.memory.short_term",
)
REMOVED_ACTIVE_SYMBOLS = (
    "AgentPool",
    "EvidenceGraph",
    "EvidenceStore",
    "SummarizerAgent",
    "run_gap_analysis",
    "evidence_relations",
    "export-obsidian",
    "vis-network",
)
AGENT_LIMIT_FIELDS = {
    "max_iterations",
    "max_tool_calls",
    "max_tool_output_chars",
    "max_children_per_agent",
    "max_fork_depth",
    "max_concurrent_agents",
    "max_total_agents",
    "max_total_tool_calls",
    "max_elapsed_seconds",
    "root_finalization_grace_seconds",
    "max_total_tokens",
    "max_retries_per_action",
    "max_total_retries",
}


def _production_python_files() -> list[Path]:
    return sorted(
        path
        for root_name in PRODUCTION_ROOTS
        for path in (ROOT / root_name).rglob("*.py")
    )


def test_removed_old_architecture_files_are_absent() -> None:
    leftovers = [name for name in REMOVED_FILES if (ROOT / name).exists()]
    for directory in REMOVED_SOURCE_DIRS:
        leftovers.extend(
            str(path.relative_to(ROOT)) for path in (ROOT / directory).glob("*.py")
        )
    assert leftovers == []


def test_production_code_has_no_old_architecture_imports_or_active_symbols() -> None:
    import_pattern = re.compile(
        rf"(?:from|import)\s+(?:{'|'.join(re.escape(v) for v in REMOVED_IMPORT_PREFIXES)})\b"
    )
    violations: list[str] = []
    for path in _production_python_files():
        text = path.read_text(encoding="utf-8")
        if import_pattern.search(text):
            violations.append(f"{path.relative_to(ROOT)}: old architecture import")
        for symbol in REMOVED_ACTIVE_SYMBOLS:
            if symbol in text:
                violations.append(f"{path.relative_to(ROOT)}: {symbol}")
    assert violations == []


def test_default_config_contains_only_active_runtime_sections() -> None:
    config = yaml.safe_load((ROOT / "configs/default.yaml").read_text(encoding="utf-8"))
    assert set(config) == {
        "system",
        "model",
        "research",
        "content_extraction",
        "chat",
        "tools",
        "runtime",
    }
    assert set(config["model"]["backend_mapping"]) == {"research", "judge"}
    assert set(config["model"]["backend_sampling"]["modules"]) == {"research", "judge"}
    assert set(config["research"]["limits"]) == AGENT_LIMIT_FIELDS
    assert config["research"]["limits"]["max_total_tokens"] == 700000
    assert config["research"]["structured_report"]["enabled"] is True
    assert config["research"]["homogeneous_fork"] == {
        "enabled": True,
        "explicit_control_decision": True,
        "budget_leases_enabled": True,
        "reconsider_after_local_rounds": 2,
        "parent_merge_reserve_tokens": 50000,
        "root_final_max_tokens": 32768,
        "root_final_output_token_budget": 50000,
        "initial_child_lease_tokens": 60000,
        "child_topup_tokens": 25000,
        "max_child_lease_tokens": 125000,
    }
    assert config["research"]["supervisor_v2"]["red_review_enabled"] is False
    assert config["research"]["supervisor_v2"]["max_red_review_rounds"] == 0
    assert config["research"]["report_review"]["enabled"] is False
    assert config["research"]["vault_root"]
    assert "memory_root" not in config["research"]
    assert config["chat"]["db_path"]
    assert config["runtime"]["retrieval_db_path"]
    assert config["content_extraction"]["mode"] == "structured"
    assert config["tools"]["enabled"] == [
        "acquire_evidence",
        "arxiv_reader",
        "file_reader",
        "calculator",
        "notepad",
    ]
    assert "execution" not in config["tools"]


def test_product_default_reuses_baseline_core_without_evaluation_storage() -> None:
    product = yaml.safe_load(
        (ROOT / "configs/default.yaml").read_text(encoding="utf-8")
    )
    baseline = yaml.safe_load(
        (ROOT / "configs/researchbench-r8-tech.yaml").read_text(encoding="utf-8")
    )

    assert product["model"] == baseline["model"]
    assert product["research"]["architecture"] == baseline["research"]["architecture"]
    assert product["research"]["structured_report"] == baseline["research"]["structured_report"]
    assert product["research"]["homogeneous_fork"] == baseline["research"]["homogeneous_fork"]
    assert product["research"]["limits"] == baseline["research"]["limits"]
    assert product["content_extraction"] == baseline["content_extraction"]
    assert product["tools"] == baseline["tools"]
    assert product["research"]["vault_root"] == "memory"
    assert product["runtime"]["retrieval_db_path"] == "data/retrieval.db"
    assert product["research"]["vault_root"] != baseline["research"]["vault_root"]
    assert product["runtime"]["retrieval_db_path"] != baseline["runtime"]["retrieval_db_path"]


def test_tech_evaluation_uses_global_leases_and_700k_budget() -> None:
    config = yaml.safe_load(
        (ROOT / "configs/researchbench-r8-tech.yaml").read_text(encoding="utf-8")
    )

    assert config["research"]["homogeneous_fork"]["budget_leases_enabled"] is True
    assert config["research"]["limits"]["max_total_tokens"] == 700000


def test_packaging_has_no_removed_dependencies_or_entrypoints() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for removed in ("networkx", "run-ablation", "run-evolution", "self-evolution", "adversarial"):
        assert removed not in pyproject.lower()


def test_web_ui_has_no_old_graph_or_export_surface() -> None:
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    for removed in (
        "vis-network",
        "renderGraph",
        "renderRelationsList",
        "/graph",
        "export-obsidian",
        "ADVERSARIAL",
    ):
        assert removed not in html
    assert "setTimeout(() => openEventStream(task_id)" in html


def test_rcs_stays_out_of_runtime_research_routing() -> None:
    violations = []
    for path in sorted((ROOT / "src/research").rglob("*.py")):
        text = path.read_text(encoding="utf-8").lower()
        if "research_completion_score" in text or re.search(r"\brcs\b", text):
            violations.append(str(path.relative_to(ROOT)))
    assert violations == []
