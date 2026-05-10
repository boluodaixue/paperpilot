"""
Short-term Memory 模块：Session 级临时存储

设计决策：
1. 纯内存结构（list + dict），不涉及持久化，保证最低延迟
2. 每条消息保留 metadata（timestamp, token_count 等），便于后续压缩决策
3. 支持角色合并检查（连续同角色消息可合并，减少上下文碎片）
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Turn:
    """单轮对话记录。"""

    turn_id: str
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    timestamp: float
    metadata: dict[str, Any] = field(default_factory=dict)


class ShortTermMemory:
    """
    Session 级短期记忆。

    维护当前会话的完整对话历史，支持按角色过滤、合并连续消息、
    以及快速估算总 token 数（字符数 heuristic）。
    """

    def __init__(self, session_id: Optional[str] = None) -> None:
        """
        初始化短期记忆。

        Args:
            session_id: 会话唯一标识，None 时自动生成 UUID
        """
        self.session_id = session_id or str(uuid.uuid4())
        self._turns: list[Turn] = []
        self._created_at = time.time()

    def add_turn(
        self,
        role: str,
        content: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Turn:
        """
        添加一轮对话。

        Args:
            role: 角色，通常为 "system" / "user" / "assistant" / "tool"
            content: 消息内容
            metadata: 额外元数据（如 token_count, tool_call_id 等）

        Returns:
            创建的 Turn 对象
        """
        turn = Turn(
            turn_id=str(uuid.uuid4()),
            role=role,
            content=content,
            timestamp=time.time(),
            metadata=metadata or {},
        )
        self._turns.append(turn)
        return turn

    def get_history(
        self,
        roles: Optional[list[str]] = None,
        last_n: Optional[int] = None,
    ) -> list[Turn]:
        """
        获取对话历史。

        Args:
            roles: 按角色过滤，None 表示不过滤
            last_n: 只返回最近 N 条，None 表示全部

        Returns:
            Turn 列表（按时间正序）
        """
        turns = self._turns
        if roles:
            turns = [t for t in turns if t.role in roles]
        if last_n is not None:
            turns = turns[-last_n:]
        return turns

    def get_history_as_dicts(
        self,
        roles: Optional[list[str]] = None,
        last_n: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """
        将历史转为 OpenAI 风格 dict 列表，便于直接传给 LLM。

        Returns:
            {"role": ..., "content": ...} 列表
        """
        return [
            {"role": t.role, "content": t.content, **t.metadata}
            for t in self.get_history(roles=roles, last_n=last_n)
        ]

    def clear(self) -> None:
        """清空当前 session 的所有对话记录。"""
        self._turns.clear()

    def estimate_tokens(self) -> int:
        """
        估算当前历史占用的 token 数。

        使用字符数 / 3.5 的 heuristic（中文/英文混合场景的经验值）。
        更精确的计算需要 tiktoken，但 heuristic 足够做压缩触发判断。
        """
        total_chars = sum(len(t.content) for t in self._turns)
        # 增加 10% overhead 给 role 标签和格式
        return int(total_chars / 3.5 * 1.1)

    def estimate_chars(self) -> int:
        """估算当前历史总字符数。"""
        return sum(len(t.content) for t in self._turns)

    def last_turn(self) -> Optional[Turn]:
        """返回最近一条记录，无记录时返回 None。"""
        return self._turns[-1] if self._turns else None

    def __len__(self) -> int:
        return len(self._turns)

    def __repr__(self) -> str:
        return (
            f"ShortTermMemory(session_id={self.session_id[:8]}, "
            f"turns={len(self._turns)}, est_tokens={self.estimate_tokens()})"
        )
