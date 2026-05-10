#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
evaluation/benchmarks/hotpotqa.py
================================================================================
HotpotQA 多跳问答评测适配器。

HotpotQA 是一个经典的 multi-hop QA 数据集，要求模型通过多步推理/检索
连接多条信息才能回答问题。本适配器将其转换为 DeepResearch Agent 的输入格式，
并计算 pass@k、exact match、F1 等评测指标。
================================================================================
"""

from __future__ import annotations

import json
import os
import random
import re
from collections import Counter
from typing import Any


class HotpotQABenchmark:
    """HotpotQA 评测集加载与评估器。"""

    # 内置小规模测试数据（当 HuggingFace 不可用时作为 fallback）
    _MOCK_DATA: list[dict[str, Any]] = [
        {
            "question": "《红楼梦》的作者曹雪芹生活在哪个朝代？",
            "answer": "清朝",
            "type": "bridge",
            "level": "easy",
            "context": [["红楼梦", ["《红楼梦》是中国古典小说巅峰之作。"]], ["曹雪芹", ["曹雪芹，清代小说家。"]]],
        },
        {
            "question": "2024 年诺贝尔物理学奖授予了哪位科学家，他以什么研究闻名？",
            "answer": "John Hopfield 和 Geoffrey Hinton，以神经网络和机器学习的基础性发现",
            "type": "bridge",
            "level": "medium",
            "context": [["诺贝尔物理学奖", ["2024 年诺贝尔物理学奖授予机器学习领域。"]], ["Hinton", ["Geoffrey Hinton 是深度学习先驱。"]]],
        },
        {
            "question": "OpenAI 的 GPT 系列模型和 Google 的 Gemini 模型分别由哪家公司开发？",
            "answer": "GPT 由 OpenAI 开发，Gemini 由 Google DeepMind 开发",
            "type": "comparison",
            "level": "easy",
            "context": [["OpenAI", ["OpenAI 是人工智能研究公司。"]], ["Google", ["Google DeepMind 开发了 Gemini 模型。"]]],
        },
        {
            "question": "NVIDIA 的 H100 芯片采用什么制程工艺，主要用于什么场景？",
            "answer": "4 纳米制程，主要用于 AI 训练和推理",
            "type": "bridge",
            "level": "medium",
            "context": [["NVIDIA", ["NVIDIA 是全球领先的 GPU 制造商。"]], ["H100", ["H100 采用台积电 4nm 工艺。"]]],
        },
        {
            "question": "Transformer 架构中的 Attention 机制是谁提出的，发表于哪一年？",
            "answer": "Vaswani 等人，2017 年",
            "type": "bridge",
            "level": "medium",
            "context": [["Transformer", ["Transformer 架构发表于 2017 年。"]], ["Attention", ["Attention Is All You Need 由 Google 团队发表。"]]],
        },
    ]

    def __init__(self, data_path: str | None = None, split: str = "validation", use_mock: bool = False) -> None:
        """
        初始化 HotpotQA 评测集。

        Args:
            data_path: HotpotQA 数据文件路径（JSON 格式）。
                       若为 None，则尝试从 HuggingFace datasets 加载。
            split: 数据划分，通常为 "train" / "validation" / "test"。
            use_mock: 若 True，使用内置测试数据（用于流程验证，无需下载）。
        """
        self.split = split
        self.data: list[dict[str, Any]] = []

        if use_mock:
            self.data = self._MOCK_DATA
            print(f"[HotpotQA] 使用内置 mock 数据: {len(self.data)} 条")
            return

        if data_path and os.path.exists(data_path):
            with open(data_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
                self.data = raw if isinstance(raw, list) else raw.get("data", [])
        else:
            # 尝试通过 datasets 库加载（带 timeout 控制）
            try:
                import os as _os
                # 设置 HuggingFace 下载 timeout
                _os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "30")
                from datasets import load_dataset

                ds = load_dataset("hotpot_qa", "distractor", split=split)
                self.data = list(ds)
                print(f"[HotpotQA] 从 HuggingFace 加载 {len(self.data)} 条数据")
            except Exception as e:
                print(f"[HotpotQA] 无法从 HuggingFace 加载: {e}")
                print(f"[HotpotQA] 建议: 1) 检查网络连接; 2) 或手动下载数据到本地并通过 data_path 传入;")
                print(f"[HotpotQA] 3) 或使用 --use_mock 参数启用内置测试数据")
                self.data = []

    # -----------------------------------------------------------------------
    # 数据格式转换
    # -----------------------------------------------------------------------
    def to_research_format(self, sample: dict[str, Any]) -> dict[str, Any]:
        """
        将 HotpotQA 单条样本转换为 ResearchReport 兼容的输入格式。

        Args:
            sample: HotpotQA 原始样本。

        Returns:
            包含 query、context、expected_answer 的字典。
        """
        question = sample.get("question", "")
        answer = sample.get("answer", "")

        # HotpotQA 中的上下文通常是 (title, sentences) 列表
        contexts = sample.get("context", [])
        context_text = ""
        if contexts and isinstance(contexts[0], (list, tuple)) and len(contexts[0]) == 2:
            # 标准格式: [(title, [sent1, sent2, ...]), ...]
            for title, sentences in contexts:
                context_text += f"\n## {title}\n" + " ".join(sentences)
        elif isinstance(contexts, str):
            context_text = contexts

        return {
            "query": question,
            "context": context_text.strip(),
            "expected_answer": answer,
            "type": sample.get("type", "bridge"),  # bridge / comparison
            "level": sample.get("level", "medium"),  # easy / medium / hard
        }

    def get_samples(self, n: int | None = None, shuffle: bool = False) -> list[dict[str, Any]]:
        """
        获取转换后的样本列表。

        Args:
            n: 返回前 n 条样本，None 表示全部。
            shuffle: 是否随机打乱顺序。

        Returns:
            转换后的样本列表。
        """
        samples = [self.to_research_format(s) for s in self.data]
        if shuffle:
            random.shuffle(samples)
        if n is not None:
            samples = samples[:n]
        return samples

    # -----------------------------------------------------------------------
    # 评测指标
    # -----------------------------------------------------------------------
    @staticmethod
    def normalize_answer(text: str) -> str:
        """对答案进行标准化：小写、去标点、去冠词。"""
        text = text.lower().strip()
        text = re.sub(r"\b(a|an|the)\b", " ", text)
        text = re.sub(r"[^\w\s]", "", text)
        text = " ".join(text.split())
        return text

    @staticmethod
    def exact_match(pred: str, gold: str) -> bool:
        """计算标准化后的精确匹配。"""
        return HotpotQABenchmark.normalize_answer(pred) == HotpotQABenchmark.normalize_answer(gold)

    @staticmethod
    def f1_score(pred: str, gold: str) -> float:
        """计算 token-level F1 分数。"""
        pred_tokens = HotpotQABenchmark.normalize_answer(pred).split()
        gold_tokens = HotpotQABenchmark.normalize_answer(gold).split()

        if not pred_tokens and not gold_tokens:
            return 1.0
        if not pred_tokens or not gold_tokens:
            return 0.0

        common = Counter(pred_tokens) & Counter(gold_tokens)
        num_same = sum(common.values())

        if num_same == 0:
            return 0.0

        precision = num_same / len(pred_tokens)
        recall = num_same / len(gold_tokens)
        return 2 * precision * recall / (precision + recall)

    @staticmethod
    def pass_at_k(preds: list[str], gold: str, k: int = 1) -> bool:
        """
        判断前 k 个预测中是否有正确答案（精确匹配）。

        Args:
            preds: 模型生成的 k 个候选答案列表。
            gold: 标准答案。
            k: 考虑的候选数。

        Returns:
            是否有候选命中。
        """
        for pred in preds[:k]:
            if HotpotQABenchmark.exact_match(pred, gold):
                return True
        return False

    # -----------------------------------------------------------------------
    # 深度研究评估：把 HotpotQA 当作研究 query，评估完整报告质量
    # -----------------------------------------------------------------------
    @staticmethod
    def gold_entity_coverage(report: str, gold_answer: str) -> float:
        """
        检查 gold answer 中的实体/关键词是否在研究报告中被覆盖。

        策略：
        1. 把 gold_answer 拆分成 token（去停用词）
        2. 检查每个 token 是否在 report 中出现
        3. 返回覆盖率
        """
        if not gold_answer or not report:
            return 0.0

        stopwords = {"a", "an", "the", "and", "or", "in", "on", "at", "to", "of", "for", "with", "is", "was", "are", "were", "be", "been", "by"}
        gold_tokens = [t for t in HotpotQABenchmark.normalize_answer(gold_answer).split() if t not in stopwords and len(t) > 1]
        if not gold_tokens:
            return 0.0

        report_lower = report.lower()
        covered = sum(1 for t in gold_tokens if t in report_lower)
        return covered / len(gold_tokens)

    @staticmethod
    def semantic_gold_coverage(report: str, gold_answer: str, threshold: float = 0.60) -> float:
        """
        用 embedding 语义相似度评估 gold answer 被报告覆盖的程度。

        把 gold_answer 和 report 分别编码，计算相似度。
        如果 gold answer 较短，直接和整个报告比；
        如果 gold answer 较长，分段比较取平均。
        """
        if not gold_answer or not report:
            return 0.0

        from src.memory.embedder import Embedder
        import numpy as np

        embedder = Embedder()
        gold_emb = np.array(embedder.encode(gold_answer))

        # 把报告拆成 chunk，分别和 gold_answer 比
        chunks = [s.strip() for s in re.split(r"[。！？\n]", report) if len(s.strip()) > 10]
        if not chunks:
            return 0.0

        try:
            chunk_embs = np.array(embedder._load_model().encode(chunks, normalize_embeddings=True))
        except Exception:
            chunk_embs = np.array([embedder.encode(c) for c in chunks])

        sims = chunk_embs.dot(gold_emb)
        # 返回超过阈值的 chunk 比例（衡量报告中有多少段落和答案语义相关）
        above_threshold = np.sum(sims > threshold)
        return float(above_threshold / len(chunks)) if len(chunks) > 0 else 0.0

    def evaluate_report(
        self,
        report: str,
        gold_answer: str,
    ) -> dict[str, float]:
        """
        对单篇研究报告进行深度评估（基于 HotpotQA 的 gold answer）。

        返回:
            - gold_entity_coverage: gold answer 实体覆盖率
            - semantic_gold_coverage: 语义覆盖度
            - report_length: 报告字数（效率参考）
        """
        return {
            "gold_entity_coverage": self.gold_entity_coverage(report, gold_answer),
            "semantic_gold_coverage": self.semantic_gold_coverage(report, gold_answer),
            "report_length": len(report),
        }

    # -----------------------------------------------------------------------
    # 批量评估
    # -----------------------------------------------------------------------
    def evaluate(
        self,
        predictions: list[dict[str, Any]],
        metrics: list[str] | None = None,
    ) -> dict[str, float]:
        """
        批量评估预测结果。

        Args:
            predictions: 每条包含 {"query_id": ..., "prediction": ..., "gold": ...} 的列表。
                         如果包含 "report" 字段，会同时计算深度研究指标。
            metrics: 需要计算的指标列表，默认 ["em", "f1", "pass@1"]。

        Returns:
            指标名称 -> 平均值的字典。
        """
        if metrics is None:
            metrics = ["em", "f1", "pass@1"]

        total = len(predictions)
        if total == 0:
            return {m: 0.0 for m in metrics}

        em_sum = 0.0
        f1_sum = 0.0
        pass1_sum = 0.0
        entity_cov_sum = 0.0
        sem_cov_sum = 0.0

        for item in predictions:
            pred = item.get("prediction", "")
            gold = item.get("gold", "")

            if "em" in metrics and HotpotQABenchmark.exact_match(pred, gold):
                em_sum += 1.0
            if "f1" in metrics:
                f1_sum += HotpotQABenchmark.f1_score(pred, gold)
            if "pass@1" in metrics:
                pass1_sum += 1.0 if HotpotQABenchmark.exact_match(pred, gold) else 0.0

            # 深度研究指标（如果提供了完整报告）
            report = item.get("report", "")
            if report:
                depth_metrics = self.evaluate_report(report, gold)
                entity_cov_sum += depth_metrics["gold_entity_coverage"]
                sem_cov_sum += depth_metrics["semantic_gold_coverage"]

        results: dict[str, float] = {}
        if "em" in metrics:
            results["exact_match"] = em_sum / total
        if "f1" in metrics:
            results["f1"] = f1_sum / total
        if "pass@1" in metrics:
            results["pass@1"] = pass1_sum / total
        if entity_cov_sum > 0:
            results["gold_entity_coverage"] = entity_cov_sum / total
        if sem_cov_sum > 0:
            results["semantic_gold_coverage"] = sem_cov_sum / total

        return results


# =============================================================================
# 简单自测
# =============================================================================
if __name__ == "__main__":
    bench = HotpotQABenchmark()
    print(f"加载样本数: {len(bench.data)}")

    # 模拟预测
    preds = [
        {"query_id": 0, "prediction": "Shanghai", "gold": "Shanghai"},
        {"query_id": 1, "prediction": "Beijing", "gold": "Shanghai"},
    ]
    print("评测结果:", bench.evaluate(preds))
