"""web/clarifier.py 澄清决策的单元测试（不真调 LLM）。"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import web.clarifier as cl


class CannedPolicy:
    def __init__(self, content: str, raise_error: bool = False) -> None:
        self.content = content
        self.raise_error = raise_error

    def __call__(self, messages):
        if self.raise_error:
            raise RuntimeError("api down")
        return {"content": self.content}


def test_confirm_parsed():
    out = cl.run_clarifier(
        CannedPolicy('{"action":"confirm","plan":{"topic":"T","scope":"S","angle":"A",'
                     '"depth":"D","focus_areas":["x","y"]},"research_query":"final q"}'),
        [], "研究transformer",
    )
    assert out["action"] == "confirm"
    assert out["plan"]["topic"] == "T"
    assert out["plan"]["focus_areas"] == ["x", "y"]
    assert out["research_query"] == "final q"


def test_confirm_missing_research_query_falls_back_to_user_msg():
    out = cl.run_clarifier(
        CannedPolicy('{"action":"confirm","plan":{"topic":"T"}}'),
        [], "原始问题",
    )
    assert out["action"] == "confirm"
    assert out["research_query"] == "原始问题"


def test_ask_parsed():
    out = cl.run_clarifier(
        CannedPolicy('{"action":"ask","question":"想侧重哪个阶段？"}'),
        [], "q",
    )
    assert out["action"] == "ask"
    assert out["question"] == "想侧重哪个阶段？"


def test_fenced_json():
    out = cl.run_clarifier(
        CannedPolicy('```json\n{"action":"ask","question":"fenced"}\n```'),
        [], "q",
    )
    assert out["action"] == "ask"


def test_surrounding_text_json():
    out = cl.run_clarifier(
        CannedPolicy('这里是思考 {"action":"confirm","plan":{"topic":"T"},"research_query":"r"} 结束'),
        [], "q",
    )
    assert out["action"] == "confirm"


def test_non_json_falls_back_to_ask():
    out = cl.run_clarifier(CannedPolicy("不是 JSON"), [], "q")
    assert out["action"] == "ask"
    assert out["question"]


def test_policy_error_falls_back_to_ask():
    out = cl.run_clarifier(CannedPolicy("", raise_error=True), [], "q")
    assert out["action"] == "ask"


class CapturingPolicy(CannedPolicy):
    def __call__(self, messages):
        self.seen_prompt = messages[-1]["content"]
        return super().__call__(messages)


def test_history_is_truncated_to_recent():
    p = CapturingPolicy('{"action":"ask","question":"x"}')
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"msg{i}", "kind": "chat"}
               for i in range(30)]
    cl.run_clarifier(p, history, "new q")
    assert "msg28" in p.seen_prompt  # 最新消息在
    assert "msg0" not in p.seen_prompt  # 最早消息被截掉