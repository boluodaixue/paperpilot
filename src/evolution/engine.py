"""
M6 自进化引擎 — 训练循环编排 (Self-Evolution Engine)

SelfEvolutionEngine 实现完整的 MAE（Maker-Advisor-Evaluator）三角架构：
- Proposer: 生成研究问题（L1/L2/L3）
- Solver: DeepResearch Agent 本身
- Judge: 五维连续 reward 评分
- GRPO Trainer: 复用项目一 veRL 框架

设计决策：
1. 每轮生成 32 个问题，展开为 32 × 8 group = 256 trajectories，保证梯度方差可控。
2. Judge 评分后通过 shape_reward 映射到 [-1, 1]，直接喂给 veRL GRPO trainer。
3. 每 3 轮触发 Symbolic Learning，每 5 轮触发 Judge 校准。
4. 所有中间数据（parquet、checkpoint、log）按轮次目录隔离，便于追溯。
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from src.evolution.collector import TrajectoryCollector
from src.evolution.experience_memory import ExperienceMemory
from src.evolution.judge import Judge
from src.evolution.proposer import Proposer
from src.evolution.symbolic_learning import SymbolicLearner
from src.orchestrator.schemas import ResearchReport


__all__ = ["SelfEvolutionEngine"]

logger = logging.getLogger(__name__)


class SelfEvolutionEngine:
    """自进化引擎主控制器。

    Attributes:
        proposer: 问题生成器。
        solver: DeepResearch Agent（需实现 async run(query:str)->ResearchReport 接口）。
        judge: 多维评分器。
        trainer_config: veRL GRPO 训练配置字典。
        collector: Trajectory 收集器。
        experience_memory: 经验记忆库。
        symbolic_learner: Prompt 自优化器（可选）。
        output_dir: 每轮数据输出根目录。
    """

    def __init__(
        self,
        proposer: Proposer,
        solver: Any,
        judge: Judge,
        trainer_config: dict[str, Any],
        collector: TrajectoryCollector | None = None,
        experience_memory: ExperienceMemory | None = None,
        symbolic_learner: SymbolicLearner | None = None,
        output_dir: str = "./evolution_output",
    ):
        self.proposer = proposer
        self.solver = solver
        self.judge = judge
        self.trainer_config = trainer_config
        self.collector = collector or TrajectoryCollector()
        self.experience_memory = experience_memory or ExperienceMemory()
        self.symbolic_learner = symbolic_learner
        self.output_dir = output_dir

        # 运行状态
        self.current_round: int = 0
        self._failed_trajectories: list[dict[str, Any]] = []
        self._current_prompts: dict[str, str] = {}

    async def run(self, num_rounds: int = 20) -> dict[str, Any]:
        """运行多轮自进化。

        Args:
            num_rounds: 总进化轮数。

        Returns:
            汇总统计字典。
        """
        logger.info(f"[SelfEvolutionEngine] Starting {num_rounds} rounds of self-evolution")
        summary = {"rounds": [], "final_avg_score": 0.0}

        for r in range(1, num_rounds + 1):
            self.current_round = r
            logger.info(f"[SelfEvolutionEngine] === Round {r} ===")
            round_result = await self.run_round()
            summary["rounds"].append(round_result)

        # 计算最终平均分数
        scores = [r["avg_score"] for r in summary["rounds"] if "avg_score" in r]
        summary["final_avg_score"] = sum(scores) / len(scores) if scores else 0.0
        return summary

    async def run_round(self) -> dict[str, Any]:
        """执行一轮自进化。

        完整流程：
        1. Proposer.generate_batch() → 32 questions
        2. Solver 并行执行 → 32 reports + trajectories
        3. Collector 收集 → veRL 格式数据
        4. Judge.evaluate() → 5维连续 reward
        5. shape_reward_for_grpo() → 单值 reward [-1, 1]
        6. build_evolution_parquet() → veRL 标准 parquet
        7. veRL GRPO trainer: 50 steps（复用项目一 veRL）
        8. Experience Memory 更新
        9. (每 3 轮) Symbolic Learning → 优化 prompts
        10. (每 5 轮) Judge 校准
        11. Proposer.update_history()

        Returns:
            本轮统计字典。
        """
        round_stats: dict[str, Any] = {"round": self.current_round}

        # =====================================================================
        # Step 1: 生成研究问题
        # =====================================================================
        questions = await self.proposer.generate_batch(n=32)
        round_stats["num_questions"] = len(questions)
        logger.info(f"[Round {self.current_round}] Generated {len(questions)} questions")

        # =====================================================================
        # Step 2: Solver 执行（并行）
        # =====================================================================
        reports: list[ResearchReport] = []
        trajectories_list: list[list[dict[str, Any]]] = []

        # 使用 asyncio.gather 并行执行，但限制并发数以避免 OOM
        semaphore = asyncio.Semaphore(self.trainer_config.get("max_concurrent_solve", 8))

        async def _solve_one(q: str) -> tuple[ResearchReport, list[dict[str, Any]]]:
            async with semaphore:
                # Solver 需实现 async run(query) -> (report, trajectory)
                if hasattr(self.solver, "run"):
                    if asyncio.iscoroutinefunction(self.solver.run):
                        result = await self.solver.run(q)
                    else:
                        result = self.solver.run(q)
                else:
                    # fallback：返回空报告
                    result = ResearchReport(query=q, content="")

                # 统一返回格式
                if isinstance(result, tuple) and len(result) == 2:
                    report, traj = result
                elif isinstance(result, ResearchReport):
                    report = result
                    traj = []
                else:
                    report = ResearchReport(query=q, content=str(result))
                    traj = []
                return report, traj

        solve_tasks = [_solve_one(q) for q in questions]
        solve_results = await asyncio.gather(*solve_tasks, return_exceptions=True)

        for q, res in zip(questions, solve_results):
            if isinstance(res, Exception):
                logger.warning(f"[Round {self.current_round}] Solver failed for query={q}: {res}")
                reports.append(ResearchReport(query=q, content=""))
                trajectories_list.append([])
            else:
                reports.append(res[0])
                trajectories_list.append(res[1])

        # =====================================================================
        # Step 3: Collector 收集并转换格式
        # =====================================================================
        collected_batch: list[dict[str, Any]] = []
        for q, report, traj in zip(questions, reports, trajectories_list):
            collected = self.collector.collect(q, report, traj)
            collected_batch.append(collected)

        verl_data = self.collector.batch_to_verl(collected_batch)
        round_stats["num_trajectories"] = len(verl_data)

        # =====================================================================
        # Step 4 & 5: Judge 评分 + Reward Shaping
        # =====================================================================
        rewards: list[float] = []
        scores_list: list[dict[str, float]] = []
        for report in reports:
            if not report.content:
                # 空报告给最低分
                rewards.append(-1.0)
                scores_list.append({})
                continue
            try:
                scores = await self.judge.evaluate(report)
                r = self.judge.shape_reward(scores)
                rewards.append(r)
                scores_list.append(scores)
            except Exception as e:
                logger.warning(f"Judge failed: {e}")
                rewards.append(0.0)
                scores_list.append({})

        round_stats["avg_reward"] = sum(rewards) / len(rewards) if rewards else 0.0
        round_stats["avg_score"] = (
            sum(
                s.get("factual_accuracy", 0.0) * 0.3
                + s.get("coverage", 0.0) * 0.25
                + s.get("logical_coherence", 0.0) * 0.2
                + s.get("citation_quality", 0.0) * 0.15
                + s.get("efficiency", 0.0) * 0.1
                for s in scores_list
                if s
            )
            / max(len([s for s in scores_list if s]), 1)
        )

        # 将 reward 写回 verl_data
        for item, r in zip(verl_data, rewards):
            item["reward"] = r

        # =====================================================================
        # Step 6: 构建 evolution parquet
        # =====================================================================
        round_dir = os.path.join(self.output_dir, f"round_{self.current_round:03d}")
        os.makedirs(round_dir, exist_ok=True)
        parquet_path = os.path.join(round_dir, "evolution_data.parquet")

        try:
            import pandas as pd

            df = pd.DataFrame(verl_data)
            df.to_parquet(parquet_path, index=False)
            round_stats["parquet_path"] = parquet_path
            logger.info(f"[Round {self.current_round}] Parquet saved to {parquet_path}")
        except Exception as e:
            logger.warning(f"Failed to save parquet: {e}")
            round_stats["parquet_path"] = ""

        # =====================================================================
        # Step 7: veRL GRPO 训练（复用项目一 veRL）
        # =====================================================================
        # NOTE: 这里通过调用项目一已实现的 veRL GRPO trainer 进行训练。
        # 用户需保证 trainer_config 包含所有必要参数（model_path, rollout_size, 等）。
        # 以下是伪代码框架，展示如何接入项目一 veRL：
        #
        # from verl.trainer.ppo.ray_trainer import RayPPOTrainer
        # from verl.utils.dataset.rl_dataset import RLHFDataset
        #
        # dataset = RLHFDataset(parquet_files=[parquet_path], ...)
        # trainer = RayPPOTrainer(config=self.trainer_config, dataset=dataset)
        # trainer.fit()
        #
        # 训练完成后，更新的模型权重会自动保存到 checkpoint_dir。
        # =====================================================================
        checkpoint_dir = os.path.join(round_dir, "checkpoint")
        os.makedirs(checkpoint_dir, exist_ok=True)
        round_stats["checkpoint_dir"] = checkpoint_dir
        logger.info(
            f"[Round {self.current_round}] GRPO training placeholder: "
            f"load {parquet_path} -> veRL trainer -> save to {checkpoint_dir}"
        )

        # =====================================================================
        # Step 8: Experience Memory 更新
        # =====================================================================
        success_threshold = self.trainer_config.get("success_threshold", 6.0)
        for q, report, traj, scores, r in zip(
            questions, reports, trajectories_list, scores_list, rewards
        ):
            success = r >= (success_threshold / 5.0 - 1.0)  # 映射到 [-1,1]
            score = max(scores.values()) if scores else 0.0
            strategy_summary = self._summarize_strategy(traj)
            self.experience_memory.add(
                trajectory=traj,
                success=success,
                score=score,
                strategy_summary=strategy_summary,
                current_round=self.current_round,
            )

        # 淘汰旧经验
        evicted = self.experience_memory.evict_old_experiences(
            max_age_rounds=5,
            current_round=self.current_round,
        )
        round_stats["evicted_experiences"] = evicted

        # =====================================================================
        # Step 9: (每 3 轮) Symbolic Learning
        # =====================================================================
        if self.symbolic_learner is not None and self.current_round % 3 == 0:
            logger.info(f"[Round {self.current_round}] Triggering Symbolic Learning...")
            # 收集本轮回失败轨迹
            failed = [
                collected_batch[i]
                for i, r in enumerate(rewards)
                if r < 0.0
            ]
            self._failed_trajectories.extend(failed)
            # 最多保留最近 100 条失败轨迹
            self._failed_trajectories = self._failed_trajectories[-100:]

            new_prompts = await self.symbolic_learner.optimize_prompts(
                failed_trajectories=self._failed_trajectories,
                current_prompts=self._current_prompts,
            )
            # 回滚检查
            performance = {"avg_score": round_stats.get("avg_score", 0.0)}
            final_prompts = self.symbolic_learner.rollback_if_needed(
                new_prompts=new_prompts,
                performance=performance,
            )
            self._current_prompts = final_prompts
            round_stats["symbolic_learning_triggered"] = True
            round_stats["prompt_changed"] = new_prompts != final_prompts
        else:
            round_stats["symbolic_learning_triggered"] = False

        # =====================================================================
        # Step 10: (每 5 轮) Judge 校准
        # =====================================================================
        if self.current_round % 5 == 0:
            logger.info(f"[Round {self.current_round}] Triggering Judge Calibration...")
            calib_result = self.judge.calibrate()
            round_stats["judge_calibration"] = calib_result
        else:
            round_stats["judge_calibration"] = {"status": "skipped"}

        # =====================================================================
        # Step 11: Proposer 历史更新
        # =====================================================================
        for q, scores in zip(questions, scores_list):
            if not scores:
                continue
            composite = (
                scores.get("factual_accuracy", 0.0) * 0.3
                + scores.get("coverage", 0.0) * 0.25
                + scores.get("logical_coherence", 0.0) * 0.2
                + scores.get("citation_quality", 0.0) * 0.15
                + scores.get("efficiency", 0.0) * 0.1
            )
            success = composite >= success_threshold
            self.proposer.update_history(q, success, composite)

        logger.info(
            f"[Round {self.current_round}] Done: avg_reward={round_stats['avg_reward']:.3f}, "
            f"avg_score={round_stats['avg_score']:.3f}"
        )
        return round_stats

    def _summarize_strategy(self, trajectory: list[dict[str, Any]]) -> str:
        """从 trajectory 中提取策略摘要，用于 Experience Memory 的 embedding。"""
        if not trajectory:
            return "empty_trajectory"
        # 提取所有 tool call 名称作为策略指纹
        tool_names: list[str] = []
        for step in trajectory:
            tool_calls = step.get("tool_calls", [])
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    name = tc.get("function", {}).get("name", "") if isinstance(tc, dict) else ""
                    if name:
                        tool_names.append(name)
        if tool_names:
            return "strategy: " + " -> ".join(tool_names)
        # fallback：用 content 前 100 字
        first_content = str(trajectory[0].get("content", ""))[:100]
        return first_content or "unknown_strategy"
