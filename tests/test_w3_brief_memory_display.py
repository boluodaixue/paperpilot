"""W3 acceptance tests for read-only Memory context in Research Briefs."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import web.server as server
from scripts._workflow_cli import format_brief
from src.research.models import ResearchBrief


def _legacy_brief() -> ResearchBrief:
    return ResearchBrief(
        question="What changed?",
        objective="Explain the change",
        scope=("Current behavior",),
        directions=("Use primary sources",),
        constraints=("Cite evidence",),
        expected_output="Markdown report",
        revision=2,
    )


def test_cli_managed_brief_displays_all_memory_context_read_only_fields():
    brief = ResearchBrief(
        question="What remains unknown?",
        objective="Extend prior research",
        scope=("Current behavior",),
        directions=("Fill the evidence gap",),
        constraints=("Use the selected Memory only",),
        expected_output="Markdown report",
        memory_id="M-prior-work",
        memory_paths=(
            "Memories/M-prior-work/Home.md",
            "Memories/M-prior-work/reports/Report-old.md",
        ),
        known_information=("The earlier report established finding A.",),
        research_gaps=("Primary evidence for finding B is missing.",),
    )

    output = format_brief(brief)

    assert "Target Memory: M-prior-work" in output
    assert "Matched Memory files:" in output
    assert "Memories/M-prior-work/Home.md" in output
    assert "Memories/M-prior-work/reports/Report-old.md" in output
    assert "Known information:" in output
    assert "The earlier report established finding A." in output
    assert "New research gaps:" in output
    assert "Primary evidence for finding B is missing." in output
    assert "Objective: Extend prior research" in output


def test_cli_legacy_brief_keeps_previous_output_shape():
    output = format_brief(_legacy_brief())

    assert output == """
Research Brief
  Question: What changed?
  Objective: Explain the change
  Scope:
    - Current behavior
  Directions:
    - Use primary sources
  Constraints:
    - Cite evidence
  Expected output: Markdown report
  Revision: 2"""
    for managed_label in (
        "Target Memory:",
        "Matched Memory files:",
        "Known information:",
        "New research gaps:",
    ):
        assert managed_label not in output


def test_web_brief_has_read_only_memory_context_and_preserves_fields():
    source = (Path(server.STATIC_DIR) / "index.html").read_text(encoding="utf-8")

    for field_id in (
        "briefMemoryContext",
        "p_memory_id",
        "p_memory_paths",
        "p_known_information",
        "p_research_gaps",
    ):
        assert f'id="{field_id}"' in source
        assert f'<textarea id="{field_id}"' not in source
        assert f'<input id="{field_id}"' not in source
    for required in (
        "Memory 研究上下文（只读）",
        "renderBriefMemoryContext(brief);",
        "brief.memory_id",
        "brief.memory_paths",
        "brief.known_information",
        "brief.research_gaps",
        "...(currentProposal ? currentProposal.brief : {})",
        "showProposalFromHistory(msgs)",
        "showProposalCard(currentProposal)",
    ):
        assert required in source
    for forbidden in (
        "/api/memory/ask",
        "/api/memories/ask",
        "/api/memories/notes",
        "saveMemoryNote",
        "continueResearch",
    ):
        assert forbidden not in source


def test_web_inline_javascript_is_valid():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    page = Path(server.STATIC_DIR) / "index.html"
    checker = (
        "const fs=require('fs');"
        "const html=fs.readFileSync(process.argv[1],'utf8');"
        "const match=html.match(/<script>([\\s\\S]*)<\\/script>/);"
        "if(!match)throw new Error('inline script missing');"
        "new Function(match[1]);"
    )
    result = subprocess.run(
        [node, "-e", checker, str(page)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
