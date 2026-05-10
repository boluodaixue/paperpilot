"""
M5 Red-Blue 对抗降噪循环 — 主控制器

AdversarialLoop 驱动 Red Agent → Blue Agent → 评分的完整对抗流程，
具备死循环检测、震荡检测、收敛判断等鲁棒机制。

设计决策：
1. 维护 resolved_issues 集合：已修复的 issue 如果在新轮次中重新出现，判定为震荡。
2. 收敛条件三选一：round >= 3 / overall >= 8.0 / Δscore < 0.3。
3. 每轮记录完整评分历史，便于后续分析和审计。
"""
from __future__ import annotations

import copy
import logging
from typing import Any

from src.adversarial.blue_agent import BlueAgent
from src.adversarial.red_agent import RedAgent
from src.adversarial.verdict import (
    Dimension,
    FixOperation,
    Issue,
    RedVerdict,
    VerdictEngine,
)
from src.orchestrator.schemas import ResearchReport
from src.utils.tracing import trace_chain


__all__ = ["AdversarialLoop"]

logger = logging.getLogger(__name__)


class AdversarialLoop:
    """Red-Blue 对抗降噪循环主控制器。

    Attributes:
        red_agent: Red Agent 实例，负责攻击。
        blue_agent: Blue Agent 实例，负责修复。
        policy: 用于 self_verify 或辅助评分的策略对象（可选）。
        max_rounds: 硬上限轮数。
        score_threshold: 综合分达标阈值。
        delta_threshold: 轮间变化量收敛阈值。
    """

    def __init__(
        self,
        red_agent: RedAgent,
        blue_agent: BlueAgent,
        policy: Any | None = None,
        max_rounds: int = 3,
        score_threshold: float = 8.0,
        delta_threshold: float = 0.3,
    ):
        self.red_agent = red_agent
        self.blue_agent = blue_agent
        self.policy = policy
        self.max_rounds = max(max_rounds, 1)
        self.score_threshold = score_threshold
        self.delta_threshold = delta_threshold

    @trace_chain(name="adversarial_loop.run", tags=["m5", "loop", "adversarial"])
    async def run(
        self, report: ResearchReport
    ) -> tuple[ResearchReport, list[dict[str, Any]]]:
        """运行完整的对抗降噪循环。

        流程：
        1. 每轮用 Red Agent 攻击当前报告。
        2. Blue Agent 根据 Verdict 修复。
        3. 记录本轮评分和修复操作。
        4. 检查收敛条件或震荡/死循环。
        5. 返回最终报告和完整历史。

        Args:
            report: 初始研究报告（不会被修改，内部深拷贝）。

        Returns:
            (修复后的报告, 每轮评分记录列表)
            每轮记录包含: round, dimension_scores, overall_score, delta, issues_count,
            fix_operations, resolved_count, oscillation_detected, stop_reason
        """
        current = copy.deepcopy(report)
        history: list[dict[str, Any]] = []
        prev_scores: dict[Dimension, float] | None = None
        resolved_issues: set[Issue] = set()  # 已修复的 issue 集合
        oscillation_detected = False
        stop_reason = ""

        for round_idx in range(1, self.max_rounds + 1):
            logger.info(f"[AdversarialLoop] Round {round_idx} starting...")

            # ---- Step 1: Red Attack ----
            verdict = await self.red_agent.attack(current)
            logger.info(
                f"[AdversarialLoop] Red attack done: overall={verdict.overall_score:.2f}, "
                f"issues={len(verdict.issues)}"
            )

            # ---- Step 2: 震荡检测 ----
            # 如果当前 issues 中有已修复过的 issue 重新出现，判定震荡
            reappeared = resolved_issues.intersection(set(verdict.issues))
            if reappeared:
                oscillation_detected = True
                logger.warning(
                    f"[AdversarialLoop] Oscillation detected at round {round_idx}: "
                    f"{len(reappeared)} previously resolved issues reappeared."
                )
                stop_reason = f"oscillation_at_round_{round_idx}"
                history.append(self._build_round_record(
                    round_idx, verdict, [], len(reappeared), oscillation_detected, stop_reason
                ))
                break

            # ---- Step 3: Blue Defend ----
            fixed_report, operations = await self.blue_agent.defend(current, verdict)
            logger.info(
                f"[AdversarialLoop] Blue defend done: operations={len(operations)}"
            )

            # 将本轮被修复的 issues 加入 resolved 集合
            for op in operations:
                if op.success:
                    resolved_issues.add(op.issue)

            # ---- Step 4: 计算 delta ----
            delta = 0.0
            if prev_scores is not None:
                delta = VerdictEngine.compute_delta(prev_scores, verdict.dimension_scores)
            prev_scores = copy.deepcopy(verdict.dimension_scores)

            # ---- Step 5: 记录本轮 ----
            stop_reason = self._check_convergence(round_idx, verdict.overall_score, delta)
            record = self._build_round_record(
                round_idx=round_idx,
                verdict=verdict,
                operations=operations,
                resolved_count=len(resolved_issues),
                oscillation=oscillation_detected,
                stop_reason=stop_reason,
            )
            history.append(record)

            # 更新当前报告为修复后的版本
            current = fixed_report
            current.adversarial_rounds = round_idx

            # ---- Step 6: 判断是否停止 ----
            if stop_reason:
                logger.info(f"[AdversarialLoop] Stopping: {stop_reason}")
                break

        # 循环结束后写入最终分数
        if history:
            current.final_score = history[-1]["overall_score"]

        return current, history

    def _check_convergence(
        self, round_idx: int, overall_score: float, delta: float
    ) -> str:
        """检查是否满足任一收敛条件。

        Returns:
            空字符串表示继续；非空字符串为停止原因。
        """
        if round_idx >= self.max_rounds:
            return f"max_rounds_reached({self.max_rounds})"
        if overall_score >= self.score_threshold:
            return f"score_threshold_met({overall_score:.2f}>={self.score_threshold})"
        # 第一轮没有上一轮做对比，delta 恒为 0，跳过 delta 收敛检测
        if round_idx > 1 and delta < self.delta_threshold:
            return f"delta_converged({delta:.3f}<{self.delta_threshold})"
        return ""

    def _build_round_record(
        self,
        round_idx: int,
        verdict: RedVerdict,
        operations: list[FixOperation],
        resolved_count: int,
        oscillation: bool,
        stop_reason: str,
    ) -> dict[str, Any]:
        """构造单轮记录字典。"""
        return {
            "round": round_idx,
            "dimension_scores": {
                k.value: round(v, 3) for k, v in verdict.dimension_scores.items()
            },
            "overall_score": round(verdict.overall_score, 3),
            "issues_count": len(verdict.issues),
            "fix_operations": [op.to_dict() for op in operations],
            "resolved_count": resolved_count,
            "oscillation_detected": oscillation,
            "stop_reason": stop_reason,
            "raw_feedback": verdict.raw_feedback,
        }
