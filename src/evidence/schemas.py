"""
PaperPilot: Evidence 数据模型。

结构化证据 = 论文级 claim + 逐字证据摘录 + 来源论文。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Evidence:
    """一条从论文中提取的结构化证据。"""

    evidence_id: str  # "E-<n>"，每 session 唯一，兼作报告引用 key
    query: str
    paper_id: str
    paper_title: str
    source_url: str  # paper["pdf_url"]
    claim: str
    evidence_text: str  # 从摘要逐字摘录的支撑片段
    confidence: float
    topic: str  # query[:50]
    session_id: str = ""
    embedding: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict，embedding 转 JSON 字符串（入库）。"""
        return {
            "evidence_id": self.evidence_id,
            "query": self.query,
            "paper_id": self.paper_id,
            "paper_title": self.paper_title,
            "source_url": self.source_url,
            "claim": self.claim,
            "evidence_text": self.evidence_text,
            "confidence": self.confidence,
            "topic": self.topic,
            "session_id": self.session_id,
            "embedding_json": json.dumps(self.embedding, ensure_ascii=False),
        }

    @classmethod
    def from_row(cls, row: Any) -> "Evidence":
        """从 SQLite Row 反序列化。"""
        return cls(
            evidence_id=row["evidence_id"],
            query=row["query"],
            paper_id=row["paper_id"],
            paper_title=row["paper_title"],
            source_url=row["source_url"],
            claim=row["claim"],
            evidence_text=row["evidence_text"],
            confidence=row["confidence"],
            topic=row["topic"],
            session_id=row["session_id"],
            embedding=json.loads(row["embedding_json"]),
        )

    def to_report_dict(self) -> dict[str, Any]:
        """报告/合成器使用的精简形状。"""
        return {
            "evidence_id": self.evidence_id,
            "claim": self.claim,
            "paper_title": self.paper_title,
            "source_url": self.source_url,
            "confidence": self.confidence,
        }
