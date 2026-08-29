#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
evaluation/report.py
================================================================================
评测报告生成器：聚合多维度评测结果，生成结构化报告。
================================================================================
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any


class EvaluationReport:
    """统一评测报告容器。"""

    def __init__(self, name: str, num_questions: int = 0) -> None:
        self.name = name
        self.num_questions = num_questions
        self.timestamp = datetime.now().isoformat()
        self.details: list[dict[str, Any]] = []
        self.summary: dict[str, Any] = {}

    def add_detail(self, detail: dict[str, Any]) -> None:
        """添加单条评测明细。"""
        self.details.append(detail)

    def set_summary(self, summary: dict[str, Any]) -> None:
        """设置汇总统计。"""
        self.summary = summary

    def to_dict(self) -> dict[str, Any]:
        """导出为字典。"""
        return {
            "evaluation_name": self.name,
            "timestamp": self.timestamp,
            "num_questions": self.num_questions,
            "summary": self.summary,
            "details": self.details,
        }

    def save(self, output_dir: str, filename: str | None = None) -> str:
        """保存为 JSON 文件。"""
        os.makedirs(output_dir, exist_ok=True)
        if filename is None:
            filename = f"{self.name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        filepath = os.path.join(output_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        return filepath

    def to_markdown(self) -> str:
        """生成 Markdown 格式摘要。"""
        lines = [
            f"# {self.name}",
            "",
            f"- **评测时间**: {self.timestamp}",
            f"- **题目数量**: {self.num_questions}",
            "",
            "## 汇总",
            "",
        ]
        for key, value in self.summary.items():
            lines.append(f"- **{key}**: {value}")
        lines.append("")
        lines.append("## 明细")
        lines.append("")
        for d in self.details:
            lines.append(f"### {d.get('question_id', 'unknown')}")
            for k, v in d.items():
                if k != "question_id":
                    lines.append(f"- {k}: {v}")
            lines.append("")
        return "\n".join(lines)
