"""Obsidian Vault 导出单元测试。"""

from __future__ import annotations

import re
from pathlib import Path

from src.evidence.graph import EdgeRecord
from src.evidence.obsidian_export import export_session_vault
from src.evidence.schemas import Evidence


def _evidence(eid: str, claim: str, paper_id: str, paper_title: str = "Paper", text: str = "quote") -> Evidence:
    return Evidence(
        evidence_id=eid, query="q", paper_id=paper_id, paper_title=paper_title,
        source_url=f"https://arxiv.org/pdf/{paper_id}",
        claim=claim, evidence_text=text, confidence=0.9, topic="t",
        session_id="s",
    )


def _relation(a: str, b: str, relation: str = "SUPPORTS", weight: float = 0.8) -> EdgeRecord:
    return EdgeRecord(
        session_id="s", source_id=a, target_id=b, relation=relation, weight=weight,
        source_claim=f"claim {a}", target_claim=f"claim {b}",
        source_paper="Paper A", target_paper="Paper B",
    )


def test_export_creates_vault_files(tmp_path):
    report = "# 报告\n\n注意力机制很重要 [E-1]，后来被扩展 [E-2]。\n\nOverall Confidence: 0.8"
    evidence = [
        _evidence("E-1", "Attention is key", "2101.00001", "Paper One", "verbatim one"),
        _evidence("E-2", "Attention extends", "2101.00002", "Paper Two", "verbatim two"),
    ]
    relations = [_relation("E-1", "E-2", "EXTENDS", 0.7)]

    vault = export_session_vault("test-s", report, evidence, relations, output_root=str(tmp_path))

    files = {p.name for p in Path(vault).glob("*.md")}
    assert {"00-研究报告.md", "01-证据索引.md", "02-证据关系.md", "E-1.md", "E-2.md",
            "paper-2101.00001.md", "paper-2101.00002.md"} <= files


def test_report_has_wikilinks(tmp_path):
    report = "模型 A 更优 [E-1]，与 [E-2] 矛盾。"
    evidence = [_evidence("E-1", "A", "p1"), _evidence("E-2", "B", "p2")]
    vault = export_session_vault("s", report, evidence, [], output_root=str(tmp_path))
    content = (Path(vault) / "00-研究报告.md").read_text(encoding="utf-8")
    assert "[[E-1|E-1]]" in content
    assert "[[E-2|E-2]]" in content
    # 纯文本 [E-1] 标记已全部转成双链
    assert re.search(r"(?<!\[)\[E-\d+\](?!\])", content) is None


def test_evidence_note_has_claim_quote_and_paper_link(tmp_path):
    ev = _evidence("E-1", "Attention is key", "2101.00001", "Paper One", "verbatim quote")
    vault = export_session_vault("s", "report [E-1]", [ev], [], output_root=str(tmp_path))
    content = (Path(vault) / "E-1.md").read_text(encoding="utf-8")
    assert "# Attention is key" in content
    assert "> verbatim quote" in content  # 逐字摘录用 blockquote
    assert "[[paper-2101.00001|Paper One]]" in content
    assert "置信度" in content


def test_paper_note_aggregates_its_evidence(tmp_path):
    evs = [
        _evidence("E-1", "claim one", "2101.00001", "Paper One"),
        _evidence("E-2", "claim two", "2101.00001", "Paper One"),
        _evidence("E-3", "claim three", "2101.00002", "Paper Two"),
    ]
    vault = export_session_vault("s", "r", evs, [], output_root=str(tmp_path))
    content = (Path(vault) / "paper-2101.00001.md").read_text(encoding="utf-8")
    assert "# Paper One" in content
    assert "[[E-1]]" in content and "[[E-2]]" in content
    assert "[[E-3]]" not in content
    assert "证据数" in content and "2" in content


def test_relation_file_lists_with_links(tmp_path):
    evidence = [_evidence("E-1", "a", "p1"), _evidence("E-2", "b", "p2")]
    relations = [
        _relation("E-1", "E-2", "CONTRADICTS", 0.87),
        _relation("E-1", "E-2", "SUPPORTS", 0.8),
    ]
    vault = export_session_vault("s", "r", evidence, relations, output_root=str(tmp_path))
    content = (Path(vault) / "02-证据关系.md").read_text(encoding="utf-8")
    assert "## 矛盾（1）" in content
    assert "[[E-1]] vs [[E-2]]" in content
    assert "（weight 0.87）" in content
    assert "## 支持（1）" in content


def test_evidence_note_links_related_evidence(tmp_path):
    evidence = [_evidence("E-1", "a", "p1"), _evidence("E-2", "b", "p2")]
    relations = [_relation("E-1", "E-2", "SUPPORTS", 0.8)]
    vault = export_session_vault("s", "r", evidence, relations, output_root=str(tmp_path))
    content = (Path(vault) / "E-1.md").read_text(encoding="utf-8")
    assert "## 相关证据" in content
    assert "[[E-2]] 支持本证据（weight 0.80）" in content


def test_export_idempotent(tmp_path):
    evidence = [_evidence("E-1", "a", "p1")]
    vault1 = export_session_vault("s", "r [E-1]", evidence, [], output_root=str(tmp_path))
    n1 = len(list(Path(vault1).glob("*.md")))
    vault2 = export_session_vault("s", "r [E-1] updated", evidence, [], output_root=str(tmp_path))
    n2 = len(list(Path(vault2).glob("*.md")))
    assert vault1 == vault2
    assert n1 == n2
    # 覆盖写反映最新报告
    assert "updated" in (Path(vault2) / "00-研究报告.md").read_text(encoding="utf-8")
