"""Phase 3 多级模型与对抗性能优化测试（无需 API key）。"""

from __future__ import annotations

import json

import pytest

from src.adversarial.blue_agent import (
    PRESERVE_CITATIONS_INSTRUCTION,
    PROMPT_IN_PLACE_FIX,
    PROMPT_REMOVAL,
    PROMPT_SUPPLEMENTARY_SEARCH,
    BlueAgent,
)
from src.adversarial.red_agent import DIMENSION_PROMPTS, RedAgent
from src.adversarial.verdict import Dimension
from src.orchestrator.schemas import ResearchReport


class _AttrDict(dict):
    """模拟 OpenAICompatibleDict：支持 .content 属性访问。"""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)


class CannedPolicy:
    """罐头 LLM policy：按消息内容返回预设 JSON。"""

    def __init__(self, responder) -> None:
        self.responder = responder
        self.tools = None
        self.max_tokens = 1024
        self.calls = 0

    def __call__(self, messages):
        self.calls += 1
        return _AttrDict(content=self.responder(messages))


def _report(content: str = "Report body.") -> ResearchReport:
    return ResearchReport(
        query="test query",
        content=content,
        confidence=0.5,
        sources=[{"title": "S1", "url": "http://x", "snippet": "snip"}],
    )


def _ok_json(dim: Dimension) -> str:
    return json.dumps(
        {
            "score": 8.0,
            "issues": [
                {
                    "severity": "minor",
                    "description": f"{dim.value} issue",
                    "location": "para1",
                    "fix_type": "in_place",
                    "evidence": "ev",
                }
            ],
        }
    )


# ---------------------------------------------------------------------------
# RedAgent 五维度并行攻击
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_red_agent_attacks_all_five_dimensions():
    """并行化后 5 个维度全部被评估并汇总。"""

    def responder(messages):
        user = messages[-1]["content"]
        # 从 prompt 中识别维度（各 prompt 有不同标题）
        if "事实核查" in user:
            return _ok_json(Dimension.FACTUAL)
        if "幻觉检测" in user:
            return _ok_json(Dimension.HALLUCINATION)
        if "逻辑一致性" in user:
            return _ok_json(Dimension.LOGICAL)
        if "来源可信度" in user:
            return _ok_json(Dimension.SOURCE_CREDIBILITY)
        if "覆盖完整度" in user:
            return _ok_json(Dimension.COVERAGE)
        return "{}"

    policy = CannedPolicy(responder)
    agent = RedAgent(policy=policy)
    verdict = await agent.attack(_report())

    assert policy.calls == 5
    assert set(verdict.dimension_scores) == set(Dimension)
    assert all(s == 8.0 for s in verdict.dimension_scores.values())
    assert len(verdict.issues) == 5  # 每个维度 1 个 issue
    assert verdict.overall_score > 0


@pytest.mark.asyncio
async def test_red_agent_dimension_failure_is_conservative():
    """单个维度抛异常时返回保守分数，不影响其他维度。"""

    def responder(messages):
        user = messages[-1]["content"]
        if "幻觉检测" in user:
            raise RuntimeError("api down")
        return _ok_json(Dimension.FACTUAL)

    policy = CannedPolicy(responder)
    agent = RedAgent(policy=policy)
    verdict = await agent.attack(_report())

    assert set(verdict.dimension_scores) == set(Dimension)  # 5 个都有分数
    assert verdict.dimension_scores[Dimension.HALLUCINATION] == 5.0  # 保守分
    assert verdict.dimension_scores[Dimension.FACTUAL] == 8.0  # 正常维度不受影响


@pytest.mark.asyncio
async def test_red_agent_parse_failure_scores_conservative():
    """解析失败（非 JSON）也返回保守分数。"""

    class PlainPolicy:
        tools = None
        max_tokens = 1024

        def __call__(self, messages):
            return _AttrDict(content="not json at all")

    agent = RedAgent(policy=PlainPolicy())
    verdict = await agent.attack(_report())
    assert all(s == 5.0 for s in verdict.dimension_scores.values())


def test_red_agent_sets_max_tokens_once():
    """构造时一次性设置 policy.max_tokens，避免并发竞态。"""
    policy = CannedPolicy(lambda m: "{}")
    RedAgent(policy=policy, max_tokens=2048)
    assert policy.max_tokens == 2048


# ---------------------------------------------------------------------------
# BlueAgent 保留 [E-x] 引用
# ---------------------------------------------------------------------------

def test_blue_prompts_preserve_evidence_citations():
    assert PRESERVE_CITATIONS_INSTRUCTION in PROMPT_IN_PLACE_FIX
    assert PRESERVE_CITATIONS_INSTRUCTION in PROMPT_SUPPLEMENTARY_SEARCH
    assert PRESERVE_CITATIONS_INSTRUCTION in PROMPT_REMOVAL
    assert "[E-<数字>]" in PRESERVE_CITATIONS_INSTRUCTION


# ---------------------------------------------------------------------------
# 多级模型：config 按模块指定 model_name
# ---------------------------------------------------------------------------

def test_config_module_sampling():
    from src.core.runner import load_config

    config = load_config()
    modules = config["model"]["backend_sampling"]["modules"]
    # 每个模块都有采样参数
    for m in ("solver", "planner", "summarizer", "red_agent", "blue_agent",
              "judge", "compressor", "extractor"):
        assert "temperature" in modules[m]
        assert modules[m]["max_tokens"] > 0
    # 无 model_name 覆盖时继承 .env 默认模型（当前 key 仅 flash 可用）
    assert all("model_name" not in modules[m] for m in modules)


def test_adversarial_disabled_by_default():
    from src.core.runner import load_config

    config = load_config()
    assert config["adversarial"]["enabled"] is False
    assert config["adversarial"]["max_rounds"] == 2
