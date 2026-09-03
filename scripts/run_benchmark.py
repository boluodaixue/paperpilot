#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/run_benchmark.py
================================================================================
PaperPilot 定量评测脚本

评测设计：
    对比 "单轮 LLM 直接回答" vs "Agent 完整流程" 的研究质量。

指标：
    1. comprehensiveness (1-5): 报告覆盖多少子话题
    2. accuracy (1-5): 信息是否准确、有无幻觉
    3. source_count: 引用来源数量
    4. report_length: 报告字数
    5. workflow counters: evidence/thread/tool/token/retry/status

用法：
    python scripts/run_benchmark.py --queries_file data/benchmark_queries.txt
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# 单轮 LLM baseline（直接问 DeepSeek，不走 Agent）
# ---------------------------------------------------------------------------
def run_baseline(query: str, config: dict) -> dict:
    """Use the configured research backend directly, without the Agent workflow."""
    from src.models.model_router import ModelRouter

    model_config = config.get("model", {})
    backend = model_config.get("backend_mapping", {}).get(
        "research", model_config.get("backend")
    )
    policy = ModelRouter.create_backend(backend)
    messages = [
        {
            "role": "system",
            "content": (
                "You are a research assistant. Answer the user's question "
                "with a comprehensive, well-structured report in Markdown. "
                "Cite sources if possible."
            ),
        },
        {"role": "user", "content": query},
    ]
    resp = policy(messages)
    content = resp.get("content", "")
    return {
        "query": query,
        "content": content,
        "length": len(content),
        "source_count": content.count("http"),
    }


# ---------------------------------------------------------------------------
# Agent 完整流程
# ---------------------------------------------------------------------------
async def run_agent(query: str, config: dict) -> dict:
    """Run one explicitly auto-confirmed production workflow."""
    from scripts.run_eval import workflow_metrics
    from src.research.checkpoint_serde import paperpilot_in_memory_saver
    from src.research.runtime import build_research_runtime

    runtime = build_research_runtime(
        config=config,
        checkpointer=paperpilot_in_memory_saver(),
    )
    try:
        result = await runtime.run_auto_confirmed(
            query, thread_id=runtime.new_thread_id()
        )
        report_md = result.report_markdown
        return {
            "query": query,
            "content": report_md,
            "length": len(report_md),
            **workflow_metrics(result),
        }
    finally:
        await runtime.close()


# ---------------------------------------------------------------------------
# 配置驱动的自动评分（LLM-as-Judge）
# ---------------------------------------------------------------------------
def auto_score(report_a: str, report_b: str, query: str, config: dict) -> dict:
    """
    使用配置中的 Judge 后端对两份报告做对比评分。
    """
    from evaluation.judge import LLMJudge
    try:
        model_config = config.get("model", {})
        backend = model_config.get("backend_mapping", {}).get(
            "judge", model_config.get("backend")
        )
        judge = LLMJudge(backend=backend)
        return judge.compare_two(report_a, report_b, query)
    except Exception as e:
        print(f"[AutoScore] Judge 评分失败: {e}")
    return {}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
async def main() -> None:
    parser = argparse.ArgumentParser(description="PaperPilot Benchmark")
    parser.add_argument("--queries_file", type=str, default=None, help="每行一个查询问题的文件")
    parser.add_argument("--queries", type=str, nargs="+", default=None, help="直接在命令行传入问题")
    parser.add_argument("--output", type=str, default="outputs/benchmark_results.json", help="结果输出路径")
    parser.add_argument("--config", type=str, default=None, help="配置文件路径")
    parser.add_argument("--skip_baseline", action="store_true", help="跳过 baseline，只跑 Agent")
    parser.add_argument("--skip_agent", action="store_true", help="跳过 Agent，只跑 baseline")
    args = parser.parse_args()

    # 加载 queries
    if args.queries:
        queries = args.queries
    elif args.queries_file:
        with open(args.queries_file, "r", encoding="utf-8") as f:
            queries = [line.strip() for line in f if line.strip()]
    else:
        # 默认评测集
        queries = [
            "分析2026年中国互联网公司对于后训练岗位的需求性并建议我该怎么准备",
            "对比 GPT-4o、Claude 3.5 Sonnet、DeepSeek-V3 的推理能力差异",
            "2025年诺贝尔物理学奖得主的主要贡献是什么",
        ]

    print(f"[Benchmark] 评测问题数: {len(queries)}")

    # 加载配置
    from src.research.runtime import load_config
    config = load_config(args.config)

    results = []

    for i, query in enumerate(queries, 1):
        print(f"\n{'='*60}")
        print(f"[Benchmark] 问题 {i}/{len(queries)}: {query[:50]}...")
        print("=" * 60)

        record = {"query": query, "baseline": None, "agent": None, "scores": None}

        # Baseline
        if not args.skip_baseline:
            print("[Benchmark] Running baseline (single-turn LLM)...")
            t0 = time.time()
            baseline = run_baseline(query, config)
            baseline["elapsed"] = round(time.time() - t0, 2)
            record["baseline"] = baseline
            print(f"[Baseline] 字数={baseline['length']}, 来源数={baseline['source_count']}, 耗时={baseline['elapsed']}s")

        # Agent
        if not args.skip_agent:
            print("[Benchmark] Running Agent (full pipeline)...")
            t0 = time.time()
            agent_result = await run_agent(query, config)
            agent_result["elapsed"] = round(time.time() - t0, 2)
            record["agent"] = agent_result
            print(
                f"[Agent] 字数={agent_result['length']}, "
                f"证据={agent_result['evidence_count']}, 来源={agent_result['source_count']}, "
                f"线程={agent_result['thread_count']}, 工具调用={agent_result['tool_calls_used']}, "
                f"tokens={agent_result['estimated_tokens_used']}, "
                f"重试={agent_result['retries_used']}, 状态={agent_result['status']}, "
                f"耗时={agent_result['elapsed']}s"
            )

        # Auto score (if both available)
        if record["baseline"] and record["agent"]:
            print("[Benchmark] Auto-scoring...")
            scores = auto_score(
                record["baseline"]["content"],
                record["agent"]["content"],
                query,
                config,
            )
            record["scores"] = scores
            if scores:
                print(f"[Score] {json.dumps(scores, ensure_ascii=False, indent=2)}")
            else:
                print("[Score] 自动评分失败，请人工对比两份报告")

        results.append(record)

    # ------------------------------------------------------------------
    # 汇总 + 统计显著性
    # ------------------------------------------------------------------
    print(f"\n{'='*60}")
    print("[Benchmark] 评测完成，汇总：")
    print("=" * 60)

    for r in results:
        print(f"\nQ: {r['query'][:40]}...")
        if r["baseline"]:
            b = r["baseline"]
            print(f"  Baseline: {b['length']}字, {b['source_count']}来源, {b['elapsed']}s")
        if r["agent"]:
            a = r["agent"]
            print(
                f"  Agent:    {a['length']}字, {a['evidence_count']}证据, "
                f"{a['source_count']}来源, {a['thread_count']}线程, "
                f"{a['tool_calls_used']}工具调用, {a['estimated_tokens_used']}tokens, "
                f"{a['retries_used']}重试, status={a['status']}, {a['elapsed']}s"
            )

    # 统计显著性：收集每道题每个维度的配对分数
    if not args.skip_baseline and not args.skip_agent:
        from evaluation.metrics.stats import bootstrap_ci_paired

        dimensions = ["comprehensiveness", "accuracy", "structure", "sources"]
        dim_scores: dict[str, dict[str, list[float]]] = {d: {"agent": [], "baseline": []} for d in dimensions}

        for r in results:
            scores = r.get("scores", {})
            for dim in dimensions:
                dim_data = scores.get(dim, {})
                if isinstance(dim_data, dict) and "A" in dim_data and "B" in dim_data:
                    # A=baseline, B=agent (来自 LLMJudge.compare_two 的约定)
                    dim_scores[dim]["baseline"].append(float(dim_data["A"]))
                    dim_scores[dim]["agent"].append(float(dim_data["B"]))

        print(f"\n{'='*60}")
        print("[Benchmark] 统计显著性 (Agent vs Baseline, 配对 bootstrap 95% CI)")
        print("=" * 60)
        stats_summary: dict[str, Any] = {}
        for dim in dimensions:
            a_scores = dim_scores[dim]["agent"]
            b_scores = dim_scores[dim]["baseline"]
            if len(a_scores) < 2:
                continue
            diffs = [a - b for a, b in zip(a_scores, b_scores)]
            stats = bootstrap_ci_paired(diffs)
            stats_summary[dim] = stats
            sig = "✓ 显著" if stats["significant"] else "✗ 不显著"
            print(f"  {dim:20s}: Agent={sum(a_scores)/len(a_scores):.2f} Baseline={sum(b_scores)/len(b_scores):.2f} "
                  f"Δ={stats['mean_diff']:+.2f} CI=[{stats['ci_lower']:+.2f}, {stats['ci_upper']:+.2f}] "
                  f"p={stats['p_value']:.4f} {sig}")

        # 保存结果
        final_output = {
            "results": results,
            "statistical_tests": stats_summary,
            "num_questions": len(queries),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
    else:
        final_output = {
            "results": results,
            "num_questions": len(queries),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(final_output, f, ensure_ascii=False, indent=2)
    print(f"\n[Benchmark] 结果已保存: {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
