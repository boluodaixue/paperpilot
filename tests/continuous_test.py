#!/usr/bin/env python3
"""
持续集成测试脚本 — 自动化验证 DeepResearch Agent 端到端能力

测试目标：
  1. 所有工具（web_search, arxiv_reader, calculator, file_reader 等）被真实调用
  2. 子任务失败时不输出虚假/空报告
  3. 报告基于搜索结果而非纯 LLM 编造
  4. 各模块（Planner/Compressor/Memory/Adversarial）按预期工作
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# 强制无缓冲输出，确保日志实时写入
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# 测试用例设计：每个 query 侧重测试不同的工具组合和能力
# ---------------------------------------------------------------------------
TEST_QUERIES: list[dict] = [
    {
        "id": "T1",
        "name": "综合搜索+论文检索",
        "query": (
            "2024年至2025年大模型Agent技术方向与后端系统开发方向的对比研究："
            "检索两个领域的最新学术论文发表数量与趋势变化、"
            "工业界代表性落地案例与市场规模数据"
        ),
        "expected_tools": ["web_search", "arxiv_reader"],
        "min_success_rate": 0.5,
        "min_report_length": 1500,
    },
    {
        "id": "T2",
        "name": "搜索+计算工具",
        "query": (
            "计算Transformer模型在7B、13B、70B三种参数量下的训练FLOPs和推理显存占用，"
            "并检索2024年至2025年主流大模型（如GPT-4、Claude 3、Gemini、DeepSeek）的实际部署成本和推理延迟数据"
        ),
        "expected_tools": ["web_search", "calculator"],
        "min_success_rate": 0.4,
        "min_report_length": 1500,
    },
    {
        "id": "T3",
        "name": "深度搜索+对抗优化",
        "query": (
            "中国新能源汽车行业2024年至2025年的市场份额变化、主要品牌销量排名、"
            "电池技术路线（磷酸铁锂vs三元锂）的技术对比与成本分析"
        ),
        "expected_tools": ["web_search"],
        "min_success_rate": 0.5,
        "min_report_length": 2000,
    },
    {
        "id": "T4",
        "name": "论文检索专项",
        "query": (
            "检索近一年（2024年至2025年）关于RLHF（基于人类反馈的强化学习）的顶级会议论文，"
            "统计NeurIPS、ICML、ICLR各会议收录数量，并分析该领域的技术演进趋势和核心作者"
        ),
        "expected_tools": ["arxiv_reader", "web_search"],
        "min_success_rate": 0.4,
        "min_report_length": 1500,
    },
    {
        "id": "T5",
        "name": "跨领域趋势分析",
        "query": (
            "对比分析2024年至2025年生成式AI在医疗健康领域和金融投资领域的应用进展："
            "检索各领域的代表性产品、监管政策变化、以及商业化落地案例"
        ),
        "expected_tools": ["web_search", "arxiv_reader"],
        "min_success_rate": 0.5,
        "min_report_length": 2000,
    },
]


def clean_env():
    """清理数据库和旧日志。"""
    db_path = PROJECT_ROOT / "data" / "memory.db"
    if db_path.exists():
        db_path.unlink()
    for f in (PROJECT_ROOT / "outputs" / "reports").glob("report_*.md"):
        f.unlink()
    print("[Test] 环境已清理")


def run_single_test(test_case: dict) -> dict:
    """执行单次测试并返回分析结果。"""
    test_id = test_case["id"]
    query = test_case["query"]
    log_file = PROJECT_ROOT / "outputs" / f"test_{test_id}.log"

    print(f"\n{'='*60}")
    print(f"[Test {test_id}] {test_case['name']}")
    print(f"Query: {query[:60]}...")
    print(f"{'='*60}")

    clean_env()

    start = time.time()
    proc = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "run_research.py"),
            "--query", query,
            "--output_dir", str(PROJECT_ROOT / "outputs" / "reports" / test_id),
        ],
        capture_output=True,
        text=True,
        timeout=900,  # 15 分钟超时
    )
    elapsed = time.time() - start

    # 保存日志
    log_file.write_text(proc.stdout + "\n" + proc.stderr, encoding="utf-8")

    # 提取关键指标
    result = analyze_log(proc.stdout, test_case)
    result["elapsed"] = elapsed
    result["returncode"] = proc.returncode

    # 检查报告文件
    report_dir = PROJECT_ROOT / "outputs" / "reports" / test_id
    reports = list(report_dir.glob("*.md")) if report_dir.exists() else []
    if reports:
        report_text = reports[0].read_text(encoding="utf-8")
        result["report_path"] = str(reports[0])
        result["report_length"] = len(report_text)
        result["report_has_content"] = len(report_text) > test_case["min_report_length"]
        result["is_empty_md"] = "Research failed" in report_text or result["report_length"] < 500
    else:
        result["report_path"] = None
        result["report_length"] = 0
        result["report_has_content"] = False
        result["is_empty_md"] = True

    return result


def analyze_log(stdout: str, test_case: dict) -> dict:
    """从 stdout 提取关键指标。"""
    metrics = {
        "subtask_total": 0,
        "subtask_success": 0,
        "subtask_failed": 0,
        "success_rate": 0.0,
        "num_searches": 0,
        "num_replan": 0,
        "adversarial_rounds": 0,
        "confidence": 0.0,
        "has_bogus_output": False,
        "issues": [],
    }

    # 子任务成功率
    m = re.search(r"子任务完成:\s*(\d+)/(\d+)\s*成功\s*\((\d+)\s*失败\)", stdout)
    if m:
        metrics["subtask_success"] = int(m.group(1))
        metrics["subtask_total"] = int(m.group(2))
        metrics["subtask_failed"] = int(m.group(3))
        if metrics["subtask_total"] > 0:
            metrics["success_rate"] = metrics["subtask_success"] / metrics["subtask_total"]

    # 元信息
    m = re.search(r"置信度=(\d+\.?\d*)", stdout)
    if m:
        metrics["confidence"] = float(m.group(1))

    m = re.search(r"搜索轮数=(\d+)", stdout)
    if m:
        metrics["num_searches"] = int(m.group(1))

    m = re.search(r"重规划=(\d+)", stdout)
    if m:
        metrics["num_replan"] = int(m.group(1))

    m = re.search(r"对抗轮数=(\d+)", stdout)
    if m:
        metrics["adversarial_rounds"] = int(m.group(1))

    # Bug 检测
    if metrics["success_rate"] == 0.0 and metrics["num_searches"] == 0:
        metrics["has_bogus_output"] = True
        metrics["issues"].append("所有子任务失败且搜索轮数为0，但可能输出了报告")

    if "Research failed" in stdout and metrics["report_length"] == 0:
        metrics["issues"].append("报告标记失败且为空——这是正确的行为")

    # 检查是否使用了预期工具（通过搜索轮数间接判断）
    if metrics["num_searches"] == 0 and "web_search" in test_case["expected_tools"]:
        metrics["issues"].append("预期使用 web_search，但搜索轮数为0")

    return metrics


def print_summary(results: list[dict]):
    """打印测试摘要。"""
    print("\n" + "=" * 70)
    print("测试摘要")
    print("=" * 70)

    total_pass = 0
    for r in results:
        tc = r["test_case"]
        passed = (
            r["success_rate"] >= tc["min_success_rate"]
            and r.get("report_has_content", False)
            and not r.get("is_empty_md", True)
            and not r["has_bogus_output"]
        )
        total_pass += int(passed)

        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"\n[{tc['id']}] {tc['name']} — {status}")
        print(f"  子任务: {r['subtask_success']}/{r['subtask_total']} 成功 ({r['success_rate']:.0%})")
        print(f"  搜索轮数: {r['num_searches']} | 重规划: {r['num_replan']} | 对抗: {r['adversarial_rounds']}")
        print(f"  置信度: {r['confidence']:.2f} | 报告长度: {r.get('report_length', 0)} 字")
        print(f"  耗时: {r['elapsed']:.1f}s")
        if r["issues"]:
            print(f"  问题: {'; '.join(r['issues'])}")

    print(f"\n总计: {total_pass}/{len(results)} 通过")


def main():
    results = []
    for tc in TEST_QUERIES:
        try:
            r = run_single_test(tc)
            r["test_case"] = tc
            results.append(r)
        except subprocess.TimeoutExpired:
            print(f"[Test {tc['id']}] 超时（15分钟）")
            results.append({
                "test_case": tc,
                "success_rate": 0,
                "has_bogus_output": True,
                "issues": ["超时"],
                "elapsed": 900,
            })
        except Exception as e:
            print(f"[Test {tc['id']}] 异常: {e}")
            results.append({
                "test_case": tc,
                "success_rate": 0,
                "has_bogus_output": True,
                "issues": [f"异常: {e}"],
                "elapsed": 0,
            })

    print_summary(results)

    # 保存详细结果
    summary_path = PROJECT_ROOT / "outputs" / "test_summary.json"
    summary_path.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n详细结果已保存: {summary_path}")


if __name__ == "__main__":
    main()
