#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/run_judge.py
================================================================================
MiMo Judge 深度评分入口脚本。

对单篇研究报告进行 5 维度专家评分，输出结构化 JSON。

Usage:
    python scripts/run_judge.py --report_file outputs/reports/report_xxx.md --query "原始问题"
    python scripts/run_judge.py --report_text "报告内容..." --query "原始问题"
================================================================================
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.judge import LLMJudge


def main() -> None:
    parser = argparse.ArgumentParser(description="MiMo Judge 深度评分")
    parser.add_argument("--report_file", type=str, default=None, help="报告文件路径（Markdown）")
    parser.add_argument("--report_text", type=str, default=None, help="报告文本内容")
    parser.add_argument("--query", type=str, required=True, help="原始研究问题")
    parser.add_argument("--ground_truth_file", type=str, default=None, help="ground_truth JSON 文件")
    parser.add_argument("--output", type=str, default=None, help="评分结果输出 JSON 路径")
    parser.add_argument("--backend", type=str, default="mimo", help="Judge 后端名称")
    args = parser.parse_args()

    # 读取报告
    if args.report_file:
        with open(args.report_file, "r", encoding="utf-8") as f:
            report_text = f.read()
    elif args.report_text:
        report_text = args.report_text
    else:
        print("错误: 必须指定 --report_file 或 --report_text")
        sys.exit(1)

    # 读取 ground_truth
    ground_truth = None
    if args.ground_truth_file:
        with open(args.ground_truth_file, "r", encoding="utf-8") as f:
            ground_truth = json.load(f)

    print(f"[Judge] 正在用 {args.backend} 对报告进行深度评分...")
    judge = LLMJudge(backend=args.backend)
    result = judge.score_single(report_text, args.query, ground_truth)

    if "error" in result:
        print(f"[Judge] 评分失败: {result['error']}")
        sys.exit(1)

    print("\n===== MiMo Judge 评分结果 =====")
    print(f"整体质量: {result['overall']['score']:.1f}/10 — {result['overall']['reason']}")
    print(f"平均分: {result['average']:.2f}")
    print("\n各维度:")
    for dim, data in result.get("dimensions", {}).items():
        print(f"  {dim:25s}: {data['score']:5.1f} — {data['reason']}")
    print("=" * 40)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"评分结果已保存: {args.output}")


if __name__ == "__main__":
    main()
