#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
evaluation/run_baseline.py
================================================================================
基线评测脚本 — 真实运行版本。

运行完整系统 vs 多个消融系统（关闭对抗/关闭进化/关闭压缩），
使用 MiMo 2.5 Pro 作为 Judge 后端进行报告评分，
最终输出结构化 JSON 评测报告。

消融配置说明：
  - full:           完整系统（所有模块开启）
  - no_adversarial: 关闭 M5 对抗降噪
  - no_evolution:   关闭 M6 进化学习
  - no_compressor:  关闭 M3 上下文压缩
  - no_memory:      关闭 M4 记忆存储
================================================================================
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evaluation.benchmarks.research_bench import ResearchBench

# 导入真实研究流程（已从 scripts/ 迁移到 src/core/）
from src.core.runner import initialize_modules, run_research


def load_config(config_path: str | None = None) -> dict:
    """加载 YAML 配置文件。"""
    if config_path is None:
        config_path = os.path.join(PROJECT_ROOT, "configs", "default.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def override_config(config: dict, system_name: str) -> dict:
    """根据消融实验名称，覆盖配置中的模块开关。"""
    cfg = copy.deepcopy(config)

    if system_name == "no_adversarial":
        cfg.setdefault("adversarial", {})["enabled"] = False
    elif system_name == "no_evolution":
        cfg.setdefault("evolution", {})["enabled"] = False
    elif system_name == "no_compressor":
        cfg.setdefault("compressor", {})["enable_multilevel"] = False
    elif system_name == "no_memory":
        cfg.setdefault("memory", {})["enabled"] = False
    # "full" 不做任何修改

    return cfg


class BaselineEvaluator:
    """基线评测器：运行多系统配置并生成对比报告。"""

    def __init__(self, output_dir: str = "outputs/evaluation") -> None:
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def run_system(
        self,
        system_name: str,
        config: dict,
        questions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """运行指定配置的系统并收集评测指标（真实运行）。"""
        print(f"[BaselineEvaluator] 正在运行系统: {system_name}")

        # 根据消融名称覆盖配置
        cfg = override_config(config, system_name)

        # 初始化模块
        modules = initialize_modules(cfg)

        scores = []
        details = []

        for q in questions:
            qid = q["id"]
            query = q["query"]
            print(f"  [{qid}] {query[:60]}...")

            start = time.time()
            try:
                report = asyncio.run(run_research(query, cfg, modules))
                elapsed = time.time() - start

                # 真实评分
                bench = ResearchBench()
                eval_result = bench.evaluate_report(report, qid)
                composite = eval_result.get("composite_score", 0.0)

                details.append({
                    "question_id": qid,
                    "composite_score": composite,
                    "metrics": eval_result.get("metrics", {}),
                    "elapsed_seconds": elapsed,
                    "system": system_name,
                })
                scores.append(composite)
                print(f"    → composite={composite:.3f}, time={elapsed:.1f}s")

            except Exception as e:
                print(f"    → FAILED: {e}")
                details.append({
                    "question_id": qid,
                    "composite_score": 0.0,
                    "error": str(e),
                    "system": system_name,
                })
                scores.append(0.0)

        avg_score = sum(scores) / len(scores) if scores else 0.0

        return {
            "system_name": system_name,
            "num_questions": len(questions),
            "average_composite_score": avg_score,
            "details": details,
            "timestamp": datetime.now().isoformat(),
        }

    def run_all_baselines(
        self,
        questions: list[dict[str, Any]],
        config: dict,
        systems: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """运行所有基线系统并生成对比报告。"""
        if systems is None:
            systems = {
                "full": "完整系统",
                "no_adversarial": "关闭对抗降噪",
                "no_evolution": "关闭进化学习",
                "no_compressor": "关闭上下文压缩",
                "no_memory": "关闭记忆存储",
            }

        results = []
        for name, desc in systems.items():
            print(f"\n{'='*60}")
            print(f"[消融实验] {name}: {desc}")
            print(f"{'='*60}")
            result = self.run_system(name, config, questions)
            result["description"] = desc
            results.append(result)

        report = {
            "evaluation_name": "DeepResearch Agent 消融基线评测",
            "timestamp": datetime.now().isoformat(),
            "num_questions": len(questions),
            "systems": results,
            "summary": {
                r["system_name"]: r["average_composite_score"] for r in results
            },
        }

        return report

    def save_report(self, report: dict[str, Any], filename: str | None = None) -> str:
        """保存评测报告为 JSON 文件。"""
        if filename is None:
            filename = f"baseline_eval_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

        path = os.path.join(self.output_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        print(f"[BaselineEvaluator] 报告已保存: {path}")
        return path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="DeepResearch Agent 基线评测脚本（真实运行）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python evaluation/run_baseline.py --questions 5 --output_dir outputs/evaluation
  python evaluation/run_baseline.py --domain tech --questions 3
        """,
    )
    parser.add_argument(
        "--questions",
        type=int,
        default=20,
        help="评测题目数量（默认 20）",
    )
    parser.add_argument(
        "--domain",
        type=str,
        default=None,
        choices=["tech", "med", "fin"],
        help="按领域过滤题目",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="配置文件路径（默认 configs/default.yaml）",
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default="outputs/evaluation",
        help="报告输出目录",
    )
    args = parser.parse_args()

    # 加载配置
    config = load_config(args.config)

    bench = ResearchBench()
    questions = bench.get_questions(domain=args.domain, n=args.questions)
    print(f"[main] 加载 {len(questions)} 道评测题")

    evaluator = BaselineEvaluator(output_dir=args.output_dir)
    report = evaluator.run_all_baselines(questions, config)
    evaluator.save_report(report)

    print("\n===== 评测摘要 =====")
    for name, score in report["summary"].items():
        print(f"  {name:20s}: {score:.4f}")


if __name__ == "__main__":
    main()
