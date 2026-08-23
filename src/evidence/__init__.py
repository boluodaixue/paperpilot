"""PaperPilot Evidence 层：提取、存储、报告引用。"""
from __future__ import annotations

from .extractor import EvidenceExtractor, extract_papers_from_result
from .schemas import Evidence
from .store import EvidenceStore

__all__ = ["Evidence", "EvidenceStore", "EvidenceExtractor", "extract_papers_from_result"]
