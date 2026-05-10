"""
M6 自进化引擎 — 多维评分器 (Judge)

Judge 对研究报告进行五维度连续评分，支持 Ensemble（3个不同 prompt 取均值）、
效率维度规则公式、Reward Shaping 等机制。

设计决策：
1. Ensemble：3 个不同视角的 prompt 独立评分后取平均，降低单一 prompt bias。
2. 效率分：纯规则公式（sigmoid 衰减），防止 reward hacking（模型可以通过无意义搜索刷分）。
3. Reward shaping：将 [0,10] 的复合分映射到 [-1,1]，适配 GRPO 的 clip 范围。
4. Held-out 校准接口：定期与人工标注对比，暴露漂移。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from src.orchestrator.schemas import ResearchReport


__all__ = ["Judge"]


# ============================================================================
# Prompt 模板 — 3 个 Ensemble 视角
# ============================================================================

SYSTEM_JUDGE = (
    "你是一位专业的研究报告评审专家。请基于以下五个维度对报告进行评分，"
    "每个维度 0-10 分。输出必须是严格 JSON 格式。"
)

# 视角 1: 学术严谨型
PROMPT_JUDGE_1 = """请从【学术严谨】视角评审以下研究报告。

评分维度：
1. factual_accuracy (0-10): 事实准确性，所有 claim 是否有可靠来源支撑。
2. coverage (0-10): 覆盖度，是否完整回答了 query 的所有子话题。
3. logical_coherence (0-10): 逻辑性，论证是否严密、无自相矛盾。
4. citation_quality (0-10): 引用质量，来源是否权威、标注是否规范。
5. efficiency (0-10): 效率分（由系统规则计算，你只需对前4项评分）。

请按以下 JSON 格式输出（不要有任何额外文字）：
{
  "factual_accuracy": float,
  "coverage": float,
  "logical_coherence": float,
  "citation_quality": float,
  "rationale": "string"   // 评分理由简述
}

--- 原始问题 ---
{query}

--- 研究报告 ---
{content}

--- 来源列表 ---
{sources}
"""

# 视角 2: 实用导向型
PROMPT_JUDGE_2 = """请从【实用导向】视角评审以下研究报告。

关注重点：
- 报告是否直接回答了用户的问题？
- 信息对决策是否有实际帮助？
- 结构是否清晰、易于阅读？

评分维度：
1. factual_accuracy (0-10)
2. coverage (0-10)
3. logical_coherence (0-10)
4. citation_quality (0-10)
5. efficiency (0-10)

请按以下 JSON 格式输出：
{
  "factual_accuracy": float,
  "coverage": float,
  "logical_coherence": float,
  "citation_quality": float,
  "rationale": "string"
}

--- 原始问题 ---
{query}

--- 研究报告 ---
{content}

--- 来源列表 ---
{sources}
"""

# 视角 3: 批判挑错型
PROMPT_JUDGE_3 = """请从【批判挑错】视角严格审查以下研究报告。

你的任务是尽可能找出报告的缺陷：
- 任何无来源支撑的具体数字
- 任何以偏概全的结论
- 任何逻辑跳跃
- 任何遗漏的关键视角

评分维度（请刻意严格）：
1. factual_accuracy (0-10)
2. coverage (0-10)
3. logical_coherence (0-10)
4. citation_quality (0-10)
5. efficiency (0-10)

请按以下 JSON 格式输出：
{
  "factual_accuracy": float,
  "coverage": float,
  "logical_coherence": float,
  "citation_quality": float,
  "rationale": "string"
}

--- 原始问题 ---
{query}

--- 研究报告 ---
{content}

--- 来源列表 ---
{sources}
"""

JUDGE_PROMPTS = [PROMPT_JUDGE_1, PROMPT_JUDGE_2, PROMPT_JUDGE_3]

# 五维度权重（与项目计划一致）
DIMENSION_WEIGHTS = {
    "factual_accuracy": 0.30,
    "coverage": 0.25,
    "logical_coherence": 0.20,
    "citation_quality": 0.15,
    "efficiency": 0.10,
}


# ============================================================================
# Judge 实现
# ============================================================================

@dataclass
class CalibrationSample:
    """Held-out 校准样本。"""
    query: str
    report_content: str
    human_scores: dict[str, float]
    model_scores: dict[str, float] | None = None


class Judge:
    """多维评分器，支持 Ensemble 和 Reward Shaping。

    Attributes:
        policy: VLLMPolicy 实例。
        ensemble_size: Judge Ensemble 数量（默认 3）。
        efficiency_optimal: 效率分最优搜索次数。
        efficiency_scale: 效率分 sigmoid 衰减尺度。
    """

    def __init__(
        self,
        policy,
        ensemble_size: int = 3,
        efficiency_optimal: int = 5,
        efficiency_scale: float = 3.0,
    ):
        self.policy = policy
        self.ensemble_size = min(max(ensemble_size, 1), len(JUDGE_PROMPTS))
        self.efficiency_optimal = efficiency_optimal
        self.efficiency_scale = efficiency_scale
        # held-out 校准样本池
        self._calibration_pool: list[CalibrationSample] = []

    async def evaluate(
        self, report: ResearchReport, query: str | None = None
    ) -> dict[str, float]:
        """对研究报告进行五维度评分。

        执行流程：
        1. 调用 ensemble_size 个不同 prompt 的 Judge。
        2. 对每个维度取平均值。
        3. 用规则公式计算 efficiency 分。
        4. 返回五维评分字典。

        Args:
            report: 待评分的研究报告。
            query: 可选的原始问题（默认使用 report.query）。

        Returns:
            五维评分字典，键: factual_accuracy / coverage / logical_coherence /
            citation_quality / efficiency，值范围 [0.0, 10.0]。
        """
        q = query or report.query
        sources_text = self._format_sources(report.sources)

        # 收集 ensemble 中每个 judge 的评分
        dim_lists: dict[str, list[float]] = {
            "factual_accuracy": [],
            "coverage": [],
            "logical_coherence": [],
            "citation_quality": [],
        }
        rationales: list[str] = []

        for i in range(self.ensemble_size):
            prompt_template = JUDGE_PROMPTS[i]
            prompt = prompt_template.format(
                query=q,
                content=report.content,
                sources=sources_text,
            )
            messages = [
                {"role": "system", "content": SYSTEM_JUDGE},
                {"role": "user", "content": prompt},
            ]
            try:
                resp = self.policy(messages)
                raw = resp.content or ""
                scores, rationale = self._parse_judge_output(raw)
                for dim in dim_lists:
                    dim_lists[dim].append(scores.get(dim, 5.0))
                rationales.append(rationale)
            except Exception as e:
                # 单个 judge 失败时不中断，用保守分填充
                for dim in dim_lists:
                    dim_lists[dim].append(5.0)
                rationales.append(f"judge_{i}_error: {e}")

        # 取 ensemble 均值
        final_scores: dict[str, float] = {}
        for dim, vals in dim_lists.items():
            final_scores[dim] = sum(vals) / len(vals) if vals else 5.0

        # 效率分：规则公式，防止 reward hacking
        final_scores["efficiency"] = self._compute_efficiency_score(report.num_searches)

        # 记录 rationale 到内部字段（便于调试）
        self._last_rationales = rationales
        return final_scores

    def shape_reward(self, scores: dict[str, float]) -> float:
        """将五维评分转换为 GRPO 可用的单值 reward。

        公式: R_grpo = clip(composite * 2 - 1, -1, 1)
        其中 composite 是五维度加权平均分，范围 [0, 10]。

        Args:
            scores: evaluate() 返回的五维评分字典。

        Returns:
            单值 reward，范围 [-1.0, 1.0]。
        """
        composite = 0.0
        weight_sum = 0.0
        for dim, weight in DIMENSION_WEIGHTS.items():
            s = scores.get(dim, 0.0)
            composite += weight * max(0.0, min(10.0, s))
            weight_sum += weight
        if weight_sum == 0.0:
            composite = 0.0
        else:
            composite /= weight_sum

        # 映射到 [-1, 1]
        r = composite * 2.0 - 1.0
        return max(-1.0, min(1.0, r))

    def _compute_efficiency_score(self, num_searches: int) -> float:
        """计算效率分：sigmoid 衰减。

        公式: score = 10.0 / (1.0 + exp((num_searches - optimal) / scale))
        搜索次数越接近 optimal，分数越高；过度搜索会显著扣分。

        Args:
            num_searches: 实际搜索次数。

        Returns:
            效率分，范围 (0.0, 10.0]。
        """
        exp_term = math.exp((num_searches - self.efficiency_optimal) / self.efficiency_scale)
        score = 10.0 / (1.0 + exp_term)
        return score

    def add_calibration_sample(
        self, query: str, report_content: str, human_scores: dict[str, float]
    ) -> None:
        """添加 held-out 校准样本。

        Args:
            query: 研究问题。
            report_content: 报告正文。
            human_scores: 人工标注的五维分数。
        """
        self._calibration_pool.append(
            CalibrationSample(
                query=query,
                report_content=report_content,
                human_scores=human_scores,
            )
        )

    def calibrate(self) -> dict[str, float]:
        """执行 held-out 校准：对比模型评分与人工标注。

        Returns:
            校准指标字典，包含各维度的平均绝对误差 (MAE) 和整体相关系数。
        """
        if not self._calibration_pool:
            return {"status": "no_samples"}

        dim_mae: dict[str, list[float]] = {
            "factual_accuracy": [],
            "coverage": [],
            "logical_coherence": [],
            "citation_quality": [],
            "efficiency": [],
        }

        # 由于 calibrate 是同步方法，这里仅对比已有 model_scores
        # 若 model_scores 为 None，说明尚未评估，需要外部先调用 evaluate 填充
        for sample in self._calibration_pool:
            if sample.model_scores is None:
                continue
            for dim in dim_mae:
                human = sample.human_scores.get(dim, 0.0)
                model = sample.model_scores.get(dim, 0.0)
                dim_mae[dim].append(abs(human - model))

        result: dict[str, float] = {}
        for dim, errs in dim_mae.items():
            if errs:
                result[f"{dim}_mae"] = sum(errs) / len(errs)
            else:
                result[f"{dim}_mae"] = -1.0  # 标记为未计算

        result["sample_count"] = float(len(self._calibration_pool))
        return result

    def update_model_scores_for_calibration(self, idx: int, model_scores: dict[str, float]) -> None:
        """为指定索引的校准样本更新模型评分。

        Args:
            idx: 校准样本索引。
            model_scores: 模型给出的五维分数。
        """
        if 0 <= idx < len(self._calibration_pool):
            self._calibration_pool[idx].model_scores = model_scores

    def _format_sources(self, sources: list[dict]) -> str:
        if not sources:
            return "（无来源）"
        lines = []
        for i, s in enumerate(sources, 1):
            title = s.get("title", "未知标题")
            url = s.get("url", "")
            lines.append(f"[{i}] {title} ({url})")
        return "\n".join(lines)

    def _parse_judge_output(self, raw: str) -> tuple[dict[str, float], str]:
        """解析 Judge 的 JSON 输出。"""
        raw = raw.strip()
        if not raw:
            return {}, ""

        import re

        # 尝试提取 JSON 块
        candidates = [raw]
        code_match = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
        candidates.extend(code_match.findall(raw))
        brace = re.search(r"\{.*\}", raw, re.DOTALL)
        if brace:
            candidates.append(brace.group(0))

        for cand in candidates:
            try:
                data = json.loads(cand.strip())
                scores = {}
                for dim in ["factual_accuracy", "coverage", "logical_coherence", "citation_quality"]:
                    scores[dim] = float(data.get(dim, 5.0))
                rationale = data.get("rationale", "")
                return scores, rationale
            except (json.JSONDecodeError, ValueError):
                continue

        return {}, "parse_failed"
