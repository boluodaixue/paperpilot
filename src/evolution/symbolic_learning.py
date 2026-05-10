"""
M6 自进化引擎 — Symbolic Learning（Prompt 自优化）

SymbolicLearner 从失败轨迹中提取系统性错误模式，生成改进指令并更新 prompt。
具备版本管理和自动回滚机制，确保 prompt 演化过程的安全性。

设计决策：
1. 错误模式提取：由 LLM 分析失败轨迹，归纳共性错误类型。
2. Prompt 优化：将错误模式转化为具体的 prompt 改进指令（如"增加 XX 约束"）。
3. 版本管理：保留最近 10 个 prompt 版本，支持快速回滚。
4. 自动回滚：新 prompt 导致性能下降 >5% 时自动回退到上一版本。
"""
from __future__ import annotations

import copy
import json
import os
from typing import Any


__all__ = ["SymbolicLearner"]


# ============================================================================
# Prompt 模板
# ============================================================================

SYSTEM_SYMBOLIC = (
    "你是一位 Prompt Engineering 专家。你的任务是分析 AI Agent 的失败轨迹，"
    "提取系统性错误模式，并生成精确的 Prompt 改进指令。"
)

PROMPT_EXTRACT_PATTERNS = """请分析以下失败轨迹，提取系统性错误模式。

要求：
1. 只关注重复出现的错误类型（单次偶发错误忽略）。
2. 每个错误模式需包含：错误描述、发生频率、根因分析。
3. 按严重程度排序（严重 → 轻微）。

请按以下 JSON 格式输出：
{
  "patterns": [
    {
      "pattern_name": "string",
      "description": "string",
      "frequency": "high|medium|low",
      "root_cause": "string",
      "suggested_fix": "string"
    }
  ]
}

--- 失败轨迹列表 ---
{trajectories}
"""

PROMPT_OPTIMIZE_PROMPT = """请根据以下错误模式，优化给定的 Prompt。

优化原则：
1. 保持 prompt 的核心目标不变。
2. 针对每个错误模式，增加具体的约束或示例。
3. 避免 prompt 过长（控制在 2000 token 以内）。
4. 输出优化后的完整 prompt。

请按以下 JSON 格式输出：
{
  "optimized_prompts": {
    "prompt_name": "string",   // 如 "system_prompt"
    "new_content": "string",
    "changes": ["string"]      // 变更说明列表
  }
}

--- 错误模式 ---
{patterns}

--- 当前 Prompts ---
{current_prompts}
"""


# ============================================================================
# SymbolicLearner 实现
# ============================================================================

class SymbolicLearner:
    """Prompt 自优化器，基于失败轨迹的符号学习。

    Attributes:
        policy: VLLMPolicy 实例。
        max_versions: 保留的最大 prompt 版本数。
        rollback_threshold: 性能下降触发回滚的阈值（比例）。
        _prompt_versions: prompt 版本历史栈。
        _performance_history: 性能记录列表，用于回滚决策。
    """

    def __init__(
        self,
        policy,
        max_versions: int = 10,
        rollback_threshold: float = 0.05,
    ):
        self.policy = policy
        self.max_versions = max_versions
        self.rollback_threshold = rollback_threshold
        self._prompt_versions: list[dict[str, str]] = []
        self._performance_history: list[dict[str, float]] = []

    async def optimize_prompts(
        self,
        failed_trajectories: list[dict[str, Any]],
        current_prompts: dict[str, str],
    ) -> dict[str, str]:
        """从失败轨迹中提取错误模式并优化 prompt。

        执行流程：
        1. 将失败轨迹压缩为文本摘要。
        2. 调用 LLM 提取系统性错误模式。
        3. 调用 LLM 基于错误模式优化 prompt。
        4. 保存当前版本到历史栈。

        Args:
            failed_trajectories: 失败轨迹列表，每个元素为 collect() 输出格式。
            current_prompts: 当前使用的 prompt 字典，键为 prompt 名称。

        Returns:
            优化后的 prompt 字典。
        """
        if not failed_trajectories:
            return copy.deepcopy(current_prompts)

        # Step 1: 压缩失败轨迹
        traj_text = self._compress_trajectories(failed_trajectories)

        # Step 2: 提取错误模式
        patterns = await self._extract_patterns(traj_text)
        if not patterns:
            return copy.deepcopy(current_prompts)

        # Step 3: 优化 prompt
        new_prompts = await self._generate_optimized_prompts(patterns, current_prompts)

        # Step 4: 保存版本
        self._save_version(current_prompts)

        return new_prompts

    def rollback_if_needed(
        self,
        new_prompts: dict[str, str],
        performance: dict[str, float],
    ) -> dict[str, str]:
        """检查性能，若下降超过阈值则回滚到上一版本。

        Args:
            new_prompts: 新应用的 prompts。
            performance: 当前轮次性能指标，必须包含 "avg_score" 键。

        Returns:
            若回滚则返回上一版本 prompts，否则返回 new_prompts。
        """
        self._performance_history.append(performance)

        if len(self._performance_history) < 2:
            return new_prompts

        prev_perf = self._performance_history[-2]
        curr_perf = self._performance_history[-1]

        prev_score = prev_perf.get("avg_score", 0.0)
        curr_score = curr_perf.get("avg_score", 0.0)

        if prev_score > 0.0:
            drop = (prev_score - curr_score) / prev_score
            if drop > self.rollback_threshold:
                # 触发回滚
                rolled_back = self._rollback_one()
                if rolled_back is not None:
                    # 回退 performance_history
                    self._performance_history.pop()
                    return rolled_back

        return new_prompts

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _compress_trajectories(self, trajectories: list[dict[str, Any]]) -> str:
        """将失败轨迹压缩为文本摘要，控制 prompt 长度。"""
        parts = []
        for i, traj in enumerate(trajectories[:20]):  # 最多取 20 条
            query = traj.get("query", "")
            final_score = traj.get("final_score", 0.0)
            num_searches = traj.get("num_searches", 0)
            content_preview = traj.get("report_content", "")[:300]
            parts.append(
                f"[案例 {i+1}] query={query}, score={final_score}, searches={num_searches}\n"
                f"内容预览: {content_preview}\n"
            )
        return "\n".join(parts)

    async def _extract_patterns(self, traj_text: str) -> list[dict[str, str]]:
        """调用 LLM 提取系统性错误模式。"""
        prompt = PROMPT_EXTRACT_PATTERNS.format(trajectories=traj_text)
        messages = [
            {"role": "system", "content": SYSTEM_SYMBOLIC},
            {"role": "user", "content": prompt},
        ]
        try:
            resp = self.policy(messages)
            raw = resp.content or ""
            data = self._parse_json(raw)
            return data.get("patterns", [])
        except Exception:
            return []

    async def _generate_optimized_prompts(
        self,
        patterns: list[dict[str, str]],
        current_prompts: dict[str, str],
    ) -> dict[str, str]:
        """基于错误模式生成优化后的 prompts。"""
        patterns_text = json.dumps(patterns, ensure_ascii=False, indent=2)
        current_text = json.dumps(current_prompts, ensure_ascii=False, indent=2)
        prompt = PROMPT_OPTIMIZE_PROMPT.format(
            patterns=patterns_text,
            current_prompts=current_text,
        )
        messages = [
            {"role": "system", "content": SYSTEM_SYMBOLIC},
            {"role": "user", "content": prompt},
        ]
        try:
            resp = self.policy(messages)
            raw = resp.content or ""
            data = self._parse_json(raw)
            # 解析优化后的 prompts
            optimized = data.get("optimized_prompts", {})
            if isinstance(optimized, dict) and "new_content" in optimized:
                # 单 prompt 优化
                name = optimized.get("prompt_name", "system_prompt")
                result = copy.deepcopy(current_prompts)
                result[name] = optimized["new_content"]
                return result
            elif isinstance(optimized, list):
                # 多 prompt 优化
                result = copy.deepcopy(current_prompts)
                for item in optimized:
                    name = item.get("prompt_name", "system_prompt")
                    result[name] = item.get("new_content", result.get(name, ""))
                return result
            return copy.deepcopy(current_prompts)
        except Exception:
            return copy.deepcopy(current_prompts)

    def _save_version(self, prompts: dict[str, str]) -> None:
        """保存当前 prompt 版本到历史栈，超出限制时淘汰最旧版本。"""
        self._prompt_versions.append(copy.deepcopy(prompts))
        while len(self._prompt_versions) > self.max_versions:
            self._prompt_versions.pop(0)

    def _rollback_one(self) -> dict[str, str] | None:
        """回滚到上一版本。"""
        if len(self._prompt_versions) < 2:
            return None
        # 当前版本已经在栈顶（由 _save_version 保存）
        # 回滚 = 丢弃最新版本，返回倒数第二个
        self._prompt_versions.pop()
        return copy.deepcopy(self._prompt_versions[-1])

    def _parse_json(self, raw: str) -> dict[str, Any]:
        """鲁棒 JSON 解析。"""
        raw = raw.strip()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        import re

        code = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
        for m in code.findall(raw):
            try:
                return json.loads(m.strip())
            except json.JSONDecodeError:
                continue
        brace = re.search(r"\{.*\}", raw, re.DOTALL)
        if brace:
            try:
                return json.loads(brace.group(0))
            except json.JSONDecodeError:
                pass
        return {}

    def save_versions_to_disk(self, dir_path: str) -> None:
        """将版本历史持久化到磁盘。"""
        os.makedirs(dir_path, exist_ok=True)
        for i, version in enumerate(self._prompt_versions):
            path = os.path.join(dir_path, f"prompt_v{i:03d}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(version, f, ensure_ascii=False, indent=2)

    def load_versions_from_disk(self, dir_path: str) -> None:
        """从磁盘加载版本历史。"""
        if not os.path.isdir(dir_path):
            return
        files = sorted(
            [f for f in os.listdir(dir_path) if f.endswith(".json")],
            key=lambda x: int(x.split("_v")[1].split(".")[0]),
        )
        self._prompt_versions = []
        for fname in files:
            with open(os.path.join(dir_path, fname), "r", encoding="utf-8") as f:
                self._prompt_versions.append(json.load(f))
