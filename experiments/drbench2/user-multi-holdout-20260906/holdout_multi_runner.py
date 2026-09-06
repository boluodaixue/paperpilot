"""Run the ten user-authorized DRBench2 holdout cases with frozen multi-Agent settings."""
from __future__ import annotations

import asyncio
import json
import msvcrt
import sys
import time
from pathlib import Path


HERE = Path(__file__).resolve().parent
DRB = HERE.parent
sys.path.insert(0, str(DRB))

import freeze_protocol as freeze
import continuation_runtime as runtime
import continuation_score as scoring


BATCH = HERE
RUNS = BATCH / "runs"
ORDER = (16, 11, 49, 42, 69, 72, 57, 54, 87, 86)
KNOWN_JUDGE_FAILURES = {
    "ValueError", "TimeoutError", "APITimeoutError", "APIConnectionError",
    "RateLimitError", "InternalServerError",
}


def patch_output_root() -> None:
    runtime.SPEC = BATCH
    scoring.SPEC = BATCH


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def run_state(idx: int) -> dict | None:
    path = RUNS / f"{idx}-multi" / "run.json"
    return read_json(path) if path.exists() else None


def usage(folder: Path) -> dict:
    calls = read_json(folder / "model_calls.json") if (folder / "model_calls.json").exists() else []
    known = [row.get("usage") for row in calls if isinstance(row.get("usage"), dict)]
    judge_rows = []
    if (folder / "judge_batches").exists():
        judge_rows = [read_json(path) for path in (folder / "judge_batches").glob("*-attempt-*.json")]
    judge_known = [row.get("usage") for row in judge_rows if isinstance(row.get("usage"), dict)]
    return {
        "research_calls": len(calls),
        "research_tokens_known": sum((row.get("total_tokens") or 0) for row in known),
        "research_calls_usage_unknown": len(calls) - len(known),
        "judge_attempts": len(judge_rows),
        "judge_tokens_known": sum((row.get("total_tokens") or 0) for row in judge_known),
        "judge_calls_usage_unknown": len(judge_rows) - len(judge_known),
    }


def write_readout(status: dict) -> None:
    rows = []
    for idx in ORDER:
        folder = RUNS / f"{idx}-multi"
        run = run_state(idx) or {"status": "not_started", "judge_status": "not_run"}
        judge_path = folder / "judge.json"
        judge = read_json(judge_path) if judge_path.exists() and run.get("judge_status") == "valid" else None
        rows.append({
            "task": idx,
            "arm": "multi",
            "run": run,
            "summary": judge.get("summary") if judge else None,
            **usage(folder),
        })
    runtime.dump(BATCH / "holdout_readout.json", rows)
    status["elapsed_seconds"] = time.time() - status["started_at"]
    runtime.dump(BATCH / "holdout_status.json", status)


def main() -> int:
    patch_output_root()
    spec = runtime.verify_seal()
    if tuple(spec["holdout_ids"]) != ORDER:
        raise ValueError("Frozen holdout order changed")
    with (BATCH / "holdout.lock").open("a+b") as lock:
        lock.seek(0)
        lock.write(b"0")
        lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        status = {
            "status": "running",
            "authorization": "user confirmed run remaining test set; multi-Agent only",
            "benchmark": "DeepResearch Bench II",
            "benchmark_repository": "https://github.com/SawyerCooper/DeepResearchBench2",
            "benchmark_commit": "440bdc33728438d317ca7860809942b5a1e40256",
            "scope": "ten fixed holdout tasks, frozen default multi-Agent only; no single runs",
            "planned": [{"task": idx, "arm": "multi"} for idx in ORDER],
            "completed": [],
            "started_at": time.time(),
        }
        write_readout(status)
        try:
            for idx in ORDER:
                runtime.verify_seal()
                folder = RUNS / f"{idx}-multi"
                state = run_state(idx)
                status.update(task=idx, arm="multi", action="research")
                write_readout(status)
                if folder.exists() and state is None:
                    raise ValueError(f"Task {idx} directory exists without run state; duplicate refused")
                if state is None:
                    try:
                        asyncio.run(runtime.research(idx, "multi"))
                    except BaseException as exc:
                        status.update(status="stopped", action="inspect_systemic_research_failure",
                                      reason=f"task {idx}: {type(exc).__name__}")
                        write_readout(status)
                        return 1
                    state = run_state(idx)
                if not state or state.get("status") != "returned":
                    status.update(status="stopped", action="inspect_incomplete_research",
                                  reason=f"task {idx}: non-terminal research state")
                    write_readout(status)
                    return 1
                if not state.get("delivery_issues"):
                    status.update(action="judge")
                    write_readout(status)
                    if state.get("judge_status") != "valid":
                        try:
                            scoring.judge(idx, "multi")
                        except Exception as exc:
                            state = run_state(idx) or {}
                            if state.get("judge_status") != "failed" or state.get("judge_error_type") not in KNOWN_JUDGE_FAILURES:
                                status.update(status="stopped", action="inspect_systemic_judge_failure",
                                              reason=f"task {idx}: {type(exc).__name__}")
                                write_readout(status)
                                return 1
                            status.setdefault("nonfatal_failures", []).append({
                                "task": idx, "phase": "judge", "error_type": state.get("judge_error_type")
                            })
                else:
                    status.setdefault("nonfatal_failures", []).append({
                        "task": idx, "phase": "delivery", "issues": state.get("delivery_issues")
                    })
                state = run_state(idx) or {}
                status["completed"].append({
                    "task": idx,
                    "arm": "multi",
                    "status": state.get("status", "missing"),
                    "delivery_issues": state.get("delivery_issues", []),
                    "judge_status": state.get("judge_status", "not_run"),
                })
                write_readout(status)
            status.update(status="completed", action="stopped_after_ten_holdouts", task=None, arm=None)
            write_readout(status)
            return 0
        except Exception as exc:
            status.update(status="stopped", action="inspect_batch_failure",
                          reason=str(exc), error_type=type(exc).__name__)
            write_readout(status)
            return 1


if __name__ == "__main__":
    sys.exit(main())
