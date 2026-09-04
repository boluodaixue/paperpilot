"""S0 integration tests for per-run FileReader authorization."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import web.server as server
from src.research.memory import MarkdownMemoryStore
from src.research.models import AgentLimits
from src.research.runtime import ResearchRuntime
from src.research.vault import LEGACY_MEMORY_ID
from src.tools import FileReaderError, FileReaderTool


class _TwoPartyGate:
    def __init__(self) -> None:
        self._arrivals = 0
        self._event = asyncio.Event()

    async def wait(self) -> None:
        self._arrivals += 1
        if self._arrivals == 2:
            self._event.set()
        await self._event.wait()


class _ScopedReadGraph:
    """Minimal graph adapter that observes the scope around Runtime calls."""

    def __init__(self, reader: FileReaderTool, *, gate: _TwoPartyGate | None = None) -> None:
        self.reader = reader
        self.gate = gate
        self.states: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _thread_id(config: dict[str, Any]) -> str:
        return str(config["configurable"]["thread_id"])

    async def _read(self, thread_id: str, memory_id: str | None) -> dict[str, Any]:
        if self.gate is not None:
            await self.gate.wait()
        result = await self.reader.execute(root="memory", path="notes/scope.txt")
        state = {"memory_id": memory_id, "file_read": result}
        self.states[thread_id] = state
        return state

    async def ainvoke(self, value: Any, *, config: dict[str, Any]) -> dict[str, Any]:
        thread_id = self._thread_id(config)
        if isinstance(value, dict):
            memory_id = value.get("memory_id")
        else:
            memory_id = self.states[thread_id].get("memory_id")
        return await self._read(thread_id, memory_id)

    async def aget_state(self, config: dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(values=self.states[self._thread_id(config)])

    async def astream(self, value: Any, *, config: dict[str, Any], **_kwargs: Any):
        thread_id = self._thread_id(config)
        memory_id = self.states[thread_id].get("memory_id")
        yield {"scope": await self._read(thread_id, memory_id)}


def _runtime(store: MarkdownMemoryStore, graph: _ScopedReadGraph) -> ResearchRuntime:
    runtime = object.__new__(ResearchRuntime)
    runtime.memory_store = store
    runtime.graph = graph
    runtime.limits = AgentLimits()
    return runtime


def _write_scope_file(store: MarkdownMemoryStore, memory_id: str, text: str) -> None:
    path = store.root / "Memories" / memory_id / "notes" / "scope.txt"
    path.write_text(text, encoding="utf-8")


@pytest.mark.asyncio
async def test_concurrent_runs_keep_managed_memory_file_scopes_isolated(
    tmp_path: Path,
) -> None:
    store = MarkdownMemoryStore(tmp_path / "Vault")
    store.create_memory("Alpha", "M-A")
    store.create_memory("Beta", "M-B")
    _write_scope_file(store, "M-A", "alpha-only")
    _write_scope_file(store, "M-B", "beta-only")
    graph = _ScopedReadGraph(FileReaderTool(), gate=_TwoPartyGate())
    runtime = _runtime(store, graph)

    alpha, beta = await asyncio.gather(
        runtime.start("A", thread_id="thread-a", memory_id="M-A"),
        runtime.start("B", thread_id="thread-b", memory_id="M-B"),
    )

    assert "alpha-only" in str(alpha["file_read"])
    assert "beta-only" not in str(alpha["file_read"])
    assert "beta-only" in str(beta["file_read"])
    assert "alpha-only" not in str(beta["file_read"])


@pytest.mark.asyncio
async def test_runtime_review_and_stream_confirm_restore_selected_memory_scope(
    tmp_path: Path,
) -> None:
    store = MarkdownMemoryStore(tmp_path / "Vault")
    store.create_memory("Alpha", "M-A")
    _write_scope_file(store, "M-A", "alpha-resume")
    graph = _ScopedReadGraph(FileReaderTool())
    runtime = _runtime(store, graph)
    graph.states["review-thread"] = {"memory_id": "M-A"}
    graph.states["stream-thread"] = {"memory_id": "M-A"}

    reviewed = await runtime.review("review-thread", "confirm")
    streamed = [item async for item in runtime.stream_confirm("stream-thread")]

    assert "alpha-resume" in str(reviewed["file_read"])
    assert "alpha-resume" in str(streamed)


@pytest.mark.asyncio
async def test_no_memory_scope_is_deny_all_and_legacy_cannot_start(
    tmp_path: Path,
) -> None:
    store = MarkdownMemoryStore(tmp_path / "Vault")
    store.create_memory("Alpha", "M-A")
    _write_scope_file(store, "M-A", "must-not-leak")
    graph = _ScopedReadGraph(FileReaderTool())
    runtime = _runtime(store, graph)

    with pytest.raises(FileReaderError) as exc_info:
        await runtime.start("No Memory", thread_id="unbound", memory_id=None)
    assert "must-not-leak" not in str(exc_info.value)
    with pytest.raises(ValueError, match="M-legacy is read-only"):
        await runtime.start(
            "Legacy",
            thread_id="legacy",
            memory_id=LEGACY_MEMORY_ID,
        )


@pytest.mark.asyncio
async def test_web_stream_confirmation_uses_runtime_scope_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _Runtime:
        async def stream_confirm(self, thread_id: str):
            calls.append(thread_id)
            yield {"execution_events": []}

        async def get_state(self, thread_id: str) -> dict[str, Any]:
            return {"thread_id": thread_id, "execution_events": []}

    monkeypatch.setattr(server.get_research_runtime, "_runtime", _Runtime(), raising=False)
    task = server.ResearchTask("web-thread", "web-session", "question", "M-A")

    state = await server._stream_confirm(task)

    assert calls == ["web-thread"]
    assert state["thread_id"] == "web-thread"


@pytest.mark.asyncio
async def test_runtime_exposes_only_root_thread_artifacts(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path / "Vault")
    store.create_memory("Alpha", "M-A")
    graph = _ScopedReadGraph(FileReaderTool())
    runtime = _runtime(store, graph)
    thread_id = "root-artifact-scope"
    scope = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:20]
    artifact_dir = store.root / "Artifacts" / scope
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "artifact-one.json").write_text('{"result":"scoped"}', encoding="utf-8")

    with runtime._research_file_scope("M-A", thread_id):
        result = await graph.reader.execute(root="artifact", path="artifact-one.json")

    assert "scoped" in result["content"]
