"""Tools 子包：外部能力封装（搜索、论文阅读、代码执行、网页浏览、文件读取、计算、笔记等）。"""
from __future__ import annotations

from .web_search import WebSearchTool, MockWebSearchTool, BaseWebSearchTool
from .arxiv_reader import ArxivReaderTool
from .code_sandbox import CodeSandboxTool
from .browser import BrowserTool, MockBrowserTool, BaseBrowserTool, get_browser_tool
from .file_reader import FileReaderTool
from .calculator import CalculatorTool
from .notepad import NotepadTool, NotepadEntry

__all__ = [
    # 搜索与阅读
    "WebSearchTool",
    "MockWebSearchTool",
    "BaseWebSearchTool",
    "ArxivReaderTool",
    "BrowserTool",
    "MockBrowserTool",
    "BaseBrowserTool",
    "get_browser_tool",
    "FileReaderTool",
    # 计算与执行
    "CodeSandboxTool",
    "CalculatorTool",
    # 辅助
    "NotepadTool",
    "NotepadEntry",
]
