"""Create the final auditable summary for the ten completed multi-Agent holdouts."""
from __future__ import annotations

import json
from pathlib import Path


BATCH = Path(__file__).resolve().parent
DRB = BATCH.parent
FORMAL = DRB / "formal-v1"
ORDER = (16, 11, 49, 42, 69, 72, 57, 54, 87, 86)
CALIBRATION = (25, 12)
DIMS = ("info_recall", "analysis", "presentation")
WEIGHTS = {"info_recall": 0.75, "analysis": 0.17, "presentation": 0.08}


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def usage(folder: Path) -> dict:
    calls = read(folder / "model_calls.json")
    known = [row.get("usage") for row in calls if isinstance(row.get("usage"), dict)]
    judge_rows = [read(path) for path in (folder / "judge_batches").glob("*-attempt-*.json")]
    judge_known = [row.get("usage") for row in judge_rows if isinstance(row.get("usage"), dict)]
    return {
        "research_tokens": sum((row.get("total_tokens") or 0) for row in known),
        "research_prompt_tokens": sum((row.get("prompt_tokens") or 0) for row in known),
        "research_completion_tokens": sum((row.get("completion_tokens") or 0) for row in known),
        "research_calls": len(calls),
        "research_calls_usage_unknown": len(calls) - len(known),
        "judge_tokens": sum((row.get("total_tokens") or 0) for row in judge_known),
        "judge_prompt_tokens": sum((row.get("prompt_tokens") or 0) for row in judge_known),
        "judge_completion_tokens": sum((row.get("completion_tokens") or 0) for row in judge_known),
        "judge_attempts": len(judge_rows),
        "judge_calls_usage_unknown": len(judge_rows) - len(judge_known),
    }


def scored_row(idx: int, folder: Path) -> dict:
    run = read(folder / "run.json")
    judge = read(folder / "judge.json")
    scores = judge["scores"]
    counts = {dim: len(scores[dim]) for dim in DIMS}
    passes = {dim: sum(item["score"] == 1 for item in scores[dim]) for dim in DIMS}
    blocked = sum(item["score"] == -1 for dim in DIMS for item in scores[dim])
    total_count = sum(counts.values())
    total_pass = sum(passes.values())
    return {
        "task": idx,
        "folder": str(folder),
        "run": run,
        "counts": counts,
        "passes": passes,
        "blocked": blocked,
        "total_count": total_count,
        "total_pass": total_pass,
        "total_pass_rate": total_pass / total_count,
        "dimension_rates": {dim: passes[dim] / counts[dim] for dim in DIMS},
        **usage(folder),
    }


def aggregate(rows: list[dict]) -> dict:
    macro_total = sum(row["total_pass_rate"] for row in rows) / len(rows)
    micro_pass = sum(row["total_pass"] for row in rows)
    micro_count = sum(row["total_count"] for row in rows)
    dimension_macro = {
        dim: sum(row["dimension_rates"][dim] for row in rows) / len(rows)
        for dim in DIMS
    }
    official_weighted_style = sum(dimension_macro[dim] * WEIGHTS[dim] for dim in DIMS)
    return {
        "tasks": len(rows),
        "delivered": sum(row["run"]["status"] == "returned" and not row["run"].get("delivery_issues") for row in rows),
        "scored": len(rows),
        "macro_task_pass_rate": macro_total,
        "micro_pass": micro_pass,
        "micro_count": micro_count,
        "micro_pass_rate": micro_pass / micro_count,
        "dimension_macro": dimension_macro,
        "official_weighted_style": official_weighted_style,
        "blocked": sum(row["blocked"] for row in rows),
        "research_tokens_known": sum(row["research_tokens"] for row in rows),
        "judge_tokens_known": sum(row["judge_tokens"] for row in rows),
        "total_tokens_known": sum(row["research_tokens"] + row["judge_tokens"] for row in rows),
        "research_calls_usage_unknown": sum(row["research_calls_usage_unknown"] for row in rows),
        "judge_calls_usage_unknown": sum(row["judge_calls_usage_unknown"] for row in rows),
        "research_elapsed_seconds_sum": sum(row["run"].get("elapsed_seconds", 0) for row in rows),
    }


def percent(value: float) -> str:
    return f"{value:.2%}"


def main() -> None:
    holdout = [scored_row(idx, BATCH / "runs" / f"{idx}-multi") for idx in ORDER]
    calibration = [scored_row(idx, FORMAL / "runs" / f"{idx}-multi") for idx in CALIBRATION]
    all_rows = [*calibration, *holdout]
    result = {
        "benchmark": {
            "name": "DeepResearch Bench II",
            "repository": "https://github.com/SawyerCooper/DeepResearchBench2",
            "commit": "440bdc33728438d317ca7860809942b5a1e40256",
            "full_task_count": 132,
        },
        "paperpilot_source_commit": "9e81efed413b86152cfcff372e379b350bfa9e38",
        "judge": "project DeepSeek judge; not the official Gemini judge",
        "holdout_rows": holdout,
        "holdout_summary": aggregate(holdout),
        "fixed_12_rows": all_rows,
        "fixed_12_summary": aggregate(all_rows),
    }
    (BATCH / "最终汇总.json").write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# DeepResearch Bench II 固定 12 题子集：PaperPilot multi-Agent 最终结果",
        "",
        "公开 Benchmark：DeepResearch Bench II；上游提交 `440bdc33728438d317ca7860809942b5a1e40256`。",
        "PaperPilot 研究源码提交：`9e81efed413b86152cfcff372e379b350bfa9e38`。",
        "本实验使用项目 DeepSeek Judge，不是官方 Gemini Judge；以下不能冒充官方榜单成绩。",
        "",
        "## 固定 12 题逐题结果",
        "",
        "| 题号 | 用途 | 得分 | 信息召回 | 分析 | 呈现 | Agent | 工具 | Evidence | 已知研究 Token | Judge Token |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in all_rows:
        run = row["run"]
        purpose = "校准" if row["task"] in CALIBRATION else "保留"
        lines.append(
            f"| {row['task']} | {purpose} | {row['total_pass']}/{row['total_count']} ({percent(row['total_pass_rate'])}) "
            f"| {row['passes']['info_recall']}/{row['counts']['info_recall']} "
            f"| {row['passes']['analysis']}/{row['counts']['analysis']} "
            f"| {row['passes']['presentation']}/{row['counts']['presentation']} "
            f"| {run.get('thread_count')} | {run.get('tool_calls')} | {run.get('evidence_count')} "
            f"| {row['research_tokens']:,} | {row['judge_tokens']:,} |"
        )
    h = result["holdout_summary"]
    f12 = result["fixed_12_summary"]
    lines += [
        "",
        "## 固定 12 题主汇总",
        "",
        f"- 交付并评分：{f12['delivered']}/12、{f12['scored']}/12。",
        f"- 逐题宏平均：**{percent(f12['macro_task_pass_rate'])}**。",
        f"- Rubric 微平均：**{f12['micro_pass']}/{f12['micro_count']} = {percent(f12['micro_pass_rate'])}**。",
        f"- 三维逐题宏平均：信息召回 {percent(f12['dimension_macro']['info_recall'])}；分析 {percent(f12['dimension_macro']['analysis'])}；呈现 {percent(f12['dimension_macro']['presentation'])}。",
        f"- 按官方 75%/17%/8% 三维权重计算的子集定位值：**{percent(f12['official_weighted_style'])}**。",
        f"- 已知 Token：研究 {f12['research_tokens_known']:,}；Judge {f12['judge_tokens_known']:,}；合计 {f12['total_tokens_known']:,}。",
        f"- 未知用量：研究调用 {f12['research_calls_usage_unknown']} 次；Judge 调用 {f12['judge_calls_usage_unknown']} 次。",
        f"- 各题研究耗时合计：{f12['research_elapsed_seconds_sum']:.1f} 秒。",
        "",
        "## 十道保留题单独汇总",
        "",
        f"- 交付并评分：{h['delivered']}/10、{h['scored']}/10。",
        f"- 逐题宏平均：{percent(h['macro_task_pass_rate'])}。",
        f"- Rubric 微平均：{h['micro_pass']}/{h['micro_count']} = {percent(h['micro_pass_rate'])}。",
        f"- 官方权重样式定位值：{percent(h['official_weighted_style'])}。",
        "",
        "## 执行边界与异常",
        "",
        "- 题 16 首次在受限 Windows 权限下发生 Vault Writer 发布失败；耐久任务随后在正常权限下从 `persist_result` 检查点恢复，未新增模型或检索调用。",
        "- 题 54 在第 2 次模型调用期间因原控制会话消失而中断；从同一检查点续跑，旧调用用量记未知，没有创建第二条研究轨迹。",
        "- 题 11 与 49 各保留 1 个模型生成的省略号 Evidence 标记导出问题；正文仍按既定协议评分。",
        "- LangSmith 遥测上传多次失败，但本地模型账本、检查点、报告和评分均已落盘，不影响研究结果。",
        "- 主结果是固定 12 题子集（2 道校准题 + 10 道保留题），不是公开 Benchmark 全量 132 题；不得直接宣称达到官方排行榜的某一名次。",
        "",
        "逐题原始报告、模型调用账本、停止原因和 Judge 批次保存在 `runs/<题号>-multi/`。",
    ]
    (BATCH / "最终结果.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"holdout": h, "fixed_12": f12}, ensure_ascii=True))


if __name__ == "__main__":
    main()
