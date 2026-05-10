"""
文件阅读器 (FileReaderTool)

设计理由：
  深度研究经常需要处理用户上传的文档（PDF 报告、CSV 数据集、Markdown 笔记）。
  FileReaderTool 负责读取本地文件并转换为 LLM 可消费的文本格式。

支持格式：
  - .txt, .md, .markdown → 直接读取
  - .pdf → 提取文本（PyPDF2 / pdfplumber 降级）
  - .csv, .json → 读取并格式化摘要
  - .docx → python-docx 提取（可选依赖）

安全设计：
  - 只允许读取指定目录下的文件（sandbox 模式）
  - 文件大小上限（默认 10MB）
  - 不执行文件中的任何代码
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


__all__ = ["FileReaderTool"]

# 默认允许的文件扩展名
_SUPPORTED_EXTS = {".txt", ".md", ".markdown", ".pdf", ".csv", ".json", ".docx"}
# 默认文件大小上限（字节）
_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB


class FileReaderTool:
    """文件阅读器：读取本地文件并返回结构化文本。"""

    name: str = "file_reader"
    description: str = (
        "Read a local file and return its content as text. "
        "Supports: .txt, .md, .pdf, .csv, .json, .docx. "
        "Use this when the user references an uploaded document or dataset. "
        "Input: {'file_path': str}. Output: file content as formatted text."
    )

    # sentinel 对象：区分"未传入参数"和"显式传入 None"
    _UNSET = object()

    def __init__(
        self,
        allowed_base_dir: str | None = _UNSET,
        max_file_size: int | None = _UNSET,
    ) -> None:
        """
        Args:
            allowed_base_dir: 允许读取的根目录。
                              为 None 时不限制（生产环境强烈建议设置）。
                              不传入时从 .env 读取 FILE_READER_ALLOWED_BASE_DIR。
            max_file_size: 最大文件大小（字节），超出则拒绝。
                           不传入时从 .env 读取 FILE_READER_MAX_FILE_SIZE。
        """
        from ..utils.env_config import get_env, get_env_int

        if allowed_base_dir is not FileReaderTool._UNSET:
            self.allowed_base_dir = Path(allowed_base_dir).resolve() if allowed_base_dir else None
        else:
            env_dir = get_env("FILE_READER_ALLOWED_BASE_DIR")
            self.allowed_base_dir = Path(env_dir).resolve() if env_dir else None

        if max_file_size is not FileReaderTool._UNSET:
            self.max_file_size = max_file_size if max_file_size is not None else _MAX_FILE_SIZE
        else:
            self.max_file_size = get_env_int("FILE_READER_MAX_FILE_SIZE", _MAX_FILE_SIZE)

    def get_openai_tool_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Absolute or relative path to the file to read",
                        },
                    },
                    "required": ["file_path"],
                },
            },
        }

    async def execute(self, file_path: str) -> str:
        """读取文件并返回内容。

        Args:
            file_path: 文件路径（绝对路径或相对路径）。

        Returns:
            文件内容的文本表示。
        """
        # 模拟异步 IO（实际文件读取是 IO-bound，但同步操作也足够快）
        import asyncio
        await asyncio.sleep(0)

        try:
            path = Path(file_path).resolve()
        except Exception as e:
            return f"[FileReader Error] Invalid path: {e}"

        # 安全检查 1：目录限制
        if self.allowed_base_dir and not str(path).startswith(str(self.allowed_base_dir)):
            return (
                f"[FileReader Error] Access denied: {path} is outside the allowed directory "
                f"{self.allowed_base_dir}."
            )

        # 安全检查 2：文件存在性
        if not path.exists():
            return f"[FileReader Error] File not found: {path}"
        if not path.is_file():
            return f"[FileReader Error] Not a file: {path}"

        # 安全检查 3：扩展名
        ext = path.suffix.lower()
        if ext not in _SUPPORTED_EXTS:
            return (
                f"[FileReader Error] Unsupported file type: {ext}. "
                f"Supported: {', '.join(sorted(_SUPPORTED_EXTS))}"
            )

        # 安全检查 4：文件大小
        size = path.stat().st_size
        if size > self.max_file_size:
            return (
                f"[FileReader Error] File too large: {size} bytes "
                f"(max allowed: {self.max_file_size} bytes)."
            )

        # 读取文件
        try:
            return self._read_by_ext(path, ext)
        except Exception as e:
            return f"[FileReader Error] Failed to read {path}: {type(e).__name__}: {e}"

    def _read_by_ext(self, path: Path, ext: str) -> str:
        """根据扩展名选择读取策略。"""
        if ext in (".txt", ".md", ".markdown"):
            return self._read_text(path)
        if ext == ".pdf":
            return self._read_pdf(path)
        if ext == ".csv":
            return self._read_csv(path)
        if ext == ".json":
            return self._read_json(path)
        if ext == ".docx":
            return self._read_docx(path)
        return f"[FileReader Error] No reader implemented for {ext}"

    @staticmethod
    def _read_text(path: Path) -> str:
        """读取纯文本文件。"""
        content = path.read_text(encoding="utf-8", errors="replace")
        # 添加文件元信息头
        return f"[File: {path.name}]\n[Size: {len(content)} chars]\n\n{content}"

    @staticmethod
    def _read_pdf(path: Path) -> str:
        """读取 PDF 文件。"""
        # 优先尝试 pdfplumber（表格保留更好）
        try:
            import pdfplumber
            texts = []
            with pdfplumber.open(path) as pdf:
                for i, page in enumerate(pdf.pages, 1):
                    text = page.extract_text()
                    if text:
                        texts.append(f"--- Page {i} ---\n{text}")
            full = "\n\n".join(texts)
            return f"[File: {path.name}]\n[Pages: {len(pdf.pages)}]\n\n{full}"
        except ImportError:
            pass

        # 降级到 PyPDF2
        try:
            from PyPDF2 import PdfReader
            reader = PdfReader(str(path))
            texts = []
            for i, page in enumerate(reader.pages, 1):
                text = page.extract_text()
                if text:
                    texts.append(f"--- Page {i} ---\n{text}")
            full = "\n\n".join(texts)
            return f"[File: {path.name}]\n[Pages: {len(reader.pages)}]\n\n{full}"
        except ImportError:
            return (
                f"[FileReader Error] Cannot read PDF: {path.name}. "
                f"Please install pdfplumber or PyPDF2: pip install pdfplumber"
            )

    @staticmethod
    def _read_csv(path: Path, preview_rows: int = 20) -> str:
        """读取 CSV 文件，返回结构化摘要。"""
        try:
            import pandas as pd
            df = pd.read_csv(path)
            shape = df.shape
            dtypes = df.dtypes.to_dict()
            head = df.head(preview_rows).to_string(index=False)
            summary = (
                f"[File: {path.name}]\n"
                f"[Shape: {shape[0]} rows × {shape[1]} columns]\n"
                f"[Columns: {list(df.columns)}]\n"
                f"[Dtypes: {dtypes}]\n\n"
                f"--- First {preview_rows} rows ---\n{head}"
            )
            if shape[0] > preview_rows:
                summary += f"\n\n[Note: {shape[0] - preview_rows} more rows not shown]"
            return summary
        except ImportError:
            return "[FileReader Error] pandas required for CSV. pip install pandas"

    @staticmethod
    def _read_json(path: Path, max_depth: int = 3) -> str:
        """读取 JSON 文件，返回格式化摘要。"""
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 计算基本统计
        def _summarize(obj, depth: int = 0) -> str:
            if depth > max_depth:
                return "..."
            if isinstance(obj, dict):
                items = []
                for k, v in list(obj.items())[:10]:
                    items.append(f"  {k}: {_summarize(v, depth + 1)}")
                if len(obj) > 10:
                    items.append(f"  ... ({len(obj) - 10} more keys)")
                return "{\n" + "\n".join(items) + "\n}"
            if isinstance(obj, list):
                if len(obj) == 0:
                    return "[]"
                sample = _summarize(obj[0], depth + 1)
                return f"[{len(obj)} items, e.g.: {sample}]"
            return repr(obj)

        summary = _summarize(data)
        type_name = type(data).__name__
        return f"[File: {path.name}]\n[Type: {type_name}]\n\n{summary}"

    @staticmethod
    def _read_docx(path: Path) -> str:
        """读取 Word 文档。"""
        try:
            from docx import Document
            doc = Document(str(path))
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            content = "\n\n".join(paragraphs)
            return f"[File: {path.name}]\n[Paragraphs: {len(paragraphs)}]\n\n{content}"
        except ImportError:
            return (
                f"[FileReader Error] Cannot read DOCX: {path.name}. "
                f"Please install python-docx: pip install python-docx"
            )
