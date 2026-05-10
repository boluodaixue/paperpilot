"""
Sliding Window Compressor 模块：FIFO 截断旧消息

设计决策：
1. 复用项目一 VLLMPolicy._truncate_messages 的核心思想，但提取为独立模块
2. 改进点：(a) 支持按 token 估算（而非纯字符），(b) 更精细的角色保留策略
3. 截断粒度为"整消息丢弃"，避免在消息中间切断导致语义破碎
4. 极端情况下对最后一条做内容级截断兜底，但保留至少 500 字符
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class SlidingWindowCompressor:
    """
    滑动窗口截断器。

    按 FIFO 原则丢弃旧消息，优先保留 system prompt 和最近交互，
    适用于会话历史超出上下文预算时的快速降级。
    """

    def __init__(
        self,
        max_tokens: int = 12000,
        char_per_token: float = 3.5,
        min_recent_turns: int = 3,
        min_last_msg_chars: int = 500,
    ) -> None:
        """
        初始化滑动窗口截断器。

        Args:
            max_tokens: 最大允许 token 数
            char_per_token: 字符/token 换算比（中文混合约 3.0-4.0）
            min_recent_turns: 至少保留的非 system 消息数
            min_last_msg_chars: 极端情况下最后一条消息至少保留的字符数
        """
        self.max_tokens = max_tokens
        self.char_per_token = char_per_token
        self.min_recent_turns = min_recent_turns
        self.min_last_msg_chars = min_last_msg_chars
        self._last_truncated = False
        self._last_stats: dict[str, Any] = {}

    def compress(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        执行滑动窗口截断。

        Args:
            messages: OpenAI 格式的消息列表，每条 dict 含 role/content

        Returns:
            截断后的消息列表
        """
        self._last_truncated = False
        max_chars = int(self.max_tokens * self.char_per_token)
        result = self._truncate_messages(messages, max_chars)
        return result

    def _truncate_messages(
        self,
        messages: list[dict[str, Any]],
        max_chars: int,
    ) -> list[dict[str, Any]]:
        """
        核心截断逻辑。

        步骤：
        1. 分离 system 消息与其他消息
        2. 若总字符数未超限，直接返回
        3. 从旧消息开始丢弃，直到字符数达标或只剩 min_recent_turns 条
        4. 极端情况下截断最后一条内容
        """
        system_msgs = [
            m for m in messages if isinstance(m, dict) and m.get("role") == "system"
        ]
        other_msgs = [
            m for m in messages if not (isinstance(m, dict) and m.get("role") == "system")
        ]

        before_chars = self._count_chars(messages)
        if before_chars <= max_chars:
            self._last_truncated = False
            self._last_stats = {
                "before_chars": before_chars,
                "after_chars": before_chars,
                "removed_turns": 0,
                "truncated": False,
            }
            return messages

        self._last_truncated = True
        logger.info(
            f"[SlidingWindow] Triggered: {before_chars} chars > {max_chars} threshold. "
            f"n_msgs={len(messages)}"
        )

        kept = list(other_msgs)
        removed_count = 0
        while len(kept) > self.min_recent_turns:
            removed = kept.pop(0)
            removed_count += 1
            after_chars = self._count_chars(system_msgs + kept)
            if after_chars <= max_chars:
                logger.info(
                    f"[SlidingWindow] Reduced to {after_chars} chars, "
                    f"kept {len(kept)} non-system msgs"
                )
                self._last_stats = {
                    "before_chars": before_chars,
                    "after_chars": after_chars,
                    "removed_turns": removed_count,
                    "truncated": True,
                }
                return system_msgs + kept

        # 极端情况：即使只保留 system + 最后 N 条也超限
        after_chars = self._count_chars(system_msgs + kept)
        if after_chars > max_chars and kept:
            last_msg = kept[-1]
            excess = after_chars - max_chars
            content = str(last_msg.get("content", ""))
            new_len = max(
                len(content) - excess - 100,
                self.min_last_msg_chars,
            )
            if new_len < len(content):
                last_msg["content"] = content[:new_len] + "\n[CONTENT_TRUNCATED]"
            final_chars = self._count_chars(system_msgs + kept)
            logger.info(
                f"[SlidingWindow] Content-truncated last msg to {new_len} chars. "
                f"Final: {final_chars}"
            )
            self._last_stats = {
                "before_chars": before_chars,
                "after_chars": final_chars,
                "removed_turns": removed_count,
                "truncated": True,
            }
            return system_msgs + kept

        self._last_stats = {
            "before_chars": before_chars,
            "after_chars": after_chars,
            "removed_turns": removed_count,
            "truncated": True,
        }
        return system_msgs + kept

    def _count_chars(self, messages: list[dict[str, Any]]) -> int:
        """计算消息列表的总字符数，包含 content/tool_calls/tool metadata。"""
        total = 0
        for m in messages:
            if not isinstance(m, dict):
                continue
            total += len(str(m.get("content", "")))
            if m.get("role") == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    func = tc.get("function", {})
                    total += len(str(func.get("arguments", "")))
                    total += len(str(func.get("name", "")))
            if m.get("role") == "tool":
                total += len(str(m.get("tool_call_id", "")))
                total += len(str(m.get("name", "")))
        return total

    def get_stats(self) -> dict[str, Any]:
        """返回最近一次压缩的统计信息。"""
        return dict(self._last_stats)

    def was_truncated(self) -> bool:
        """返回最近一次压缩是否发生了截断。"""
        return self._last_truncated
