"""
LLM Summarizer 模块：层级摘要

设计决策：
1. 两级摘要：逐文档摘要 → 聚合摘要，避免一次性输入过多内容导致摘要质量下降
2. Prompt 模板严格要求保留数字、来源引用、不确定性表述（如"约""可能""据报道"）
3. 单文档摘要控制 max_length，聚合摘要允许更长以保留跨文档关联
4. 复用项目一 VLLMPolicy 作为 LLM 后端，保持接口一致性
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 单文档摘要 Prompt
_DOCUMENT_SUMMARY_PROMPT = """请对以下文档进行摘要，要求：
1. 保留所有关键数字、日期、统计数据
2. 保留来源引用和作者信息
3. 保留不确定性表述（如"约""可能""据报道""初步结果显示"）
4. 摘要长度不超过 {max_length} 字
5. 使用中文输出

文档内容：
{doc}

摘要："""

# 聚合摘要 Prompt
_AGGREGATE_SUMMARY_PROMPT = """请将以下多篇文档摘要整合为一份连贯的综述，要求：
1. 合并重复信息，保留不同文档间的互补内容
2. 保留所有关键数字、日期、统计数据
3. 保留来源引用
4. 保留不确定性表述
5. 若文档间存在矛盾，请分别列出不同观点并标注来源
6. 总长度不超过 {max_length} 字
7. 使用中文输出

用户查询背景：{query}

文档摘要列表：
{docs}

综述："""


class LLMSummarizer:
    """
    LLM 层级摘要器。

    调用 VLLMPolicy 执行单文档摘要和聚合摘要，
    Prompt 经过专门设计以保留高信息密度内容。
    """

    def __init__(self, llm_policy: Any) -> None:
        """
        初始化 LLM 摘要器。

        Args:
            llm_policy: VLLMPolicy 实例或任何实现了 __call__(messages) 接口的对象
        """
        self.llm_policy = llm_policy

    def summarize_document(
        self,
        doc: str,
        query: str = "",
        max_length: int = 500,
    ) -> str:
        """
        单文档摘要。

        Args:
            doc: 原始文档文本
            query: 当前查询（可选，用于上下文理解）
            max_length: 摘要最大字数

        Returns:
            摘要文本
        """
        if not doc or len(doc) < max_length:
            # 文档已经很短，直接返回（但做简单清理）
            return doc.strip()

        prompt = _DOCUMENT_SUMMARY_PROMPT.format(doc=doc, max_length=max_length)
        if query:
            prompt = f"用户查询：{query}\n\n" + prompt

        try:
            resp = self.llm_policy([{"role": "user", "content": prompt}])
            summary = str(resp.content or "").strip()
            if not summary:
                logger.warning("LLM returned empty summary, returning truncated original.")
                return doc[:max_length] + "\n[TRUNCATED]"
            return summary
        except Exception as e:
            logger.error(f"LLM summarize_document failed: {e}")
            # Fallback: 直接截断原文
            return doc[:max_length] + "\n[TRUNCATED]"

    def summarize_documents(
        self,
        docs: list[str],
        query: str = "",
        max_length: int = 800,
    ) -> str:
        """
        多篇文档聚合摘要。

        流程：
        1. 先对每篇文档做单文档摘要（如果文档较长）
        2. 将所有单文档摘要聚合为最终综述

        Args:
            docs: 文档列表
            query: 当前查询
            max_length: 最终综述最大字数

        Returns:
            聚合摘要文本
        """
        if not docs:
            return ""
        if len(docs) == 1:
            return self.summarize_document(docs[0], query, max_length)

        # 第一阶段：逐文档摘要
        single_summaries: list[str] = []
        for i, doc in enumerate(docs, 1):
            # 单文档摘要控制在较小长度，避免聚合时输入爆炸
            single_max = max(200, max_length // len(docs))
            summary = self.summarize_document(doc, query, max_length=single_max)
            single_summaries.append(f"[文档{i}]\n{summary}")

        # 第二阶段：聚合摘要
        combined_text = "\n\n".join(single_summaries)
        prompt = _AGGREGATE_SUMMARY_PROMPT.format(
            query=query,
            docs=combined_text,
            max_length=max_length,
        )

        try:
            resp = self.llm_policy([{"role": "user", "content": prompt}])
            aggregate = str(resp.content or "").strip()
            if not aggregate:
                logger.warning("LLM returned empty aggregate summary, concatenating singles.")
                return "\n\n".join(single_summaries)
            return aggregate
        except Exception as e:
            logger.error(f"LLM summarize_documents failed: {e}")
            return "\n\n".join(single_summaries)

    def get_stats(self, original_docs: list[str], summary: str) -> dict[str, Any]:
        """
        返回摘要统计信息。

        Args:
            original_docs: 原始文档列表
            summary: 摘要结果

        Returns:
            统计 dict
        """
        total_orig = sum(len(d) for d in original_docs)
        comp_len = len(summary)
        ratio = comp_len / max(total_orig, 1)
        return {
            "compression_ratio": round(ratio, 3),
            "original_chars": total_orig,
            "summary_chars": comp_len,
            "n_documents": len(original_docs),
        }
