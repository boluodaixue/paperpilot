"""W2 CLI acceptance tests for managed Memory and legacy output locations."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import run_repl, run_single
from scripts._workflow_cli import (
    format_result_locations,
    run_reviewed_workflow,
    vault_name_from_config,
)
from src.research.models import (
    MemoryManifest,
    ResearchBrief,
    ResearchResult,
    ResearchStatus,
    ResearchWorkflowResult,
)
from src.research.obsidian import build_obsidian_open_uri


def _result(memory_id: str | None) -> ResearchWorkflowResult:
    prefix = f"Memories/{memory_id}/" if memory_id is not None else ""
    return ResearchWorkflowResult(
        brief=ResearchBrief(
            question="Question",
            objective="Objective",
            scope=(),
            directions=("Direction",),
            constraints=(),
            expected_output="Markdown",
        ),
        research_result=ResearchResult(
            task_id="task",
            status=ResearchStatus.COMPLETED,
            summary="Complete",
        ),
        report_markdown="# Report\n",
        memory_manifest=MemoryManifest(
            report_path=f"{prefix}reports/Report-test.md",
        ),
        memory_id=memory_id,
    )


class _Runtime:
    def __init__(self, root: Path, result: ResearchWorkflowResult) -> None:
        self.memory_store = SimpleNamespace(root=root)
        self.result = result
        self.start_calls: list[dict[str, Any]] = []
        self.closed = False
        self._counter = 0

    def new_thread_id(self) -> str:
        self._counter += 1
        return f"research-{self._counter}"

    async def start(
        self,
        question: str,
        *,
        thread_id: str,
        memory_id: str | None = None,
    ) -> dict[str, Any]:
        self.start_calls.append(
            {
                "question": question,
                "thread_id": thread_id,
                "memory_id": memory_id,
            }
        )
        return {"brief": self.result.brief}

    async def review(
        self,
        thread_id: str,
        action: str,
        feedback: str | None = None,
    ) -> dict[str, Any]:
        return {"workflow_result": self.result}

    async def close(self, *, shutdown: bool = False) -> None:
        self.closed = True


class _LegacyRuntime:
    """The N5-compatible fake deliberately has no memory_id parameter."""

    def __init__(self, root: Path) -> None:
        self.memory_store = SimpleNamespace(root=root)
        self.result = _result(None)
        self.started = False

    async def start(self, question: str, *, thread_id: str) -> dict[str, Any]:
        self.started = True
        return {"brief": self.result.brief}

    async def review(
        self,
        thread_id: str,
        action: str,
        feedback: str | None = None,
    ) -> dict[str, Any]:
        return {"workflow_result": self.result}


def test_managed_output_includes_vault_home_uri_and_report(tmp_path: Path) -> None:
    vault_root = tmp_path / "中文 Vault"
    runtime = _Runtime(vault_root, _result("M-paper-notes"))
    vault_name = "论文 Vault"

    output = format_result_locations(
        runtime,  # type: ignore[arg-type]
        runtime.result,
        vault_name=vault_name,
    )

    expected_home = (vault_root / "Memories/M-paper-notes/Home.md").resolve()
    expected_uri = build_obsidian_open_uri(
        vault_root,
        "Memories/M-paper-notes/Home.md",
        vault_name=vault_name,
    )
    assert f"Vault: {vault_root.resolve()}" in output
    assert f"Memory Home: {expected_home}" in output
    assert f"Obsidian URI: {expected_uri}" in output
    assert f"Report: {(vault_root / runtime.result.memory_manifest.report_path).resolve()}" in output
    assert "%E8%AE%BA%E6%96%87%20Vault" in output


def test_legacy_output_does_not_invent_memory_home_or_uri(tmp_path: Path) -> None:
    runtime = _LegacyRuntime(tmp_path)

    output = format_result_locations(runtime, runtime.result)  # type: ignore[arg-type]

    assert f"Vault: {tmp_path.resolve()}" in output
    assert "Memory Home: unavailable (legacy Memory)" in output
    assert "Obsidian URI: unavailable (legacy Memory)" in output
    assert f"Report: {(tmp_path / 'reports/Report-test.md').resolve()}" in output
    assert "Memories/" not in output


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({}, None),
        ({"research": {}}, None),
        ({"research": {"vault_name": None}}, None),
        ({"research": {"vault_name": "  Paper Vault  "}}, "Paper Vault"),
    ],
)
def test_optional_vault_name_config(config: dict[str, Any], expected: str | None) -> None:
    assert vault_name_from_config(config) == expected


@pytest.mark.parametrize("value", ["", "   ", 1, [], {}])
def test_invalid_explicit_vault_name_is_a_config_error(value: Any) -> None:
    with pytest.raises(ValueError, match="research.vault_name"):
        vault_name_from_config({"research": {"vault_name": value}})


@pytest.mark.asyncio
async def test_reviewed_workflow_passes_only_selected_memory(tmp_path: Path) -> None:
    runtime = _Runtime(tmp_path, _result("M-selected"))
    result = await run_reviewed_workflow(
        runtime,  # type: ignore[arg-type]
        "Question",
        thread_id="root-managed",
        memory_id="M-selected",
        auto_confirm=True,
    )

    assert result.memory_id == "M-selected"
    assert runtime.start_calls == [
        {
            "question": "Question",
            "thread_id": "root-managed",
            "memory_id": "M-selected",
        }
    ]


@pytest.mark.asyncio
async def test_reviewed_workflow_legacy_fake_keeps_old_start_signature(
    tmp_path: Path,
) -> None:
    runtime = _LegacyRuntime(tmp_path)

    result = await run_reviewed_workflow(
        runtime,  # type: ignore[arg-type]
        "Question",
        thread_id="root-legacy",
        auto_confirm=True,
    )

    assert runtime.started is True
    assert result.memory_id is None


@pytest.mark.asyncio
async def test_single_run_passes_memory_and_formats_locations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Runtime(tmp_path, _result("M-single"))
    monkeypatch.setattr(run_single, "load_config", lambda _: {"research": {}})
    monkeypatch.setattr(run_single, "build_research_runtime", lambda **_: runtime)
    args = SimpleNamespace(
        config=None,
        query="Question",
        thread_id="root-single",
        memory_id="M-single",
        yes=True,
    )

    output = await run_single._run(args)

    assert runtime.start_calls[0]["memory_id"] == "M-single"
    assert "Memory Home:" in output and "Obsidian URI:" in output
    assert runtime.closed is True


@pytest.mark.asyncio
async def test_repl_passes_one_selected_memory_to_each_question(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Runtime(tmp_path, _result("M-repl"))
    answers = iter(["Question", "q"])
    output: list[str] = []
    reviewed_calls: list[dict[str, Any]] = []

    async def reviewed(runtime_arg, question, **kwargs):
        reviewed_calls.append(
            {"runtime": runtime_arg, "question": question, **kwargs}
        )
        return runtime.result

    monkeypatch.setattr(run_repl, "load_config", lambda _: {"research": {}})
    monkeypatch.setattr(run_repl, "build_research_runtime", lambda **_: runtime)
    monkeypatch.setattr(run_repl, "run_reviewed_workflow", reviewed)
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr("builtins.print", lambda *items, **_: output.append(" ".join(map(str, items))))
    args = SimpleNamespace(config=None, session_id="session", memory_id="M-repl")

    await run_repl._repl(args)

    assert reviewed_calls[0]["memory_id"] == "M-repl"
    assert any("Obsidian URI:" in line for line in output)
    assert runtime.closed is True
