#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/run_ablation.py
================================================================================
消融实验入口脚本（合并了原 run_baseline.py + run_adversarial_ablation.py）。

支持两种消融模式:
  --mode module : 模块消融 (full / no_adversarial / no_compressor / no_memory / no_evolution)
  --mode rounds : 对抗轮数消融 (0/1/2/3 轮)

统计增强:
  - 每道题保留配对分数
  - full vs 消融配置输出 bootstrap 95% CI + p-value
  - 输出 Cohen's d 效应量

Usage:
    python scripts/run_ablation.py --mode module --questions 10
    python scripts/run_ablation.py --mode rounds --questions 10 --max_rounds 3
================================================================================
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.runner import initialize_modules, load_config, run_research, setup_logging
from src.core.ablation import AblationStudy
from evaluation.benchmarks.research_bench import ResearchBench
from evaluation.metrics.rule_based import RuleBasedMetrics
from evaluation.metrics.stats import bootstrap_ci_paired, cohens_d


def evaluate_with_rules(report: str, qid: str, bench: ResearchBench) -> dict[str, Any]:
    """用规则指标对单篇报告评分。"""
    q = next((x for x in bench.questions if x["id"] == qid), None)
    if q is None:
        raise ValueError(f"未找到题目 ID: {qid}")

    expected_topics = q.get("expected_topics", [])
    ground_truth = q.get("ground_truth", {})

    factual = RuleBasedMetrics.fact_accuracy(report, ground_truth)
    hallucination = RuleBasedMetrics.hallucination_rate(report)
    citation = RuleBasedMetrics.citation_coverage(report)
    logic = RuleBasedMetrics.logical_consistency(report)
    comprehensive = RuleBasedMetrics.comprehensiveness(report, expected_topics)
    bias_score = max(0.0, 1.0 - hallucination)

    metrics = {
        "factual_accuracy": factual,
        "logical_consistency": logic,
        "citation_coverage": citation,
        "bias": bias_score,
        "comprehensiveness": comprehensive,
    }
    composite = RuleBasedMetrics.composite_score(metrics)

    return {
        "question_id": qid,
        "metrics": metrics,
        "composite_score": composite,
        "hallucination_rate": hallucination,
    }


def run_single_system(
    system_name: str,
    desc: str,
    config: dict,
    overrides: dict,
    questions: list[dict[str, Any]],
    bench: ResearchBench,
) -> dict[str, Any]:
    """跑单个系统配置，返回每道题的评分结果。"""
    print(f"\n{'='*60}")
    print(f"[消融] {system_name}: {desc}")
    print(f"{'='*60}")

    cfg = AblationStudy.override_config(config, overrides)
    modules = initialize_modules(cfg)

    per_question_scores: dict[str, float] = {}
    details: list[dict[str, Any]] = []

    for q in questions:
        qid = q["id"]
        query = q["query"]
        print(f"  [{qid}] {query[:60]}...")

        start = time.time()
        try:
            report = asyncio.run(run_research(query, cfg, modules))
            elapsed = time.time() - start
            eval_result = evaluate_with_rules(report, qid, bench)
            composite = eval_result["composite_score"]
            per_question_scores[qid] = composite
            details.append({
                "question_id": qid,
                "query": query,
                "composite_score": composite,
                "metrics": eval_result["metrics"],
                "elapsed_seconds": elapsed,
            })
            print(f"    → composite={composite:.3f}, time={elapsed:.1f}s")
        except Exception as e:
            print(f"    → FAILED: {e}")
            per_question_scores[qid] = 0.0
            details.append({
                "question_id": qid,
                "query": query,
                "error": str(e),
                "composite_score": 0.0,
            })

    scores = list(per_question_scores.values())
    avg = sum(scores) / len(scores) if scores else 0.0
    return {
        "system_name": system_name,
        "description": desc,
        "average_composite_score": avg,
        "per_question_scores": per_question_scores,
        "details": details,
    }


def compute_ablation_stats(
    full_result: dict[str, Any],
    ablation_result: dict[str, Any],
) -> dict[str, Any]:
    """计算 full vs 消融配置的统计显著性（配对差异）。"""
    full_scores = []
    ablation_scores = []

    for qid in full_result["per_question_scores"]:
        if qid in ablation_result["per_question_scores"]:
            full_scores.append(full_result["per_question_scores"][qid])
            ablation_scores.append(ablation_result["per_question_scores"][qid])

    diffs = [f - a for f, a in zip(full_scores, ablation_scores)]
    stats = bootstrap_ci_paired(diffs)
    effect = cohens_d(full_scores, ablation_scores)

    return {
        **stats,
        "cohens_d": round(effect, 4),
        "full_mean": round(sum(full_scores) / len(full_scores), 4) if full_scores else 0.0,
        "ablation_mean": round(sum(ablation_scores) / len(ablation_scores), 4) if ablation_scores else 0.0,
    }


def run_module_ablation(config: dict, questions: list[dict[str, Any]], output_dir: str) -> None:
    """运行模块消融实验，输出统计显著性。"""
    bench = ResearchBench()
    systems = AblationStudy.DEFAULT_MODULE_ABLATIONS

    # 跑所有配置
    all_results: dict[str, dict[str, Any]] = {}
    for name, (desc, overrides) in systems.items():
        result = run_single_system(name, desc, config, overrides, questions, bench)
        all_results[name] = result

    # 以 full 为基准，计算统计显著性
    full_result = all_results["full"]
    stats_report: dict[str, Any] = {}
    for name, result in all_results.items():
        if name == "full":
            continue
        stats_report[name] = compute_ablation_stats(full_result, result)

    # 组装输出
    report = {
        "evaluation_name": "DeepResearch Agent 模块消融实验（含统计显著性）",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "num_questions": len(questions),
        "systems": [
            {
                "system_name": r["system_name"],
                "description": r["description"],
                "average_composite_score": r["average_composite_score"],
                "details": r["details"],
            }
            for r in all_results.values()
        ],
        "summary": {r["system_name"]: r["average_composite_score"] for r in all_results.values()},
        "statistical_tests": stats_report,
    }

    filepath = AblationStudy.save_results(report, output_dir, prefix="module_ablation")

    # 打印摘要
    print(f"\n{'='*60}")
    print("模块消融摘要 + 统计显著性")
    print(f"{'='*60}")
    for name, score in report["summary"].items():
        print(f"  {name:20s}: {score:.4f}")

    print(f"\n统计检验 (full vs 消融, 配对 bootstrap 95% CI):")
    for name, st in stats_report.items():
        sig = "✓ 显著" if st["significant"] else "✗ 不显著"
        print(f"  {name:20s}: Δ={st['mean_diff']:+.4f} "
              f"CI=[{st['ci_lower']:+.4f}, {st['ci_upper']:+.4f}] "
              f"p={st['p_value']:.4f} d={st['cohens_d']:.3f} {sig}")
    print(f"\n结果已保存: {filepath}")


def run_rounds_ablation(config: dict, questions: list[dict[str, Any]], max_rounds: int, output_dir: str) -> None:
    """运行对抗轮数消融实验，输出统计显著性。"""
    bench = ResearchBench()

    all_results: dict[str, dict[str, Any]] = {}
    for rounds in range(max_rounds + 1):
        desc = f"对抗轮数={rounds}"
        overrides = {
            "adversarial": {"max_rounds": rounds, "enabled": rounds > 0}
        }
        result = run_single_system(f"adv_{rounds}", desc, config, overrides, questions, bench)
        all_results[f"adv_{rounds}"] = result

    # 以 adv_0 为基准，计算与 adv_N 的差异
    base_result = all_results["adv_0"]
    stats_report: dict[str, Any] = {}
    for name, result in all_results.items():
        if name == "adv_0":
            continue
        stats_report[name] = compute_ablation_stats(base_result, result)

    report = {
        "evaluation_name": "DeepResearch Agent 对抗轮数消融实验（含统计显著性）",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "num_questions": len(questions),
        "systems": [
            {
                "system_name": r["system_name"],
                "description": r["description"],
                "average_composite_score": r["average_composite_score"],
                "details": r["details"],
            }
            for r in all_results.values()
        ],
        "summary": {r["system_name"]: r["average_composite_score"] for r in all_results.values()},
        "statistical_tests": stats_report,
    }

    filepath = AblationStudy.save_results(report, output_dir, prefix="rounds_ablation")

    # 同时保存 summary 扁平格式
    summary_path = os.path.join(output_dir, "adv_results_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(report["summary"], f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print("对抗轮数消融摘要 + 统计显著性")
    print(f"{'='*60}")
    for k, v in report["summary"].items():
        print(f"  {k:10s}: {v:.4f}")

    print(f"\n统计检验 (adv_0 vs adv_N, 配对 bootstrap 95% CI):")
    for name, st in stats_report.items():
        sig = "✓ 显著" if st["significant"] else "✗ 不显著"
        print(f"  {name:10s}: Δ={st['mean_diff']:+.4f} "
              f"CI=[{st['ci_lower']:+.4f}, {st['ci_upper']:+.4f}] "
              f"p={st['p_value']:.4f} d={st['cohens_d']:.3f} {sig}")
    print(f"\n结果已保存: {filepath}")
    print(f"Summary 已保存: {summary_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="DeepResearch Agent 消融实验脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/run_ablation.py --mode module --questions 10
  python scripts/run_ablation.py --mode rounds --questions 10 --max_rounds 3
        """,
    )
    parser.add_argument("--mode", type=str, choices=["module", "rounds"], default="module",
                        help="消融模式: module=模块消融, rounds=对抗轮数消融")
    parser.add_argument("--questions", type=int, default=10, help="评测题目数量（默认 10）")
    parser.add_argument("--domain", type=str, default=None, choices=["tech", "med", "fin"],
                        help="按领域过滤题目（仅 module 模式）")
    parser.add_argument("--max_rounds", type=int, default=3, help="最大对抗轮数（仅 rounds 模式）")
    parser.add_argument("--config", type=str, default=None, help="配置文件路径")
    parser.add_argument("--output_dir", type=str, default="outputs/evaluation", help="输出目录")
    parser.add_argument("--log_level", type=str, default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    setup_logging(args.log_level)
    logger = logging.getLogger("main")

    config = load_config(args.config)
    logger.info(f"配置加载完成: {args.config or 'configs/default.yaml'}")

    bench = ResearchBench()
    questions = bench.get_questions(domain=args.domain, n=args.questions)
    logger.info(f"加载 {len(questions)} 道评测题")

    if args.mode == "module":
        run_module_ablation(config, questions, args.output_dir)
    elif args.mode == "rounds":
        run_rounds_ablation(config, questions, args.max_rounds, args.output_dir)


if __name__ == "__main__":
    main()
