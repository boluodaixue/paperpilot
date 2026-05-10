"""
Token 预算追踪器

在 Deep Research 长链路中，上下文长度可能迅速膨胀。
BudgetTracker 提供显式的 token 使用监控，供编排器决定是否触发压缩或截断。
"""
from __future__ import annotations

from dataclasses import dataclass, field


__all__ = ["BudgetTracker", "BudgetSnapshot"]


@dataclass
class BudgetSnapshot:
    """某一时刻的预算快照。"""
    total_tokens: int = 0
    budget_limit: int = 0
    usage_ratio: float = 0.0
    is_over_budget: bool = False


class BudgetTracker:
    """追踪累积 token 消耗，支持动态预算阈值。

    设计要点:
      - 线程安全由调用方保证（编排器在单 asyncio 事件循环中调用）
      - 阈值可运行时调整，支持渐进式压缩策略
      - 记录历史 usage，方便后续分析 token 膨胀曲线
    """

    def __init__(self, budget_limit: int = 100_000) -> None:
        """初始化预算追踪器。

        Args:
            budget_limit: token 预算上限，默认 100K（约 64K 上下文模型的安全区）。
        """
        self._budget_limit = max(budget_limit, 1)
        self._total_tokens: int = 0
        self._history: list[int] = []

    # ------------------------------------------------------------------
    # 核心操作
    # ------------------------------------------------------------------

    def track(self, tokens: int) -> None:
        """记录本次消耗的 token 数。"""
        if tokens < 0:
            raise ValueError(f"token 消耗不能为负数: {tokens}")
        self._total_tokens += tokens
        self._history.append(tokens)

    def get_usage(self) -> int:
        """返回当前累计 token 消耗。"""
        return self._total_tokens

    def get_usage_ratio(self) -> float:
        """返回当前消耗占预算的比例 [0.0, 1.0+]。"""
        return self._total_tokens / self._budget_limit

    def is_over_budget(self) -> bool:
        """是否已超出预算上限。"""
        return self._total_tokens >= self._budget_limit

    def is_near_budget(self, threshold: float = 0.8) -> bool:
        """是否接近预算上限（默认 80%）。

        用于提前触发压缩，避免硬截断导致信息丢失。
        """
        return self.get_usage_ratio() >= threshold

    def set_budget_limit(self, new_limit: int) -> None:
        """动态调整预算上限。"""
        self._budget_limit = max(new_limit, 1)

    def reset(self) -> None:
        """重置累计计数（通常在 replan 后调用）。"""
        self._total_tokens = 0
        self._history.clear()

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------

    def snapshot(self) -> BudgetSnapshot:
        """获取当前预算快照。"""
        return BudgetSnapshot(
            total_tokens=self._total_tokens,
            budget_limit=self._budget_limit,
            usage_ratio=self.get_usage_ratio(),
            is_over_budget=self.is_over_budget(),
        )

    def get_history(self) -> list[int]:
        """返回每次 track() 的历史记录。"""
        return list(self._history)

    def __repr__(self) -> str:
        return (
            f"<BudgetTracker used={self._total_tokens}/{self._budget_limit} "
            f"ratio={self.get_usage_ratio():.2%}>"
        )
