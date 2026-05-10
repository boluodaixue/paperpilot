"""
Agent 抽象基类

所有可执行 SubTask 的 Agent 必须继承 BaseAgent。
采用策略模式 (Strategy Pattern)：policy 对象通过依赖注入传入，便于单元测试时 mock。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orchestrator.schemas import SubTask, AgentResult


__all__ = ["BaseAgent"]


class BaseAgent(ABC):
    """Agent 抽象基类。

    Attributes:
        name: Agent 实例名称，用于日志和监控。
        policy: VLLMPolicy 实例，提供 LLM 调用能力。
        tools: 当前 Agent 可用的工具列表。
    """

    def __init__(self, name: str, policy, tools: list | None = None):
        """初始化 Agent。

        Args:
            name: Agent 名称。
            policy: VLLMPolicy 实例（或任何实现了 __call__(messages) 接口的对象）。
            tools: 可选的工具列表，元素需有 name / description / execute 接口。
        """
        self.name = name
        self.policy = policy
        self.tools = tools or []

    @abstractmethod
    async def run(self, task: "SubTask", context: dict) -> "AgentResult":
        """执行给定的 SubTask。

        Args:
            task: 待执行的原子任务。
            context: 全局共享上下文（Memory 的快照），只读。

        Returns:
            AgentResult: 包含状态、输出、轨迹等。
        """
        pass

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name} tools={len(self.tools)}>"
