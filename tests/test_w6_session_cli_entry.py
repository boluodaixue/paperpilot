"""Focused W6 tests for one Runtime facade and durable Web Memory binding."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import web.server as server
from scripts import run_repl, run_single
from scripts._workflow_cli import confirm_memory_import, run_memory_question
from src.memory.chat_store import ChatStore
from src.research.memory import MarkdownMemoryStore
from src.research.models import (
    MemoryAnswer,
    MemoryCitation,
    MemoryImportProposal,
    MemoryNoteProposal,
)
from src.research.vault import LEGACY_MEMORY_ID
from src.research.runtime import build_research_runtime


def test_chat_store_adds_only_nullable_memory_id_and_binds_once(tmp_path: Path) -> None:
    database = tmp_path / "chat.db"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE session_meta (
            session_id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            pinned INTEGER NOT NULL DEFAULT 0,
            sort_order REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL
        )
        """
    )
    connection.commit()
    connection.close()

    store = ChatStore(str(database))
    columns = {
        row[1]
        for row in sqlite3.connect(database).execute(
            "PRAGMA table_info(session_meta)"
        ).fetchall()
    }
    assert "memory_id" in columns
    assert "memory_bound" not in columns
    assert store.get_memory_binding("old-session") is None

    assert store.bind_memory("session", "M-alpha") == "M-alpha"
    assert store.bind_memory("session", "M-alpha") == "M-alpha"
    with pytest.raises(ValueError, match="different Memory"):
        store.bind_memory("session", "M-beta")
    assert ChatStore(str(database)).get_memory_binding("session") == "M-alpha"
    listed = {item["session_id"]: item for item in store.list_sessions()}
    assert listed["session"]["memory_id"] == "M-alpha"


class _WebRuntime:
    def __init__(self, root: Path) -> None:
        self.memory_store = MarkdownMemoryStore(root)
        self.memory_store.create_memory("Alpha", "M-alpha")
        self.memory_store.create_memory("Beta", "M-beta")
        legacy_report = root / "reports" / "Report-old.md"
        legacy_report.parent.mkdir(parents=True)
        legacy_report.write_text(
            "# Existing report\n\nLegacybaseline is supported.\n",
            encoding="utf-8",
        )
        self.counter = 0

    def get_memory(self, memory_id: str):
        return self.memory_store.get_memory(memory_id)

    def list_memories(self):
        return self.memory_store.list_memories()

    def get_memory_option(self, memory_id: str):
        if memory_id == LEGACY_MEMORY_ID:
            if not (self.memory_store.root / "reports" / "Report-old.md").is_file():
                raise FileNotFoundError(memory_id)
            return {
                "memory_id": LEGACY_MEMORY_ID,
                "title": "Existing Memory (read-only)",
                "relative_path": None,
                "created_at": None,
                "updated_at": None,
                "read_only": True,
                "can_migrate": True,
                "file_count": 1,
            }
        descriptor = self.get_memory(memory_id)
        return {
            **descriptor.__dict__,
            "read_only": False,
            "can_migrate": False,
            "file_count": None,
        }

    def list_memory_options(self):
        return tuple(
            self.get_memory_option(item.memory_id) for item in self.list_memories()
        ) + (self.get_memory_option(LEGACY_MEMORY_ID),)

    def create_memory(self, title: str):
        return self.memory_store.create_memory(title)

    def prepare_legacy_memory_migration(self, title: str, memory_id: str):
        return self.memory_store.prepare_legacy_memory_migration(title, memory_id)

    def commit_legacy_memory_migration(self, proposal):
        return self.memory_store.commit_legacy_memory_migration(proposal)

    def new_thread_id(self) -> str:
        self.counter += 1
        return f"research-{self.counter}"

    async def start(self, question: str, *, thread_id: str, memory_id=None):
        brief = {
            "question": question,
            "objective": question,
            "scope": [],
            "directions": [],
            "constraints": [],
            "expected_output": "Markdown",
            "revision": 0,
            "memory_id": memory_id,
        }
        return {"__interrupt__": (SimpleNamespace(value={
            "kind": "research_brief_confirmation", "brief": brief,
        }),)}

    async def answer_memory(self, memory_id: str, question: str) -> MemoryAnswer:
        if memory_id == LEGACY_MEMORY_ID:
            citation = MemoryCitation(
                relative_path="reports/Report-old.md",
                title="Existing report",
                wikilink="[[reports/Report-old]]",
            )
        else:
            citation = MemoryCitation(
                relative_path=f"Memories/{memory_id}/Home.md",
                title="Home",
                wikilink=f"[[Memories/{memory_id}/Home]]",
            )
        return MemoryAnswer(
            answer_id=f"Answer-{self.counter + 1}",
            memory_id=memory_id,
            question=question,
            markdown="Grounded",
            citations=(citation,),
            insufficient_evidence=(),
        )

    async def close(self, *, shutdown: bool = False) -> None:
        return None


@pytest.fixture()
def bound_web_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runtime = _WebRuntime(tmp_path / "Vault")
    database = tmp_path / "chat.db"
    monkeypatch.setattr(server, "CHAT_DB_PATH", str(database))
    monkeypatch.setattr(server, "_config", {"research": {}})
    server.get_chat_store._store = None
    server.get_research_runtime._runtime = runtime
    server._TASKS.clear()
    server._MEMORY_ANSWERS.clear()
    server._MEMORY_NOTE_PROPOSALS.clear()
    server._MEMORY_IMPORT_PROPOSALS.clear()
    server._LEGACY_MIGRATION_PROPOSALS.clear()
    with TestClient(server.app) as client:
        yield client, database
    server._TASKS.clear()
    server._MEMORY_ANSWERS.clear()
    server._MEMORY_NOTE_PROPOSALS.clear()
    server._MEMORY_IMPORT_PROPOSALS.clear()
    server._LEGACY_MIGRATION_PROPOSALS.clear()
    server.get_chat_store._store = None
    server.get_research_runtime._runtime = None


def test_web_session_binding_covers_answer_import_note_and_research(
    bound_web_client,
) -> None:
    client, database = bound_web_client
    answer = client.post(
        "/api/memories/M-alpha/answers",
        json={"session_id": "fixed", "question": "Known?"},
    )
    assert answer.status_code == 200
    assert answer.json()["session_id"] == "fixed"

    assert client.post(
        "/api/memories/M-beta/answers",
        json={"session_id": "fixed", "question": "Switch?"},
    ).status_code == 409
    assert client.post(
        "/api/memories/M-beta/import-proposals",
        json={"kind": "text", "session_id": "fixed", "title": "T", "text": "x"},
    ).status_code == 409
    assert client.post(
        "/api/memories/M-beta/note-proposals",
        json={"session_id": "fixed", "answer_id": answer.json()["answer_id"]},
    ).status_code == 409

    aligned = client.post(
        "/api/alignment",
        json={"session_id": "fixed", "memory_id": "M-alpha", "message": "Research"},
    )
    assert aligned.status_code == 200
    server._TASKS[aligned.json()["task_id"]].status = "done"
    assert client.post(
        "/api/alignment",
        json={"session_id": "fixed", "memory_id": "M-beta", "message": "Switch"},
    ).status_code == 409

    sessions = {item["session_id"]: item for item in client.get("/api/sessions").json()}
    assert sessions["fixed"]["memory_id"] == "M-alpha"
    server.get_chat_store._store = ChatStore(str(database))
    restored = {item["session_id"]: item for item in client.get("/api/sessions").json()}
    assert restored["fixed"]["memory_id"] == "M-alpha"


def test_w6_web_runtime_rejects_missing_memory_before_creating_legacy_work(
    bound_web_client,
) -> None:
    client, _ = bound_web_client

    response = client.post(
        "/api/alignment",
        json={"session_id": "missing-memory", "message": "Research"},
    )

    assert response.status_code == 400
    assert "managed Memory" in response.json()["detail"]
    assert "missing-memory" not in {
        item["session_id"] for item in client.get("/api/sessions").json()
    }
    assert not server._TASKS


def test_static_web_uses_session_binding_instead_of_last_report() -> None:
    source = (Path(server.STATIC_DIR) / "index.html").read_text(encoding="utf-8")
    for required in (
        'id="memoryIdBadge"',
        "let sessionMemoryBound = false",
        "bindCurrentSession(answer.session_id, answer.memory_id)",
        "session_id: currentSessionId",
        "W6 deliberately ignores lastReport.memory_id",
        'id="migrateLegacyBtn"',
        "selected && selected.read_only",
    ):
        assert required in source
    assert "setMemorySelection(lastReport ? lastReport.memory_id : null" not in source


class _LoopPolicy:
    def __call__(self, messages, *, tools=None):
        system = str(messages[0].get("content") or "")
        user = str(messages[-1].get("content") or "")
        if "Answer only from the supplied selected-Memory notes" in system:
            context = json.loads(user.split("MEMORY_CONTEXT_JSON:\n", 1)[1])
            path = next(
                (
                    hit["path"]
                    for hit in context["hits"]
                    if "externalfresh" in hit["summary"].lower()
                ),
                context["hits"][0]["path"],
            )
            return {
                "content": json.dumps(
                    {
                        "claims": [
                            {
                                "text": "The current selected-Memory note supports this answer.",
                                "source_paths": [path],
                            }
                        ],
                        "insufficient_evidence": [],
                    }
                )
            }
        if "Create a complete Markdown note" in system:
            contract = json.loads(
                user.split("FIXED_NOTE_CONTRACT_JSON:\n", 1)[1].split(
                    "\n\nMEMORY_ANSWER:", 1
                )[0]
            )
            fixed = contract["frontmatter"]
            sources = "\n".join(
                f"- [[{path[:-3]}]]" for path in contract["allowed_source_paths"]
            )
            markdown = (
                "---\n"
                f'id: {json.dumps(fixed["id"])}\n'
                f'type: {json.dumps(fixed["type"])}\n'
                f'memory_id: {json.dumps(fixed["memory_id"])}\n'
                'title: "Saved answer"\n'
                f'created_at: {json.dumps(fixed["created_at"])}\n'
                f'updated_at: {json.dumps(fixed["updated_at"])}\n'
                f'origin: {json.dumps(fixed["origin"])}\n'
                f'status: {json.dumps(fixed["status"])}\n'
                "tags:\n  - paperpilot\n"
                "---\n\n# Saved answer\n\nGrounded answer.\n\n"
                f"## Sources\n\n{sources}\n"
            )
            return {"content": json.dumps({"markdown": markdown})}
        if "before research begins" in system:
            return {
                "content": json.dumps(
                    {
                        "objective": "Extend the externally edited note",
                        "scope": ["selected Memory"],
                        "directions": ["Find one new source"],
                        "constraints": ["Keep sources locatable"],
                        "expected_output": "Markdown report",
                        "known_information": ["Externalfresh is already known."],
                        "research_gaps": ["One new source remains."],
                    }
                )
            }
        raise AssertionError("unexpected policy prompt")


@pytest.mark.asyncio
async def test_runtime_loop_rescans_external_edit_before_answer_and_research(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Vault"
    store = MarkdownMemoryStore(root)
    store.create_memory("Loop", "M-loop")
    source_path = "Memories/M-loop/notes/N-source.md"
    source = root / source_path
    source.write_text("# Source\n\nBaselinekey is known.\n", encoding="utf-8")
    runtime = build_research_runtime(
        {},
        policy=_LoopPolicy(),
        tools=[],
        memory_store=store,
    )
    try:
        first = await runtime.answer_memory("M-loop", "Baselinekey")
        before = {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }
        proposal = await runtime.propose_memory_note(first)
        assert proposal.markdown.startswith("---\n")
        assert "# Saved answer" in proposal.markdown
        assert {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        } == before
        committed = runtime.commit_memory_note(proposal)
        assert committed["memory_id"] == "M-loop"

        source.write_text(
            source.read_text(encoding="utf-8")
            + "\nExternalfresh was added outside PaperPilot.\n",
            encoding="utf-8",
        )
        second = await runtime.answer_memory("M-loop", "Externalfresh")
        assert second.memory_id == "M-loop"
        assert [item.relative_path for item in second.citations] == [source_path]

        paused = await runtime.start(
            "Continue Externalfresh research",
            thread_id="w6-loop",
            memory_id="M-loop",
        )
        brief = paused["__interrupt__"][0].value["brief"]
        assert brief["memory_id"] == "M-loop"
        assert source_path in brief["memory_paths"]
    finally:
        await runtime.close(shutdown=True)


def test_web_legacy_is_read_only_and_migration_is_previewed_then_explicit(
    bound_web_client,
) -> None:
    client, _ = bound_web_client
    runtime = server.get_research_runtime()
    source = runtime.memory_store.root / "reports" / "Report-old.md"
    original = source.read_bytes()

    options = {item["memory_id"]: item for item in client.get("/api/memories").json()}
    assert options[LEGACY_MEMORY_ID]["read_only"] is True
    assert options[LEGACY_MEMORY_ID]["home_relative_path"] is None

    answer = client.post(
        f"/api/memories/{LEGACY_MEMORY_ID}/answers",
        json={"session_id": "legacy-session", "question": "Known?"},
    )
    assert answer.status_code == 200
    assert answer.json()["citations"][0]["relative_path"] == "reports/Report-old.md"
    assert client.post(
        "/api/alignment",
        json={
            "session_id": "legacy-session",
            "memory_id": LEGACY_MEMORY_ID,
            "message": "Research",
        },
    ).status_code == 409
    assert client.post(
        f"/api/memories/{LEGACY_MEMORY_ID}/import-proposals",
        json={
            "session_id": "legacy-session",
            "kind": "text",
            "title": "No write",
            "text": "content",
        },
    ).status_code == 409

    preview = client.post(
        "/api/legacy-memory/migration-proposals",
        json={"title": "Migrated", "target_memory_id": "M-from-legacy"},
    )
    assert preview.status_code == 200
    proposal = preview.json()
    assert proposal["status"] == "proposal"
    assert proposal["files"][0]["markdown"]
    assert source.read_bytes() == original
    assert not (runtime.memory_store.root / "Memories" / "M-from-legacy").exists()

    confirmed = client.post(
        f"/api/legacy-memory/migration-proposals/{proposal['proposal_id']}/confirm"
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["memory_id"] == "M-from-legacy"
    assert source.read_bytes() == original
    assert runtime.memory_store.get_memory("M-from-legacy").title == "Migrated"
    sessions = {item["session_id"]: item for item in client.get("/api/sessions").json()}
    assert sessions["legacy-session"]["memory_id"] == LEGACY_MEMORY_ID


class _CliRuntime:
    def __init__(self) -> None:
        self.commits = 0

    def get_memory(self, memory_id: str):
        if memory_id != "M-alpha":
            raise FileNotFoundError(memory_id)
        return SimpleNamespace(memory_id=memory_id)

    def list_memories(self):
        return (SimpleNamespace(memory_id="M-alpha", title="Alpha"),)

    async def answer_memory(self, memory_id: str, question: str) -> MemoryAnswer:
        return MemoryAnswer("A-1", memory_id, question, "Answer", (), ())

    async def propose_memory_note(self, answer: MemoryAnswer) -> MemoryNoteProposal:
        return MemoryNoteProposal(
            "P-1", answer.answer_id, answer.memory_id, "N-1", "Note",
            "Memories/M-alpha/notes/N-1.md", "# Note\n", "[[Memories/M-alpha/notes/N-1]]",
            (), "Memories/M-alpha/Home.md", "0" * 64, None, "# Home\n",
        )

    def commit_memory_note(self, proposal: MemoryNoteProposal):
        self.commits += 1
        return {
            "wikilink": proposal.wikilink,
            "target_path": proposal.target_path,
        }

    def commit_memory_import(self, proposal: MemoryImportProposal):
        self.commits += 1
        return {"import_path": proposal.import_path}

    async def close(self, *, shutdown: bool = False) -> None:
        return None


@pytest.mark.asyncio
async def test_cli_question_and_import_preview_require_confirmation() -> None:
    runtime = _CliRuntime()
    outputs: list[str] = []
    answers = iter(["y", "n"])
    await run_memory_question(
        runtime,  # type: ignore[arg-type]
        "M-alpha",
        "Question",
        input_fn=lambda _prompt: next(answers),
        output_fn=outputs.append,
    )
    assert runtime.commits == 0
    assert any("Memory note preview" in item for item in outputs)

    proposal = MemoryImportProposal(
        "IP-1", "I-1", "N-2", "M-alpha", "text", "source", "lines:1-1",
        "text/plain", 1, "0" * 64, "Memories/M-alpha/attachments/Asset.txt", b"x",
        "Memories/M-alpha/imports/I-1.md", "# Import\n", "[[Memories/M-alpha/imports/I-1]]",
        "Memories/M-alpha/notes/N-2.md", "# Note\n", "[[Memories/M-alpha/notes/N-2]]",
        (), "Memories/M-alpha/Home.md", "1" * 64, "# Home\n",
    )
    confirm_memory_import(
        runtime,  # type: ignore[arg-type]
        proposal,
        input_fn=lambda _prompt: "y",
        output_fn=outputs.append,
    )
    assert runtime.commits == 1
    assert any("Memory import preview" in item for item in outputs)


@pytest.mark.asyncio
async def test_repl_selects_and_questions_through_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _CliRuntime()
    values = iter(["memories", "use M-alpha", "ask Known?", "n", "q"])
    output: list[str] = []
    monkeypatch.setattr(run_repl, "load_config", lambda _path: {})
    monkeypatch.setattr(run_repl, "build_research_runtime", lambda **_kwargs: runtime)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(values))
    monkeypatch.setattr(
        "builtins.print",
        lambda *items, **_kwargs: output.append(" ".join(map(str, items))),
    )
    await run_repl._repl(
        SimpleNamespace(config=None, session_id="cli", memory_id=None)
    )
    assert any("M-alpha  Alpha" in item for item in output)
    assert any("Selected Memory: M-alpha" in item for item in output)
    assert any("Memory answer (M-alpha)" in item for item in output)


@pytest.mark.asyncio
async def test_repl_answers_legacy_read_only_and_migrates_without_switching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LegacyCliRuntime:
        def __init__(self, root: Path) -> None:
            self.memory_store = MarkdownMemoryStore(root)
            report = root / "reports" / "Report-old.md"
            report.parent.mkdir(parents=True)
            report.write_text("# Old\n\nLegacybaseline.\n", encoding="utf-8")

        def get_memory_option(self, memory_id: str):
            if memory_id == LEGACY_MEMORY_ID:
                return {
                    "memory_id": memory_id,
                    "title": "Existing Memory (read-only)",
                    "read_only": True,
                    "can_migrate": True,
                }
            descriptor = self.memory_store.get_memory(memory_id)
            return {
                "memory_id": descriptor.memory_id,
                "title": descriptor.title,
                "read_only": False,
                "can_migrate": False,
            }

        def list_memory_options(self):
            return (self.get_memory_option(LEGACY_MEMORY_ID),)

        async def answer_memory(self, memory_id: str, question: str):
            return MemoryAnswer(
                "A-legacy",
                memory_id,
                question,
                "Grounded [[reports/Report-old]]",
                (
                    MemoryCitation(
                        "reports/Report-old.md",
                        "Old",
                        "[[reports/Report-old]]",
                    ),
                ),
                (),
            )

        def prepare_legacy_memory_migration(self, title: str, memory_id: str):
            return self.memory_store.prepare_legacy_memory_migration(title, memory_id)

        def commit_legacy_memory_migration(self, proposal):
            return self.memory_store.commit_legacy_memory_migration(proposal)

        async def close(self, *, shutdown: bool = False):
            return None

    runtime = LegacyCliRuntime(tmp_path / "Vault")
    values = iter(
        [
            "memories",
            "use M-legacy",
            "ask What is known?",
            "migrate-legacy M-copy Copy",
            "y",
            "q",
        ]
    )
    output: list[str] = []
    monkeypatch.setattr(run_repl, "load_config", lambda _path: {})
    monkeypatch.setattr(run_repl, "build_research_runtime", lambda **_kwargs: runtime)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(values))
    monkeypatch.setattr(
        "builtins.print",
        lambda *items, **_kwargs: output.append(" ".join(map(str, items))),
    )

    await run_repl._repl(SimpleNamespace(config=None, session_id="cli", memory_id=None))

    assert any("M-legacy" in item and "read-only" in item for item in output)
    assert any("This Memory is read-only" in item for item in output), output
    assert any("Legacy Memory migration preview" in item for item in output)
    assert runtime.memory_store.get_memory("M-copy").title == "Copy"
    assert (runtime.memory_store.root / "reports" / "Report-old.md").is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("memory_id", "message"),
    [
        (None, "select a Memory"),
        (LEGACY_MEMORY_ID, "read-only"),
    ],
)
async def test_single_run_requires_explicit_writable_managed_memory(
    memory_id: str | None,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Runtime:
        def __init__(self) -> None:
            self.closed = False

        def get_memory_option(self, value: str):
            if value != LEGACY_MEMORY_ID:
                raise FileNotFoundError(value)
            return {"memory_id": value, "read_only": True}

        async def close(self, *, shutdown: bool = False) -> None:
            self.closed = shutdown

    runtime = Runtime()
    monkeypatch.setattr(run_single, "load_config", lambda _path: {"research": {}})
    monkeypatch.setattr(
        run_single,
        "build_research_runtime",
        lambda **_kwargs: runtime,
    )
    args = SimpleNamespace(
        config=None,
        query="Question",
        thread_id="single-explicit-memory",
        memory_id=memory_id,
        yes=True,
    )

    with pytest.raises(ValueError, match=message):
        await run_single._run(args)

    assert runtime.closed is True
