"""PaperPilot Evidence 层：提取、存储、关系图、报告引用。"""
from __future__ import annotations

from .extractor import EvidenceExtractor, extract_papers_from_result
from .graph import EdgeRecord, EvidenceGraph
from .schemas import Evidence
from .store import EvidenceStore

__all__ = [
    "Evidence",
    "EvidenceStore",
    "EvidenceExtractor",
    "extract_papers_from_result",
    "EdgeRecord",
    "EvidenceGraph",
]
