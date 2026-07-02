"""Gap Analyzer 缺口分析单元测试（不真调 LLM）。"""

from __future__ import annotations

from src.agents.gap_analyzer import run_gap_analysis


class CannedPolicy:
    def __init__(self, content: str, raise_error: bool = False) -> None:
        self.content = content
        self.raise_error = raise_error

    def __call__(self, messages):
        if self.raise_error:
            raise RuntimeError("api down")
        return {"content": self.content}


def test_continue_parses_tasks_and_gaps():
    p = CannedPolicy(
        '{"decision":"continue","reason":"缺eval证据","gaps":[{"topic":"eval","reason":"少"}],'
        '"tasks":[{"description":"补搜eval方向","search_hints":["eval","benchmark"]}]}'
    )
    plan = run_gap_analysis(p, "q", 1, ["arch", "eval"], {"arch": 5, "eval": 0}, 2)
    assert plan.decision == "continue"
    assert plan.gaps[0]["topic"] == "eval"
    assert len(plan.new_tasks) == 1
    assert plan.new_tasks[0].description == "补搜eval方向"
    assert plan.new_tasks[0].search_hints == ["eval", "benchmark"]


def test_complete_parses():
    p = CannedPolicy('{"decision":"complete","reason":"覆盖充分"}')
    plan = run_gap_analysis(p, "q", 1, ["a"], {"a": 3}, 2)
    assert plan.decision == "complete"
    assert plan.new_tasks == []
    assert "充分" in plan.reason


def test_fenced_json():
    p = CannedPolicy('```json\n{"decision":"complete","reason":"x"}\n```')
    assert run_gap_analysis(p, "q", 1, [], {}, 2).decision == "complete"


def test_continue_with_invalid_tasks_drops_them():
    p = CannedPolicy('{"decision":"continue","tasks":[{"description":""},{"description":"ok","search_hints":["h"]}]}')
    plan = run_gap_analysis(p, "q", 1, [], {}, 2)
    assert len(plan.new_tasks) == 1
    assert plan.new_tasks[0].description == "ok"


def test_policy_error_falls_back_complete():
    plan = run_gap_analysis(CannedPolicy("", raise_error=True), "q", 1, [], {}, 2)
    assert plan.decision == "complete"


def test_garbage_output_falls_back_complete():
    plan = run_gap_analysis(CannedPolicy("不是 JSON"), "q", 1, [], {}, 2)
    assert plan.decision == "complete"