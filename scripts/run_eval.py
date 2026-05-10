#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/run_eval.py
================================================================================
标准评测集入口脚本（合并了原 run_evaluation.py）。

支持:
  --benchmark research_bench : 自建深度研究评测集（规则指标）
  --benchmark hotpotqa      : 公共多跳 QA 评测集（EM/F1）

Usage:
    python scripts/run_eval.py --benchmark research_bench --num_questions 20
    python scripts/run_eval.py --benchmark hotpotqa --num_questions 100
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
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.runner import initialize_modules, load_config, run_research, setup_logging
from evaluation.benchmarks.research_bench import ResearchBench
from evaluation.benchmarks.hotpotqa import HotpotQABenchmark
from evaluation.report import EvaluationReport


def evaluate_research_bench(
    num_questions: int,
    domain: str | None,
    config: dict,
) -> EvaluationReport:
    """在 ResearchBench 上运行评测。"""
    logger = logging.getLogger("run_eval")
    bench = ResearchBench()
    questions = bench.get_questions(domain=domain, n=num_questions)
    logger.info(f"ResearchBench 加载 {len(questions)} 道题目")

    modules = initialize_modules(config)
    report = EvaluationReport(name="ResearchBench_Evaluation", num_questions=len(questions))

    for idx, q in enumerate(questions, 1):
        qid = q["id"]
        query = q["query"]
        logger.info(f"[{idx}/{len(questions)}] 评测题目: {qid}")

        start = time.time()
        try:
            report_text = asyncio.run(run_research(query, config, modules))
            elapsed = time.time() - start

            eval_result = bench.evaluate_report(report_text, qid)
            eval_result["elapsed_seconds"] = elapsed
            report.add_detail(eval_result)
            logger.info(f"  → composite={eval_result['composite_score']:.3f}, time={elapsed:.1f}s")
        except Exception as e:
            logger.warning(f"  → FAILED: {e}")
            report.add_detail({
                "question_id": qid,
                "error": str(e),
                "composite_score": 0.0,
            })

    # 汇总
    valid_scores = [d["composite_score"] for d in report.details if "composite_score" in d]
    report.set_summary({
        "average_composite": sum(valid_scores) / len(valid_scores) if valid_scores else 0.0,
        "num_success": len([d for d in report.details if "error" not in d]),
        "num_failed": len([d for d in report.details if "error" in d]),
    })

    return report


def evaluate_hotpotqa(
    num_questions: int,
    config: dict,
    use_mock: bool = False,
) -> EvaluationReport:
    """在 HotpotQA 上运行评测（深度研究变体：评估完整报告质量）。"""
    logger = logging.getLogger("run_eval")
    bench = HotpotQABenchmark(use_mock=use_mock)
    questions = bench.get_samples(n=num_questions, shuffle=True)
    logger.info(f"HotpotQA 加载 {len(questions)} 道题目")

    modules = initialize_modules(config)
    report = EvaluationReport(name="HotpotQA_DeepResearch_Evaluation", num_questions=len(questions))

    predictions = []
    for idx, q in enumerate(questions, 1):
        query = q["query"]
        gold = q["expected_answer"]
        logger.info(f"[{idx}/{len(questions)}] 评测: {query[:60]}...")

        try:
            report_text = asyncio.run(run_research(query, config, modules))
            pred_answer = report_text.strip().split("\n")[0] if report_text.strip() else ""
        except Exception as e:
            logger.warning(f"  → FAILED: {e}")
            pred_answer = ""
            report_text = ""

        predictions.append({
            "query_id": idx,
            "prediction": pred_answer,
            "gold": gold,
            "report": report_text,
        })

        depth = bench.evaluate_report(report_text, gold) if report_text else {}
        report.add_detail({
            "query_id": idx,
            "query": query,
            "prediction": pred_answer,
            "gold": gold,
            "depth_metrics": depth,
        })

    metrics = bench.evaluate(predictions, metrics=["em", "f1", "pass@1"])
    report.set_summary(metrics)

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="DeepResearch Agent 标准评测脚本")
    parser.add_argument("--benchmark", type=str, choices=["research_bench", "hotpotqa"],
                        required=True, help="评测基准")
    parser.add_argument("--num_questions", type=int, default=20, help="评测题目数量")
    parser.add_argument("--domain", type=str, default=None, help="领域过滤（仅 ResearchBench）")
    parser.add_argument("--use_mock", action="store_true", help="使用内置 mock 数据（仅 HotpotQA，用于流程验证）")
    parser.add_argument("--config", type=str, default=None, help="配置文件路径")
    parser.add_argument("--output_dir", type=str, default="outputs/evaluation", help="输出目录")
    parser.add_argument("--log_level", type=str, default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    setup_logging(args.log_level)
    logger = logging.getLogger("main")

    config = load_config(args.config)
    logger.info(f"配置加载完成: {args.config or 'configs/default.yaml'}")

    if args.benchmark == "research_bench":
        report = evaluate_research_bench(args.num_questions, args.domain, config)
    elif args.benchmark == "hotpotqa":
        report = evaluate_hotpotqa(args.num_questions, config, use_mock=args.use_mock)
    else:
        raise ValueError(f"未知基准: {args.benchmark}")

    filepath = report.save(args.output_dir)
    logger.info(f"评测报告已保存: {filepath}")

    print("\n" + "=" * 60)
    print("评测摘要")
    print("=" * 60)
    print(json.dumps(report.summary, ensure_ascii=False, indent=2))
    print("=" * 60)


if __name__ == "__main__":
    main()
