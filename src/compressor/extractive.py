"""
Extractive Compressor 模块：TextRank + query-biased 关键句提取

设计决策：
1. 简易 TextRank 实现：基于句子间 cosine similarity 构建全连接图，
   用幂迭代计算 PageRank 得分，无需外部依赖（如 networkx）
2. query-biased scoring：将 TextRank 得分与 query 相关性加权融合，
   确保提取的句子与当前查询高度相关
3. 保留数字、来源引用、关键结论：通过正则预标记含数字/URL/引用的句子，给予 bonus
4. 动态保留比例：基于剩余 budget 计算，预算越紧张保留比例越低
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

import numpy as np

from src.memory.embedder import Embedder

logger = logging.getLogger(__name__)

# 句子分隔模式：支持中英文句号、问号、感叹号、换行
_SENTENCE_PATTERN = re.compile(r'[^.!?。！？\n]+[.!?。！？\n]*')

# 含数字/URL/引用的正则（这些句子信息密度高，给予 bonus）
_HIGH_VALUE_PATTERN = re.compile(
    r"(\d+[\d,]*\.?\d*\s*%?|\d{4}-\d{2}-\d{2}|https?://|www\.|\[[\d\w]+\]|"
    r"according to|cited|reported|found that|结论|结果表明|数据显示)",
    re.IGNORECASE,
)


def _tokenize_sentences(text: str) -> list[str]:
    """将文本分句，过滤过短句子。"""
    raw = _SENTENCE_PATTERN.findall(text)
    sentences = [s.strip() for s in raw if len(s.strip()) > 8]
    return sentences


def _cosine_similarity_matrix(vectors: np.ndarray) -> np.ndarray:
    """
    计算向量矩阵的余弦相似度矩阵。

    Args:
        vectors: (n_sentences, dim) 的归一化向量矩阵

    Returns:
        (n_sentences, n_sentences) 的相似度矩阵
    """
    # 防御性归一化
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms < 1e-9] = 1.0
    normalized = vectors / norms
    return normalized.dot(normalized.T)


def _textrank_scores(sim_matrix: np.ndarray, damping: float = 0.85, max_iter: int = 30, tol: float = 1e-4) -> np.ndarray:
    """
    简易 PageRank 迭代计算 TextRank 得分。

    Args:
        sim_matrix: 句子相似度矩阵（已归一化权重）
        damping: 阻尼系数
        max_iter: 最大迭代次数
        tol: 收敛阈值

    Returns:
        每个句子的 TextRank 得分
    """
    n = sim_matrix.shape[0]
    if n == 0:
        return np.array([])

    # 将相似度矩阵转为转移概率矩阵（行归一化）
    # 只保留相似度 > 0.1 的边（稀疏化，减少噪音）
    adj = sim_matrix.copy()
    adj[adj < 0.1] = 0.0
    row_sums = adj.sum(axis=1, keepdims=True)
    row_sums[row_sums < 1e-9] = 1.0
    transition = adj / row_sums

    scores = np.ones(n) / n
    for _ in range(max_iter):
        new_scores = (1 - damping) / n + damping * transition.T.dot(scores)
        if np.linalg.norm(new_scores - scores) < tol:
            break
        scores = new_scores
    return scores


class ExtractiveCompressor:
    """
    提取式压缩器：TextRank + query-biased 关键句提取。
    """

    def __init__(self, embedder: Optional[Embedder] = None) -> None:
        """
        初始化提取式压缩器。

        Args:
            embedder: 向量化器，None 时自动创建
        """
        self.embedder = embedder or Embedder()

    def compress(
        self,
        text: str,
        query: str,
        target_ratio: float = 0.3,
    ) -> str:
        """
        对单段文本执行提取式压缩。

        Args:
            text: 原始文本
            query: 当前查询（用于相关性加权）
            target_ratio: 保留句子比例（0-1）

        Returns:
            压缩后的文本（按原文顺序拼接保留的句子）
        """
        sentences = _tokenize_sentences(text)
        if not sentences:
            return text
        if len(sentences) <= 3:
            # 太短不压缩
            return text

        top_sents = self.textrank_sentences(sentences, query, target_ratio)
        return " ".join(top_sents)

    def textrank_sentences(
        self,
        sentences: list[str],
        query: str,
        top_ratio: float = 0.3,
    ) -> list[str]:
        """
        TextRank + query-biased 提取关键句，返回按原文顺序排列的句子列表。

        Args:
            sentences: 分句结果
            query: 查询文本
            top_ratio: 保留比例

        Returns:
            保留的句子列表（按原文顺序）
        """
        n = len(sentences)
        if n == 0:
            return []

        # 1. 计算句子 embedding
        embeddings = self.embedder.encode_batch(sentences)
        emb_matrix = np.array(embeddings, dtype=np.float32)

        # 2. 计算 TextRank 得分
        sim_matrix = _cosine_similarity_matrix(emb_matrix)
        textrank = _textrank_scores(sim_matrix)

        # 3. 计算 query-biased 得分
        query_emb = np.array(self.embedder.encode(query), dtype=np.float32)
        q_norm = float(np.linalg.norm(query_emb))
        if q_norm > 1e-9:
            query_emb = query_emb / q_norm
        else:
            query_emb = query_emb

        norms = np.linalg.norm(emb_matrix, axis=1, keepdims=True)
        norms[norms < 1e-9] = 1.0
        normalized_emb = emb_matrix / norms
        query_sims = normalized_emb.dot(query_emb)

        # 4. 高价值句子 bonus
        value_bonus = np.array([
            1.2 if _HIGH_VALUE_PATTERN.search(s) else 1.0
            for s in sentences
        ], dtype=np.float32)

        # 5. 融合得分 = TextRank * query_sim * value_bonus
        combined = textrank * query_sims * value_bonus

        # 6. 选择 top_k 句子，但保持原文顺序
        k = max(1, int(n * top_ratio))
        top_indices = set(np.argsort(combined)[::-1][:k].tolist())
        result = [s for i, s in enumerate(sentences) if i in top_indices]
        return result

    def query_biased_score(
        self,
        sentence: str,
        query: str,
        embedding: Optional[list[float]] = None,
    ) -> float:
        """
        计算单句的 query-biased 得分。

        Args:
            sentence: 句子文本
            query: 查询文本
            embedding: 预计算的句子 embedding，None 时现场计算

        Returns:
            query 相关性得分（0-1 之间）
        """
        if embedding is None:
            embedding = self.embedder.encode(sentence)
        sent_vec = np.array(embedding, dtype=np.float32)
        q_vec = np.array(self.embedder.encode(query), dtype=np.float32)
        s_norm = float(np.linalg.norm(sent_vec))
        q_norm = float(np.linalg.norm(q_vec))
        if s_norm < 1e-9 or q_norm < 1e-9:
            return 0.0
        return float(np.dot(sent_vec, q_vec) / (s_norm * q_norm))

    def compress_documents(
        self,
        documents: list[str],
        query: str,
        top_ratio: float = 0.3,
    ) -> list[str]:
        """
        对多篇文档分别执行提取式压缩。

        Args:
            documents: 文档列表
            query: 查询文本
            top_ratio: 每篇文档保留比例

        Returns:
            压缩后的文档列表（与输入一一对应）
        """
        return [self.compress(doc, query, top_ratio) for doc in documents]

    def get_stats(self, original: str, compressed: str) -> dict[str, Any]:
        """
        返回压缩统计信息。

        Args:
            original: 原始文本
            compressed: 压缩后文本

        Returns:
            {"compression_ratio": ..., "original_chars": ..., "compressed_chars": ...}
        """
        orig_len = len(original)
        comp_len = len(compressed)
        ratio = comp_len / max(orig_len, 1)
        return {
            "compression_ratio": round(ratio, 3),
            "original_chars": orig_len,
            "compressed_chars": comp_len,
        }
