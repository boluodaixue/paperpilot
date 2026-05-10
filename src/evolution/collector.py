"""
M6 自进化引擎 — Trajectory 收集器

TrajectoryCollector 负责将 DeepResearch Agent 的完整执行轨迹收集并转换为
veRL 训练所需的格式。它是 Solver（DeepResearch Agent）与训练框架之间的适配层。

设计决策：
1. 收集的内容包括：query、report、多轮交互轨迹、搜索次数、重规划次数等。
2. to_verl_format 方法复用项目一的 parquet 构建逻辑，输出标准格式。
3. 支持批量收集，便于后续构建训练数据集。
"""
from __future__ import annotations

import json
from typing import Any

from src.orchestrator.schemas import ResearchReport


__all__ = ["TrajectoryCollector"]


class TrajectoryCollector:
    """Trajectory 收集与格式转换器。

    Attributes:
        system_prompt: 可选的系统级 prompt，用于 veRL 数据格式。
    """

    def __init__(self, system_prompt: str = ""):
        self.system_prompt = system_prompt

    def collect(
        self,
        query: str,
        report: ResearchReport,
        trajectories: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """收集单次 DeepResearch 的完整轨迹。

        Args:
            query: 原始研究问题。
            report: 最终生成的研究报告。
            trajectories: 多轮交互轨迹列表，每轮包含 role/content/tool_calls 等。

        Returns:
            统一格式的 trajectory 字典，包含 veRL 所需的所有字段。
        """
        return {
            "query": query,
            "report_content": report.content,
            "sources": report.sources,
            "confidence": report.confidence,
            "num_searches": report.num_searches,
            "num_replan": report.num_replan,
            "adversarial_rounds": report.adversarial_rounds,
            "final_score": report.final_score,
            "trajectories": trajectories,
            # 元信息
            "trajectory_length": len(trajectories),
            "content_length": len(report.content),
            "source_count": len(report.sources),
        }

    def to_verl_format(self, data: dict[str, Any]) -> dict[str, Any]:
        """将收集的 trajectory 转换为 veRL 训练所需的 parquet 行格式。

        veRL 期望的字段（与项目一 scripts/11_build_grpo_parquet.py 对齐）：
        - prompt: list[dict] — 多轮对话格式，包含 system + user 初始 query
        - response: str — 模型的完整输出（report content）
        - trajectories: list[dict] — 多轮交互轨迹（observation, action pairs）
        - metadata: dict — 额外元信息

        Args:
            data: collect() 方法的输出。

        Returns:
            veRL 格式的字典，可直接写入 parquet。
        """
        query = data.get("query", "")
        trajectories = data.get("trajectories", [])
        report_content = data.get("report_content", "")

        # 构建 prompt 字段：system + user query
        prompt_messages: list[dict[str, str]] = []
        if self.system_prompt:
            prompt_messages.append({"role": "system", "content": self.system_prompt})
        prompt_messages.append({"role": "user", "content": query})

        # metadata 包含所有原始字段（去除大字段避免 parquet 膨胀）
        metadata = {
            "query": query,
            "num_searches": data.get("num_searches", 0),
            "num_replan": data.get("num_replan", 0),
            "adversarial_rounds": data.get("adversarial_rounds", 0),
            "final_score": data.get("final_score", 0.0),
            "trajectory_length": data.get("trajectory_length", 0),
            "source_count": data.get("source_count", 0),
            "content_length": data.get("content_length", 0),
        }

        return {
            "prompt": prompt_messages,
            "response": report_content,
            "trajectories": trajectories,
            "metadata": metadata,
        }

    def batch_to_verl(
        self, batch: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """批量转换为 veRL 格式。

        Args:
            batch: collect() 输出列表。

        Returns:
            veRL 格式字典列表。
        """
        return [self.to_verl_format(item) for item in batch]

    def serialize(self, data: dict[str, Any]) -> str:
        """将 trajectory 序列化为 JSON 字符串（用于日志或持久化）。"""
        return json.dumps(data, ensure_ascii=False, indent=2)
