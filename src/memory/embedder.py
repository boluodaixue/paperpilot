"""
Embedder 模块：文本向量化封装

设计决策：
1. 主模型使用 all-MiniLM-L6-v2（轻量、384维、效果足够）
2. 提供 graceful fallback：当 sentence-transformers 未安装时，
   返回 deterministic random embedding（基于文本hash），确保测试可复现
3. 单例模型加载 + lazy init，避免重复初始化开销
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
from typing import Optional

import numpy as np

# 国内环境自动使用 HuggingFace 镜像（hf-mirror.com）
if os.environ.get("HF_ENDPOINT", "").strip() == "":
    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

logger = logging.getLogger(__name__)

# 尝试导入 sentence-transformers，未安装时标记
try:
    from sentence_transformers import SentenceTransformer
    _SENTENCE_TRANSFORMERS_AVAILABLE = True
except ImportError:
    _SENTENCE_TRANSFORMERS_AVAILABLE = False
    logger.warning(
        "sentence-transformers not installed. Embedder will use deterministic random fallback."
    )


class Embedder:
    """文本向量化器，封装 sentence-transformers 并提供 fallback。"""

    # 类级别缓存：避免重复加载模型
    _model_instance: Optional[object] = None
    _model_name: str = "sentence-transformers/all-MiniLM-L6-v2"
    _embedding_dim: int = 384  # all-MiniLM-L6-v2 输出维度

    def __init__(self, model_name: Optional[str] = None) -> None:
        """
        初始化 Embedder。

        Args:
            model_name: 指定 sentence-transformers 模型名，None 使用默认 all-MiniLM-L6-v2
        """
        self.model_name = model_name or self._model_name
        self._model: Optional[object] = None
        self._available = _SENTENCE_TRANSFORMERS_AVAILABLE

    def _load_model(self) -> object:
        """懒加载模型，返回 SentenceTransformer 实例或 None（fallback 模式）。"""
        if not self._available:
            return None
        if self._model is not None:
            return self._model
        # 尝试加载类缓存
        if Embedder._model_instance is not None:
            self._model = Embedder._model_instance
            return self._model
        try:
            Embedder._model_instance = SentenceTransformer(self.model_name)
            self._model = Embedder._model_instance
            logger.info(f"Loaded embedding model: {self.model_name}")
        except Exception as e:
            logger.error(f"Failed to load embedding model: {e}")
            self._available = False
            self._model = None
        return self._model

    def encode(self, text: str) -> list[float]:
        """
        将文本转为 embedding 向量。

        Args:
            text: 输入文本

        Returns:
            浮点列表，长度 384（主模型）或 fallback 维度
        """
        if not text or not text.strip():
            # 空文本返回零向量
            return [0.0] * self._embedding_dim

        model = self._load_model()
        if model is not None:
            try:
                embedding = model.encode(text, normalize_embeddings=True)
                return embedding.tolist()
            except Exception as e:
                logger.warning(f"Model encode failed, fallback to random: {e}")

        # Fallback: deterministic random embedding（基于文本 hash）
        return self._fallback_embedding(text)

    def _fallback_embedding(self, text: str) -> list[float]:
        """
        确定性随机 embedding fallback。

        使用文本 MD5 hash 作为随机种子，确保相同文本始终产生相同向量，
        便于测试和去重逻辑的一致性验证。
        """
        seed = int(hashlib.md5(text.encode("utf-8")).hexdigest(), 16) % (2**31)
        rng = random.Random(seed)
        vec = [rng.gauss(0.0, 1.0) for _ in range(self._embedding_dim)]
        # L2 归一化
        norm = float(np.linalg.norm(vec))
        if norm > 1e-9:
            vec = [v / norm for v in vec]
        return vec

    def encode_batch(self, texts: list[str]) -> list[list[float]]:
        """
        批量编码，比多次单条 encode 更高效。

        Args:
            texts: 文本列表

        Returns:
            embedding 列表
        """
        if not texts:
            return []
        model = self._load_model()
        if model is not None:
            try:
                embeddings = model.encode(texts, normalize_embeddings=True)
                return [e.tolist() for e in embeddings]
            except Exception as e:
                logger.warning(f"Batch encode failed, fallback to loop: {e}")
        return [self.encode(t) for t in texts]

    @property
    def dim(self) -> int:
        """返回 embedding 维度。"""
        return self._embedding_dim

    @property
    def is_available(self) -> bool:
        """返回是否使用真实模型（False 表示处于 fallback 模式）。"""
        _ = self._load_model()
        return self._available and self._model is not None
