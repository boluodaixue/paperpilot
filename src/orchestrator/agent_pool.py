"""
Agent 生命周期管理 (AgentPool)

负责 Worker Agent 的创建、复用、超时和降级。
采用对象池模式减少重复创建开销，同时支持按 TaskType 路由到不同 Agent 实现。
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..agents.base_agent import BaseAgent
    from .schemas import TaskType


__all__ = ["AgentPool"]


class AgentPool:
    """Agent 对象池。

    设计要点:
      - 延迟创建：首次请求某类型 Agent 时才实例化
      - 复用策略：同类型 Agent 在释放后回到池中复用
      - 降级策略：当 Agent 执行超时或异常时，标记为"需重建"
      - 线程安全：asyncio 单线程模型下无需锁，但状态变更需原子性

    Attributes:
        policy_factory: 无参工厂函数，返回 policy 实例。
        tools_factory: 无参工厂函数，返回 tools 列表。
        max_idle: 每类型最大空闲 Agent 数，防止内存膨胀。
    """

    def __init__(
        self,
        policy_factory,
        tools_factory=None,
        max_idle: int = 3,
    ) -> None:
        self.policy_factory = policy_factory
        self.tools_factory = tools_factory
        self.max_idle = max(max_idle, 1)

        # 类型 -> 空闲 Agent 列表
        self._idle: dict[str, list[BaseAgent]] = {}
        # 类型 -> 活跃 Agent 数量（用于限制并发，非精确对象追踪）
        self._active_count: dict[str, int] = {}
        # 类型 -> 总创建数（监控用）
        self._created_count: dict[str, int] = {}
        # 类型 -> 异常/需重建标记次数
        self._degraded_count: dict[str, int] = {}

    # ------------------------------------------------------------------
    # 核心 API
    # ------------------------------------------------------------------

    async def get_agent(self, task_type: "TaskType") -> BaseAgent:
        """根据任务类型获取可用的 Agent 实例。

        优先从池中复用，无空闲则新建。
        """
        type_key = task_type.value

        # 初始化该类型的计数器
        if type_key not in self._idle:
            self._idle[type_key] = []
            self._active_count[type_key] = 0
            self._created_count[type_key] = 0
            self._degraded_count[type_key] = 0

        # 尝试复用空闲 Agent
        while self._idle[type_key]:
            agent = self._idle[type_key].pop()
            # 简单健康检查：若 Agent 内部 policy 被标记为截断/污染，则丢弃
            if hasattr(agent, "policy") and getattr(agent.policy, "was_truncated", False):
                self._degraded_count[type_key] += 1
                continue  # 丢弃，尝试下一个
            self._active_count[type_key] += 1
            return agent

        # 新建 Agent
        agent = self._create_agent(type_key)
        self._created_count[type_key] += 1
        self._active_count[type_key] += 1
        return agent

    async def release_agent(self, agent: "BaseAgent") -> None:
        """释放 Agent 回对象池。

        若 Agent 状态异常（如 policy was_truncated），则丢弃不回收。
        """
        if agent is None:
            return

        # 推断类型（从 agent 名称或类名推断）
        type_key = self._infer_type_key(agent)

        self._active_count[type_key] = max(0, self._active_count.get(type_key, 0) - 1)

        # 健康检查
        if hasattr(agent, "policy") and getattr(agent.policy, "was_truncated", False):
            self._degraded_count[type_key] = self._degraded_count.get(type_key, 0) + 1
            return  # 不回收

        # 回收
        idle_list = self._idle.setdefault(type_key, [])
        if len(idle_list) < self.max_idle:
            idle_list.append(agent)

    def get_stats(self) -> dict[str, dict]:
        """返回对象池统计信息。"""
        stats = {}
        for key in set(list(self._idle.keys()) + list(self._active_count.keys())):
            stats[key] = {
                "idle": len(self._idle.get(key, [])),
                "active": self._active_count.get(key, 0),
                "created": self._created_count.get(key, 0),
                "degraded": self._degraded_count.get(key, 0),
            }
        return stats

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _create_agent(self, type_key: str) -> "BaseAgent":
        """根据类型键创建对应的 Agent 实例。"""
        policy = self.policy_factory()
        tools = self.tools_factory() if self.tools_factory else []

        # 延迟导入避免循环依赖
        from ..agents.researcher import ResearcherAgent
        from ..agents.summarizer import SummarizerAgent
        from .schemas import TaskType

        if type_key == TaskType.SEARCH.value:
            return ResearcherAgent(name=f"researcher_{type_key}", policy=policy, tools=tools)
        elif type_key == TaskType.ANALYZE.value:
            return ResearcherAgent(name=f"analyzer_{type_key}", policy=policy, tools=tools)
        elif type_key == TaskType.VERIFY.value:
            return ResearcherAgent(name=f"verifier_{type_key}", policy=policy, tools=tools)
        elif type_key == "synthesize":
            return SummarizerAgent(name="summarizer", policy=policy, tools=tools)
        else:
            # 默认降级为 Researcher
            return ResearcherAgent(name=f"researcher_default", policy=policy, tools=tools)

    def _infer_type_key(self, agent: "BaseAgent") -> str:
        """从 Agent 实例推断其类型键。"""
        # 简单启发式：通过类名推断
        cls_name = agent.__class__.__name__
        if "Summarizer" in cls_name:
            return "synthesize"
        # ResearcherAgent 用于 search/analyze/verify，统一归到 search
        from .schemas import TaskType
        return TaskType.SEARCH.value
