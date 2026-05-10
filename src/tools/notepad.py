"""
笔记工具 (NotepadTool)

设计理由：
  深度研究是一个长时程、多轮次的过程。Agent 在 10+ 轮搜索后容易"遗忘"早期结论
  或陷入重复搜索。NotepadTool 提供一个持久化的"草稿纸"，让 Agent 可以：

  1. 记录中间结论（如"A 公司 2024 年营收 = 300 亿，来源：财报第 3 页"）
  2. 记录待验证假设（"需要确认 B 公司是否也推出了类似产品"）
  3. 记录搜索策略（"已搜过 X 和 Y，接下来搜 Z"）
  4. 在后续轮次中读取笔记，避免重复工作

与 Memory Store (M4) 的区别：
  - Memory Store：结构化、去重、矛盾检测，用于跨 Agent 共享信息
  - Notepad：非结构化、个人化、临时性，用于单个 Agent 的"思维草稿"

设计要点：
  - 纯内存实现（session 级），不持久化
  - 支持 CRUD：write / read / list / clear
  - 每条笔记带 timestamp 和 category（conclusion / todo / question / source）
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


__all__ = ["NotepadTool", "NotepadEntry"]


@dataclass
class NotepadEntry:
    """单条笔记。"""
    content: str
    category: str  # "conclusion" | "todo" | "question" | "source" | "strategy"
    timestamp: float = field(default_factory=time.time)
    source: str = ""  # 可选：这条笔记的来源/依据


class NotepadTool:
    """笔记工具：Agent 的草稿纸。"""

    name: str = "notepad"
    description: str = (
        "A personal notepad for the agent to record intermediate thoughts, conclusions, "
        "and todo items during long-horizon research. Use this to avoid forgetting key "
        "findings or repeating searches. "
        "Input: {'action': str, 'content': str(optional), 'category': str(optional), ...}. "
        "Actions: write, read, list_categories, clear, search."
    )

    def __init__(self) -> None:
        self._notes: list[NotepadEntry] = []

    def get_openai_tool_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": "Action to perform: write, read, list_categories, clear, search",
                            "enum": ["write", "read", "list_categories", "clear", "search"],
                        },
                        "content": {
                            "type": "string",
                            "description": "Content for write action",
                        },
                        "category": {
                            "type": "string",
                            "description": "Category for write/read/clear: conclusion, todo, question, source, strategy",
                        },
                        "source": {
                            "type": "string",
                            "description": "Optional source annotation for write action",
                        },
                        "keyword": {
                            "type": "string",
                            "description": "Search keyword for search action",
                        },
                        "max_entries": {
                            "type": "integer",
                            "description": "Maximum entries to return for read/search",
                            "default": 10,
                        },
                    },
                    "required": ["action"],
                },
            },
        }

    async def execute(self, action: str, **kwargs) -> str:
        """统一入口：根据 action 分发到具体方法。"""
        import asyncio
        await asyncio.sleep(0)

        if action == "write":
            return await self.write(**kwargs)
        if action == "read":
            return await self.read(**kwargs)
        if action == "list_categories":
            return await self.list_categories(**kwargs)
        if action == "clear":
            return await self.clear(**kwargs)
        if action == "search":
            return await self.search(**kwargs)
        return f"[Notepad Error] Unknown action: {action}. Supported: write, read, list_categories, clear, search."

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    async def write(self, content: str, category: str = "conclusion", source: str = "") -> str:
        import asyncio
        await asyncio.sleep(0)

        """写一条笔记。

        Args:
            content: 笔记内容。
            category: 笔记类型。建议值：
                      - "conclusion": 已确认的结论
                      - "todo": 待办事项（需要后续验证）
                      - "question": 未回答的疑问
                      - "source": 重要来源记录
                      - "strategy": 搜索策略/计划
            source: 可选的来源标注。

        Returns:
            确认信息。
        """
        entry = NotepadEntry(content=content, category=category, source=source)
        self._notes.append(entry)
        return f"[Notepad] Written ({category}): {content[:80]}{'...' if len(content) > 80 else ''}"

    async def read(self, category: str | None = None, max_entries: int = 10) -> str:
        import asyncio
        await asyncio.sleep(0)

        """读取笔记。

        Args:
            category: 只读取指定类型的笔记。为 None 时读取全部。
            max_entries: 最多返回多少条（最新的优先）。

        Returns:
            格式化的笔记列表。
        """
        notes = self._notes
        if category:
            notes = [n for n in notes if n.category == category]

        if not notes:
            cat_hint = f' in category "{category}"' if category else ""
            return f"[Notepad] No notes found{cat_hint}."

        # 最新的优先
        notes = sorted(notes, key=lambda n: n.timestamp, reverse=True)[:max_entries]

        lines = [f"=== Notepad ({len(notes)} entries) ==="]
        for i, n in enumerate(notes, 1):
            time_str = time.strftime("%H:%M:%S", time.localtime(n.timestamp))
            source_hint = f" [src: {n.source}]" if n.source else ""
            lines.append(f"{i}. [{n.category}] {time_str}{source_hint}\n   {n.content}")
        return "\n".join(lines)

    async def list_categories(self) -> str:
        import asyncio
        await asyncio.sleep(0)

        """列出所有笔记类型及其数量。"""
        from collections import Counter
        counts = Counter(n.category for n in self._notes)
        if not counts:
            return "[Notepad] No notes."
        lines = ["=== Notepad Categories ==="]
        for cat, cnt in counts.most_common():
            lines.append(f"  {cat}: {cnt}")
        return "\n".join(lines)

    async def clear(self, category: str | None = None) -> str:
        import asyncio
        await asyncio.sleep(0)

        """清空笔记。

        Args:
            category: 只清空指定类型。为 None 时清空全部。
        """
        if category is None:
            count = len(self._notes)
            self._notes.clear()
            return f"[Notepad] Cleared all {count} notes."

        before = len(self._notes)
        self._notes = [n for n in self._notes if n.category != category]
        removed = before - len(self._notes)
        return f"[Notepad] Cleared {removed} notes in category '{category}'."

    async def search(self, keyword: str, max_entries: int = 5) -> str:
        import asyncio
        await asyncio.sleep(0)

        """搜索笔记内容。

        Args:
            keyword: 搜索关键词。
            max_entries: 最多返回多少条。
        """
        matches = [n for n in self._notes if keyword.lower() in n.content.lower()]
        if not matches:
            return f"[Notepad] No notes matching '{keyword}'."

        matches = sorted(matches, key=lambda n: n.timestamp, reverse=True)[:max_entries]
        lines = [f"=== Notepad Search: '{keyword}' ({len(matches)} matches) ==="]
        for i, n in enumerate(matches, 1):
            time_str = time.strftime("%H:%M:%S", time.localtime(n.timestamp))
            lines.append(f"{i}. [{n.category}] {time_str}\n   {n.content}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 序列化（用于保存 trajectory）
    # ------------------------------------------------------------------

    def to_dict(self) -> list[dict]:
        """导出为字典列表。"""
        return [
            {
                "content": n.content,
                "category": n.category,
                "timestamp": n.timestamp,
                "source": n.source,
            }
            for n in self._notes
        ]
