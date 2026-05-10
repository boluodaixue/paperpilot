#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/run_evolution.py
================================================================================
自进化训练脚本

功能：
    1. 启动 M6 Self-Evolution Engine 的 MAE（Multi-Agent Evolution）三角训练循环
    2. 每轮保存模型 checkpoint
    3. 记录训练曲线（奖励、KL 散度、损失等）到 JSONL

Usage:
    python run_evolution.py --config configs/evolution/grpo_online.yaml
================================================================================
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def setup_logging(log_level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def load_config(config_path: str) -> dict:
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"配置文件未找到: {config_path}")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def initialize_evolution(config: dict) -> Any:
    """初始化 Self-Evolution Engine 及其依赖模块。"""
    logger = logging.getLogger("run_evolution")
    logger.info("正在初始化自进化引擎...")

    # 加载 LLM policy（使用 judge_policy 或 default）
    from src.models.model_router import ModelRouter

    model_cfg = config.get("model", {})
    backend = model_cfg.get("backend", "deepseek")
    backend_sampling = model_cfg.get("backend_sampling", {})
    kwargs = backend_sampling.get(backend, {})
    policy = ModelRouter.create_backend(backend, **kwargs)
    logger.info(f"[LLM] Policy 后端已加载: {backend}")

    # 初始化 Orchestrator 作为 Solver
    from src.core.runner import initialize_modules

    research_config = config.get("research_config", {})
    modules = initialize_modules(research_config)
    solver = modules["orchestrator"]
    logger.info("[Solver] Orchestrator 已初始化")

    # 初始化 Proposer
    from src.evolution.proposer import Proposer

    proposer = Proposer(policy=policy)
    logger.info("[Proposer] 已初始化")

    # 初始化 Judge
    from src.evolution.judge import Judge

    judge_cfg = config.get("judge", {})
    judge = Judge(
        policy=policy,
        ensemble_size=judge_cfg.get("ensemble_size", 3),
        efficiency_optimal=judge_cfg.get("efficiency_optimal", 5),
        efficiency_scale=judge_cfg.get("efficiency_scale", 3.0),
    )
    logger.info("[Judge] 已初始化")

    # 初始化 Self-Evolution Engine
    from src.evolution.engine import SelfEvolutionEngine

    trainer_config = config.get("trainer", {})
    output_dir = config.get("logging", {}).get("output_dir", "./evolution_output")
    engine = SelfEvolutionEngine(
        proposer=proposer,
        solver=solver,
        judge=judge,
        trainer_config=trainer_config,
        output_dir=output_dir,
    )
    logger.info("[M6] Self-Evolution Engine 已初始化")
    return engine


def main() -> None:
    parser = argparse.ArgumentParser(
        description="自进化训练脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/run_evolution.py --config configs/evolution/grpo_online.yaml
  python scripts/run_evolution.py --config configs/evolution/grpo_online.yaml --output_dir checkpoints/exp1
        """,
    )
    parser.add_argument(
        "--config",
        type=str,
        default="configs/evolution/grpo_online.yaml",
        help="GRPO 训练配置文件路径",
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default=None,
        help="覆盖配置中的输出目录",
    )
    parser.add_argument(
        "--num_rounds",
        type=int,
        default=20,
        help="进化总轮数",
    )
    parser.add_argument(
        "--log_level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别",
    )
    args = parser.parse_args()

    setup_logging(args.log_level)
    logger = logging.getLogger("main")

    try:
        config = load_config(args.config)
        if args.output_dir:
            config.setdefault("logging", {})["output_dir"] = args.output_dir

        engine = initialize_evolution(config)
        summary = asyncio.run(engine.run(num_rounds=args.num_rounds))

        # 保存最终汇总
        output_dir = config.get("logging", {}).get("output_dir", "./evolution_output")
        summary_path = os.path.join(output_dir, "evolution_summary.json")
        os.makedirs(output_dir, exist_ok=True)
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)

        logger.info(f"自进化完成！汇总已保存: {summary_path}")
        logger.info(f"最终平均得分: {summary.get('final_avg_score', 0.0):.4f}")

    except Exception as e:
        logger.exception("训练过程中发生错误")
        sys.exit(1)


if __name__ == "__main__":
    main()
