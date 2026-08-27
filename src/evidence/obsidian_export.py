"""
Obsidian Vault 导出：把 PaperPilot 的报告 + 证据 + 关系导出为 Obsidian 双链仓库。

结构（outputs/obsidian/<session_id>/）：
    00-研究报告.md       报告正文，[E-x] → [[E-x|E-x]] 双链
    01-证据索引.md       全部证据列表
    02-证据关系.md       矛盾 / 支持 / 扩展（互相双链）
    E-<n>.md             每条证据（claim）一个笔记
    paper-<id>.md        每篇论文一个聚合笔记

用户在 Obsidian 里打开该目录即可获得原生双链跳转 / 反链 / Graph View。
"""

from __future__ import annotations

import re
from pathlib import Path

from .graph import EdgeRecord
from .schemas import Evidence

# 报告正文里的引用标记 [E-N] → Obsidian 双链
_CITE_RE = re.compile(r"\[E-(\d+)\]")

REL_CN = {"SUPPORTS": "支持", "CONTRADICTS": "矛盾", "EXTENDS": "扩展"}
SEMANTIC_RELATIONS = {"SUPPORTS", "CONTRADICTS", "EXTENDS"}


def _sanitize_filename(name: str) -> str:
    return re.sub(r"[^\w.\-]", "_", name)


def _wikilink(eid: str) -> str:
    """证据笔记双链：[[E-1]]。"""
    return f"[[{eid}]]"


def _report_with_links(report_md: str) -> str:
    """把正文 [E-N] 替换成可点击的 Obsidian 双链。"""
    return _CITE_RE.sub(lambda m: f"[[E-{m.group(1)}|E-{m.group(1)}]]", report_md)


def _evidence_note(ev: Evidence, relations: list[EdgeRecord]) -> str:
    lines = [f"# {ev.claim}", ""]
    if ev.evidence_text:
        lines += [f"> {ev.evidence_text}", ""]
    lines.append(f"- **来源论文**：[[paper-{_sanitize_filename(ev.paper_id)}|{ev.paper_title}]]")
    if ev.source_url:
        lines.append(f"- **链接**：[{ev.source_url}]({ev.source_url})")
    lines.append(f"- **置信度**：{ev.confidence:.2f}")
    if ev.topic:
        lines.append(f"- **主题**：{ev.topic}")
    lines.append("")

    related = [
        r for r in relations
        if r.source_id == ev.evidence_id or r.target_id == ev.evidence_id
    ]
    if related:
        lines.append("## 相关证据")
        lines.append("")
        for r in related:
            other = r.target_id if r.source_id == ev.evidence_id else r.source_id
            direction = "支持本证据" if r.relation == "SUPPORTS" else (
                "与本证据矛盾" if r.relation == "CONTRADICTS" else "扩展本证据"
            )
            lines.append(f"- {_wikilink(other)} {direction}（weight {r.weight:.2f}）")
        lines.append("")
    return "\n".join(lines)


def _paper_note(paper_id: str, title: str, url: str, evidence: list[Evidence]) -> str:
    lines = [f"# {title}", ""]
    if url:
        lines.append(f"- **链接**：[{url}]({url})")
    lines.append(f"- **证据数**：{len(evidence)}")
    lines.append("")
    lines.append("## 证据")
    lines.append("")
    for ev in evidence:
        lines.append(f"- {_wikilink(ev.evidence_id)}：{ev.claim[:60]}")
    lines.append("")
    return "\n".join(lines)


def export_session_vault(
    session_id: str,
    report_md: str,
    evidence: list[Evidence],
    relations: list[EdgeRecord],
    output_root: str = "outputs/obsidian",
) -> str:
    """把会话导出为 Obsidian Vault，返回 vault 目录绝对路径。"""
    vault_dir = Path(output_root) / _sanitize_filename(session_id)
    vault_dir.mkdir(parents=True, exist_ok=True)

    # 00-研究报告.md
    (vault_dir / "00-研究报告.md").write_text(
        f"# 研究报告：{session_id}\n\n---\n\n{_report_with_links(report_md)}\n",
        encoding="utf-8",
    )

    # 01-证据索引.md
    index_lines = ["# 证据索引", ""]
    for ev in sorted(evidence, key=lambda e: e.evidence_id):
        index_lines.append(
            f"- {_wikilink(ev.evidence_id)} {ev.claim[:40]}"
            f"（*{ev.paper_title}*，置信度 {ev.confidence:.2f}）"
        )
    (vault_dir / "01-证据索引.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")

    # 02-证据关系.md
    semantic = [r for r in relations if r.relation in SEMANTIC_RELATIONS]
    rel_lines = ["# 证据关系", ""]
    for rel in ("CONTRADICTS", "SUPPORTS", "EXTENDS"):
        group = [r for r in semantic if r.relation == rel]
        if not group:
            continue
        rel_lines.append(f"## {REL_CN[rel]}（{len(group)}）")
        rel_lines.append("")
        for r in group:
            rel_lines.append(
                f"- {_wikilink(r.source_id)} vs {_wikilink(r.target_id)}（weight {r.weight:.2f}）："
                f"{r.source_claim[:40]} — {r.target_claim[:40]}"
                f"（{r.source_paper} vs {r.target_paper}）"
            )
        rel_lines.append("")
    (vault_dir / "02-证据关系.md").write_text("\n".join(rel_lines) + "\n", encoding="utf-8")

    # E-<n>.md + paper-<id>.md
    papers: dict[str, list[Evidence]] = {}
    for ev in evidence:
        (vault_dir / f"{ev.evidence_id}.md").write_text(
            _evidence_note(ev, semantic), encoding="utf-8"
        )
        papers.setdefault(ev.paper_id, []).append(ev)

    for paper_id, evs in papers.items():
        first = evs[0]
        (vault_dir / f"paper-{_sanitize_filename(paper_id)}.md").write_text(
            _paper_note(paper_id, first.paper_title, first.source_url, evs),
            encoding="utf-8",
        )

    return str(vault_dir.resolve())
