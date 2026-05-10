#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/run_all_experiments.py
================================================================================
DeepResearch Agent 批量实验脚本

一键运行全部核心实验，生成结构化汇总报告：
  1. 模块消融实验（full / no_adversarial / no_compressor / no_memory / no_evolution）
  2. 对抗轮数消融（0/1/2/3 轮）
  3. 标准评测集（ResearchBench 规则指标）
  4. 多领域对比（tech / med / fin 分领域评测）
  5. Agent vs 单轮 LLM（head-to-head benchmark）
  6. MiMo Judge 深度评分（单篇报告专家评审）
  7. 汇总报告生成（Markdown 格式）

Usage:
    python scripts/run_all_experiments.py \
        --report_file outputs/reports1/report_xxx.md \
        --report_query "你的研究问题"

每个实验独立子进程运行，互不影响。失败实验会被记录但不会中断整体流程。
================================================================================
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# 实验配置与执行器
# ---------------------------------------------------------------------------
class ExperimentRunner:
    """批量实验执行器。"""

    def __init__(
        self,
        config_path: str | None,
        output_dir: str,
        ablation_q: int,
        eval_q: int,
        domain_q: int,
        benchmark_queries: list[str],
        report_file: str | None,
        report_query: str | None,
    ) -> None:
        self.config_path = config_path
        self.output_dir = output_dir
        # 0 表示使用全部可用题目
        self.ablation_q = ablation_q if ablation_q > 0 else None
        self.eval_q = eval_q if eval_q > 0 else None
        self.domain_q = domain_q if domain_q > 0 else None
        self.benchmark_queries = benchmark_queries
        self.report_file = report_file
        self.report_query = report_query
        self.results: list[dict[str, Any]] = []
        self.start_time = time.time()

        os.makedirs(output_dir, exist_ok=True)

    def _run_subprocess(self, name: str, cmd: list[str], cwd: str = str(PROJECT_ROOT)) -> dict[str, Any]:
        """运行子进程实验，返回结果摘要。"""
        print(f"\n{'='*70}")
        print(f"[批量实验] 启动: {name}")
        print(f"{'='*70}")
        print(f"命令: {' '.join(cmd)}")

        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=False,
                text=True,
                timeout=None,  # 不限制单实验超时，由用户 Ctrl+C 控制
            )
            elapsed = time.time() - t0
            status = "success" if proc.returncode == 0 else "failed"
            print(f"[批量实验] {name} 完成 | 状态={status} | 耗时={elapsed:.1f}s")
            return {
                "name": name,
                "status": status,
                "elapsed_seconds": elapsed,
                "returncode": proc.returncode,
            }
        except subprocess.TimeoutExpired:
            elapsed = time.time() - t0
            print(f"[批量实验] {name} 超时 (>2h)")
            return {
                "name": name,
                "status": "timeout",
                "elapsed_seconds": elapsed,
                "returncode": -1,
            }
        except Exception as e:
            elapsed = time.time() - t0
            print(f"[批量实验] {name} 异常: {e}")
            return {
                "name": name,
                "status": "error",
                "elapsed_seconds": elapsed,
                "error": str(e),
            }

    def _build_config_args(self) -> list[str]:
        return ["--config", self.config_path] if self.config_path else []

    # ------------------------------------------------------------------
    # 实验 1: 模块消融
    # ------------------------------------------------------------------
    def run_ablation_module(self) -> dict[str, Any]:
        out = os.path.join(self.output_dir, "ablation_module")
        cmd = [
            sys.executable, "scripts/run_ablation.py",
            "--mode", "module",
            "--output_dir", out,
        ] + self._build_config_args()
        if self.ablation_q is not None:
            cmd.extend(["--questions", str(self.ablation_q)])
        return self._run_subprocess("模块消融实验", cmd)

    # ------------------------------------------------------------------
    # 实验 2: 对抗轮数消融
    # ------------------------------------------------------------------
    def run_ablation_rounds(self) -> dict[str, Any]:
        out = os.path.join(self.output_dir, "ablation_rounds")
        cmd = [
            sys.executable, "scripts/run_ablation.py",
            "--mode", "rounds",
            "--max_rounds", "3",
            "--output_dir", out,
        ] + self._build_config_args()
        if self.ablation_q is not None:
            cmd.extend(["--questions", str(self.ablation_q)])
        return self._run_subprocess("对抗轮数消融", cmd)

    # ------------------------------------------------------------------
    # 实验 3: 标准评测集
    # ------------------------------------------------------------------
    def run_eval_research_bench(self) -> dict[str, Any]:
        out = os.path.join(self.output_dir, "eval_research_bench")
        cmd = [
            sys.executable, "scripts/run_eval.py",
            "--benchmark", "research_bench",
            "--output_dir", out,
        ] + self._build_config_args()
        if self.eval_q is not None:
            cmd.extend(["--num_questions", str(self.eval_q)])
        return self._run_subprocess("标准评测集 (ResearchBench)", cmd)

    # ------------------------------------------------------------------
    # 实验 4: 多领域对比
    # ------------------------------------------------------------------
    def run_domain_comparison(self) -> dict[str, Any]:
        domains = ["tech", "med", "fin"]
        sub_results = []
        for domain in domains:
            out = os.path.join(self.output_dir, "domain_comparison", domain)
            cmd = [
                sys.executable, "scripts/run_eval.py",
                "--benchmark", "research_bench",
                "--domain", domain,
                "--output_dir", out,
            ] + self._build_config_args()
            if self.domain_q is not None:
                cmd.extend(["--num_questions", str(self.domain_q)])
            r = self._run_subprocess(f"领域对比 ({domain})", cmd)
            sub_results.append(r)

        # 汇总为一个结果
        return {
            "name": "多领域对比",
            "status": "success" if all(s["status"] == "success" for s in sub_results) else "partial",
            "sub_results": sub_results,
        }

    # ------------------------------------------------------------------
    # 实验 5: Agent vs 单轮 LLM
    # ------------------------------------------------------------------
    def run_benchmark(self) -> dict[str, Any]:
        out = os.path.join(self.output_dir, "benchmark")
        os.makedirs(out, exist_ok=True)
        cmd = [
            sys.executable, "scripts/run_benchmark.py",
            "--output", os.path.join(out, "results.json"),
            "--queries",
        ] + self.benchmark_queries
        return self._run_subprocess("Agent vs 单轮 LLM", cmd)

    # ------------------------------------------------------------------
    # 实验 5b: HotpotQA 深度研究评测（可选）
    # ------------------------------------------------------------------
    def run_hotpotqa(self) -> dict[str, Any]:
        out = os.path.join(self.output_dir, "eval_hotpotqa")
        cmd = [
            sys.executable, "scripts/run_eval.py",
            "--benchmark", "hotpotqa",
            "--use_mock",
            "--output_dir", out,
        ] + self._build_config_args()
        if self.eval_q is not None:
            cmd.extend(["--num_questions", str(self.eval_q)])
        return self._run_subprocess("HotpotQA 深度研究评测 (mock)", cmd)

    # ------------------------------------------------------------------
    # 实验 6: Judge 深度评分
    # ------------------------------------------------------------------
    def run_judge(self) -> dict[str, Any]:
        if not self.report_file or not self.report_query:
            return {
                "name": "MiMo Judge 深度评分",
                "status": "skipped",
                "reason": "未指定 --report_file 或 --report_query",
            }
        out = os.path.join(self.output_dir, "judge")
        os.makedirs(out, exist_ok=True)
        cmd = [
            sys.executable, "scripts/run_judge.py",
            "--report_file", self.report_file,
            "--query", self.report_query,
            "--output", os.path.join(out, "score.json"),
        ]
        return self._run_subprocess("MiMo Judge 深度评分", cmd)

    # ------------------------------------------------------------------
    # 汇总报告生成
    # ------------------------------------------------------------------
    def generate_summary(self) -> str:
        """生成 Markdown 汇总报告。"""
        total_elapsed = time.time() - self.start_time
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        lines = [
            "# DeepResearch Agent 批量实验汇总报告",
            "",
            f"- **实验时间**: {timestamp}",
            f"- **总耗时**: {total_elapsed/60:.1f} 分钟",
            f"- **实验项目数**: {len(self.results)}",
            f"- **成功**: {sum(1 for r in self.results if r.get('status') == 'success')}",
            f"- **失败/跳过**: {sum(1 for r in self.results if r.get('status') != 'success')}",
            "",
            "## 实验结果一览",
            "",
            "| 实验名称 | 状态 | 耗时(秒) | 备注 |",
            "|---------|------|---------|------|",
        ]

        for r in self.results:
            name = r.get("name", "未知")
            status = r.get("status", "未知")
            elapsed = r.get("elapsed_seconds", 0.0)
            note = ""
            if status == "success":
                note = "✓ 完成"
            elif status == "skipped":
                note = f"跳过: {r.get('reason', '')}"
            elif status == "partial":
                note = "部分完成"
            else:
                note = f"✗ {r.get('error', '')[:40]}"
            lines.append(f"| {name} | {status} | {elapsed:.1f} | {note} |")

        lines.extend([
            "",
            "## 产出文件",
            "",
        ])

        # 列出各实验的产出文件
        for subdir, desc in [
            ("ablation_module", "模块消融结果"),
            ("ablation_rounds", "对抗轮数消融结果"),
            ("eval_research_bench", "标准评测集结果"),
            ("domain_comparison", "多领域对比结果"),
            ("benchmark", "Agent vs LLM 对比结果"),
            ("judge", "MiMo Judge 深度评分结果"),
        ]:
            path = os.path.join(self.output_dir, subdir)
            if os.path.exists(path):
                files = [f for f in os.listdir(path) if f.endswith(".json")]
                if files:
                    lines.append(f"- **{desc}**: `{path}`")
                    for f in sorted(files):
                        lines.append(f"  - `{f}`")

        lines.extend([
            "",
            "## 面试可用结论",
            "",
            "### 消融实验",
            "- 检查 `ablation_module/` 下的 JSON，看各模块的 `mean_diff` 和 `significant`",
            "- 若 `no_adversarial` 的 CI 不包含 0 且 p<0.05，说明对抗模块有独立贡献",
            "",
            "### 标准评测集",
            "- `eval_research_bench/` 下查看 `average_composite` 和按领域统计",
            "- 35 题 × 5 维度规则指标 = 可复现的客观分数",
            "",
            "### Agent vs LLM",
            "- `benchmark/results.json` 中查看 `statistical_tests`",
            "- 若 4 个维度的 CI 都在 0 右侧，说明 Agent 显著优于单轮 LLM",
            "",
            "### Judge 深度评分",
            "- `judge/score.json` 中查看 5 维度分数 + 理由",
            "- 可用于抽查验证和最终质量把关",
            "",
        ])

        md = "\n".join(lines)
        md_path = os.path.join(self.output_dir, "SUMMARY.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md)

        # 同时保存 JSON 汇总
        json_path = os.path.join(self.output_dir, "summary.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({
                "timestamp": timestamp,
                "total_elapsed_seconds": total_elapsed,
                "results": self.results,
            }, f, ensure_ascii=False, indent=2)

        return md_path

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def run_all(self) -> None:
        """按顺序运行全部实验（不跳过任何一项）。"""
        print("=" * 70)
        print("DeepResearch Agent 批量实验启动 —— 全量模式")
        print("=" * 70)
        print(f"输出目录: {self.output_dir}")
        abl_str = str(self.ablation_q) if self.ablation_q else "全部可用"
        eval_str = str(self.eval_q) if self.eval_q else "全部可用"
        domain_str = str(self.domain_q) if self.domain_q else "全部可用"
        print(f"消融题目数: {abl_str}")
        print(f"评测题目数: {eval_str}")
        print(f"领域对比题目数: {domain_str}")
        print(f"Benchmark 问题数: {len(self.benchmark_queries)}")
        print(f"HotpotQA: mock 模式全部题目")
        print()

        self.results.append(self.run_ablation_module())
        self.results.append(self.run_ablation_rounds())
        self.results.append(self.run_eval_research_bench())
        self.results.append(self.run_domain_comparison())
        self.results.append(self.run_benchmark())
        self.results.append(self.run_hotpotqa())
        self.results.append(self.run_judge())

        # 生成汇总
        md_path = self.generate_summary()
        print(f"\n{'='*70}")
        print("批量实验全部完成！")
        print(f"汇总报告: {md_path}")
        print(f"总耗时: {(time.time() - self.start_time)/60:.1f} 分钟")
        print("=" * 70)


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="DeepResearch Agent 批量实验脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
面试推荐默认配置（约 12 小时，拉满样本量，不跳过任何实验）：
  模块消融 5 配置 × 12 题 = 60 次
  轮数消融 4 配置 × 12 题 = 48 次
  标准评测 35 题（全量）   = 35 次
  领域对比 3 领域 × 5 题   = 15 次
  Agent vs LLM 3 题 × 2    =  6 次
  Judge 深度评分           =  1 次
  ───────────────────────────────
  合计约 165 次研究运行

快速验证（约 2 小时）：
  python scripts/run_all_experiments.py \
      --ablation_questions 3 --eval_questions 5 --domain_questions 2
        """,
    )
    parser.add_argument("--config", type=str, default=None, help="配置文件路径")
    parser.add_argument("--output_dir", type=str, default="outputs/experiments", help="实验输出根目录")
    parser.add_argument(
        "--ablation_questions", type=int, default=12,
        help="消融实验题目数（默认 12，0=全部可用）"
    )
    parser.add_argument(
        "--eval_questions", type=int, default=35,
        help="标准评测题目数（默认 35，0=全部可用）"
    )
    parser.add_argument(
        "--domain_questions", type=int, default=5,
        help="领域对比每个领域题目数（默认 5，0=全部可用）"
    )
    parser.add_argument("--report_file", type=str, default=None, help="Judge 评分的报告文件路径")
    parser.add_argument("--report_query", type=str, default=None, help="Judge 评分对应的原始问题")
    args = parser.parse_args()

    # 如果没有指定 benchmark 问题，从 ResearchBench 默认抽取 3 道深度题
    benchmark_queries = [
        "分析2026年中国互联网公司对于后训练岗位的需求性并建议我该怎么准备",
        "对比 GPT-4o、Claude 3.5 Sonnet、DeepSeek-V3 的推理能力差异",
        "2025年诺贝尔物理学奖得主的主要贡献是什么",
    ]

    # 如果指定了 report_file 但没有 report_query，尝试从报告文件名推断
    report_query = args.report_query
    if args.report_file and not report_query:
        # 从文件名提取 query 前缀（report_时间戳_query前20字.md）
        fname = Path(args.report_file).stem
        parts = fname.split("_")
        if len(parts) >= 4:
            # report_YYYYMMDD_HHMMSS_query前缀
            report_query = "_".join(parts[3:])
        else:
            report_query = fname
        print(f"[提示] 未指定 --report_query，从文件名推断为: {report_query}")

    runner = ExperimentRunner(
        config_path=args.config,
        output_dir=args.output_dir,
        ablation_q=args.ablation_questions,
        eval_q=args.eval_questions,
        domain_q=args.domain_questions,
        benchmark_queries=benchmark_queries,
        report_file=args.report_file,
        report_query=report_query,
    )

    runner.run_all()


if __name__ == "__main__":
    main()
