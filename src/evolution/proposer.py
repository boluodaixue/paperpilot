"""
M6 自进化引擎 — 研究问题生成器 (Proposer)

Proposer 生成三级难度（L1/L2/L3）的研究问题，支持自适应难度校准、
质量过滤和多样性约束。

设计决策：
1. 三级难度对应不同搜索深度，确保训练数据覆盖简单到复杂的全谱系。
2. 倒U型自适应权重：成功率接近 50% 时权重最高，避免数据集过于简单或困难。
3. embedding 相似度过滤：新生成的问题与已有问题相似度 < 0.7，保证多样性。
4. 成功率过滤：太简单 (>80%) 或太难 (<20%) 的问题被淘汰，维持合理学习梯度。
"""
from __future__ import annotations

import json
import random
from typing import Any


__all__ = ["Proposer"]


# ============================================================================
# Prompt 模板
# ============================================================================

SYSTEM_PROPOSER = (
    "你是一位研究问题设计专家。请根据要求生成高质量、有挑战性且可验证的研究问题。"
    "输出必须是 JSON 格式。"
)

PROMPT_GENERATE = """请生成 {n} 个难度为 {difficulty} 的研究问题。

难度定义：
- L1（事实查询）：1-2次搜索即可回答，答案明确且可验证。
- L2（多步推理）：3-6次搜索，需要整合多来源信息、进行因果或比较分析。
- L3（跨领域综合）：5-10+次搜索，需要跨学科知识、长程推理、处理冲突信息。

要求：
1. 每个问题必须有明确的客观答案或评判标准。
2. 避免过于宽泛（如"谈谈人工智能"）或过于狭窄（如"某具体人的生日"）。
3. 涉及最新事件时，确保事件发生在 2023 年之前（保证可验证性）。
4. 问题之间不得重复或高度相似。

领域偏好（可选）：{domains}

请按以下 JSON 格式输出：
{
  "questions": [
    {
      "question": "string",
      "difficulty": "L1|L2|L3",
      "expected_searches": int,
      "domain": "string",
      "verification_hint": "string"   // 如何验证答案正确性
    }
  ]
}
"""


# ============================================================================
# Proposer 实现
# ============================================================================

class Proposer:
    """研究问题生成器，支持三级难度和自适应校准。

    Attributes:
        policy: VLLMPolicy 实例。
        difficulty_history: 记录每个问题的难度、成功率和平均得分。
        _embedding_cache: 已生成问题的 embedding 缓存，用于多样性检查。
    """

    def __init__(
        self,
        policy,
        difficulty_history: dict[str, dict[str, Any]] | None = None,
    ):
        self.policy = policy
        # history 结构: {question_str: {"difficulty": "L1", "success": bool, "score": float, "attempts": int}}
        self.difficulty_history = difficulty_history or {}
        self._embedding_cache: list[list[float]] = []

    async def generate_batch(
        self,
        n: int = 32,
        domains: list[str] | None = None,
    ) -> list[str]:
        """生成一批研究问题。

        执行流程：
        1. 根据自适应权重决定 L1/L2/L3 的生成比例。
        2. 按批次调用 LLM 生成问题。
        3. 过滤太简单/太难的问题（基于历史）。
        4. 用 embedding 相似度去重，确保多样性。

        Args:
            n: 目标生成数量。
            domains: 可选的领域偏好列表。

        Returns:
            研究问题字符串列表。
        """
        weights = self.get_difficulty_weights()
        # 根据权重分配各难度生成数量
        total_weight = sum(weights.values())
        if total_weight == 0.0:
            counts = {"L1": n // 3, "L2": n // 3, "L3": n - 2 * (n // 3)}
        else:
            counts = {}
            remaining = n
            for lvl in ["L1", "L2"]:
                cnt = int(n * weights.get(lvl, 0.0) / total_weight)
                counts[lvl] = cnt
                remaining -= cnt
            counts["L3"] = remaining

        domains_text = ", ".join(domains) if domains else "不限"
        results: list[str] = []

        for difficulty, count in counts.items():
            if count <= 0:
                continue
            # 每次最多生成 8 个，避免 prompt 过长
            batch_size = 8
            generated = 0
            while generated < count:
                req = min(batch_size, count - generated)
                prompt = PROMPT_GENERATE.format(
                    n=req,
                    difficulty=difficulty,
                    domains=domains_text,
                )
                messages = [
                    {"role": "system", "content": SYSTEM_PROPOSER},
                    {"role": "user", "content": prompt},
                ]
                try:
                    resp = self.policy(messages)
                    raw = resp.content or ""
                    data = self._parse_json(raw)
                    for item in data.get("questions", []):
                        q = item.get("question", "").strip()
                        if not q:
                            continue
                        # 历史过滤：太简单或太难的问题跳过
                        hist = self.difficulty_history.get(q)
                        if hist and hist.get("attempts", 0) >= 3:
                            success_rate = hist.get("success_rate", 0.5)
                            if success_rate > 0.8 or success_rate < 0.2:
                                continue
                        # 多样性过滤
                        if not self._is_diverse(q):
                            continue
                        results.append(q)
                        generated += 1
                        if generated >= count:
                            break
                except Exception:
                    # 生成失败时填充简单占位问题，避免批次为空
                    fallback = self._fallback_question(difficulty, domains)
                    results.append(fallback)
                    generated += 1

        return results[:n]

    def update_history(
        self, question: str, success: bool, score: float
    ) -> None:
        """更新问题的历史记录，用于自适应校准。

        Args:
            question: 研究问题文本。
            success: 是否成功（score >= 6.0 视为成功）。
            score: 最终评分。
        """
        if question not in self.difficulty_history:
            self.difficulty_history[question] = {
                "attempts": 0,
                "successes": 0,
                "total_score": 0.0,
                "success_rate": 0.5,
            }
        h = self.difficulty_history[question]
        h["attempts"] += 1
        if success:
            h["successes"] += 1
        h["total_score"] += score
        h["success_rate"] = h["successes"] / h["attempts"]
        h["avg_score"] = h["total_score"] / h["attempts"]

    def get_difficulty_weights(self) -> dict[str, float]:
        """计算三级难度的自适应权重。

        倒U型公式: weight = 1 - 4 * (success_rate - 0.5) ^ 2
        成功率越接近 50%，权重越高；过于简单或困难的问题权重降低。

        Returns:
            难度权重字典，键: L1/L2/L3。
        """
        weights: dict[str, float] = {"L1": 1.0, "L2": 1.0, "L3": 1.0}
        for question, hist in self.difficulty_history.items():
            sr = hist.get("success_rate", 0.5)
            # 估算难度级别（简单启发：搜索次数映射）
            # 这里假设历史中没有显式难度，统一计算
            w = 1.0 - 4.0 * (sr - 0.5) ** 2
            w = max(0.1, min(1.0, w))
            # 由于历史不区分难度，这里将权重影响平摊
            for lvl in weights:
                weights[lvl] += w

        # 归一化
        total = sum(weights.values())
        if total > 0.0:
            weights = {k: v / total for k, v in weights.items()}
        return weights

    def _is_diverse(self, question: str, threshold: float = 0.7) -> bool:
        """检查新问题是否与已有问题足够多样（embedding 余弦相似度 < threshold）。"""
        if not self._embedding_cache:
            return True
        try:
            from memory.embedder import Embedder

            embedder = Embedder()
            emb = embedder.encode(question)
            for cached_emb in self._embedding_cache:
                sim = self._cosine_similarity(emb, cached_emb)
                if sim > threshold:
                    return False
            self._embedding_cache.append(emb)
            return True
        except Exception:
            # embedding 失败时默认接受
            return True

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        import math

        if len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return dot / (norm_a * norm_b)

    def _fallback_question(self, difficulty: str, domains: list[str] | None) -> str:
        """生成兜底问题，避免批次为空。"""
        domain = random.choice(domains) if domains else "科技"
        templates = {
            "L1": [
                f"{domain}领域在2022年的市场规模是多少？",
                f"{domain}的主要应用领域有哪些？",
            ],
            "L2": [
                f"对比分析{domain}领域2020-2022年的技术演进与商业落地情况。",
                f"{domain}的发展对就业市场产生了哪些多维度影响？",
            ],
            "L3": [
                f"从经济、伦理、技术三个维度综合评估{domain}的未来十年发展趋势。",
                f"跨学科视角下，{domain}与生物医药、气候科学的交叉创新有哪些关键突破？",
            ],
        }
        return random.choice(templates.get(difficulty, templates["L2"]))

    def _parse_json(self, raw: str) -> dict[str, Any]:
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
