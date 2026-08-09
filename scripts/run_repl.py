#!/usr/bin/env python3
"""Interactive PaperPilot shell; every question starts an independent root thread."""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections.abc import Mapping
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts._workflow_cli import (
    UserCancelled,
    format_result_locations,
    reconcile_cli_workflow,
    report_path,
    require_memory,
    run_memory_question,
    run_legacy_migration_workflow,
    run_memory_import_workflow,
    run_reviewed_workflow,
    vault_name_from_config,
)
from src.memory.chat_store import ChatStore
from src.research.runtime import (
    ResearchRuntime,
    build_research_runtime,
    load_config,
    open_research_runtime,
    setup_logging,
)
from src.research.runtime_registry import RuntimeRegistry, WorkflowRecord


_DEFAULT_BUILD_RESEARCH_RUNTIME = build_research_runtime


def _checkpoint_db_path(config: dict[str, Any]) -> Path:
    configured = Path(str(config.get("chat", {}).get("db_path", "data/chat.db")))
    return configured if configured.is_absolute() else PROJECT_ROOT / configured


@asynccontextmanager
async def _open_runtime(config: dict[str, Any]) -> AsyncIterator[ResearchRuntime]:
    """Own the product saver while retaining the established test injection seam."""
    if build_research_runtime is not _DEFAULT_BUILD_RESEARCH_RUNTIME:
        runtime = build_research_runtime(config=config)
        try:
            yield runtime
        finally:
            await runtime.close(shutdown=True)
        return

    async with open_research_runtime(
        _checkpoint_db_path(config),
        config=config,
    ) as runtime:
        yield runtime


def _session_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")
    return slug or "session"


def _new_root_thread(runtime: Any, session_id: str) -> str:
    # The prefix groups runs for operators only; no graph state or research context is shared.
    return f"{_session_slug(session_id)}--{runtime.new_thread_id()}"


def print_help() -> None:
    print(
        "Commands:\n"
        "  <question>       start an independent research workflow\n"
        "  memories         list managed Memories\n"
        "  use <memory-id>  select a Memory explicitly\n"
        "  new-memory <title> create and select a Memory\n"
        "  ask <question>   answer from the selected Memory, with optional note save\n"
        "  import-file <path> preview and optionally import a PDF/text file\n"
        "  import-url <url> preview and optionally import an explicit URL\n"
        "  import-text <title> paste one text value, then preview the import\n"
        "  migrate-legacy <target-id> <title> preview and publish a managed copy\n"
        "  workflows        list checkpointed workflows for this CLI session\n"
        "  resume <id>      resume one checkpointed workflow\n"
        "  ls               list reports completed in this shell session\n"
        "  help             show this help\n"
        "  q / quit / exit  exit"
    )


async def _print_workflows(
    runtime: ResearchRuntime,
    registry: RuntimeRegistry,
    session_id: str,
) -> tuple[WorkflowRecord, ...]:
    records = registry.list(session_id=session_id)
    if not records:
        print("No checkpointed workflows in this session.")
        return records
    print("Checkpointed workflows:")
    for record in records:
        _snapshot, status = await reconcile_cli_workflow(
            runtime, registry, record
        )
        print(
            f"  {record.thread_id}  {record.workflow_type}  "
            f"{status}  {record.memory_id}"
        )
    return records


async def _resume_workflow(
    runtime: ResearchRuntime,
    registry: RuntimeRegistry,
    record: WorkflowRecord,
) -> Any:
    snapshot, status = await reconcile_cli_workflow(runtime, registry, record)
    values = dict(snapshot.values)
    if status != "waiting_confirmation":
        raise ValueError(f"workflow is not waiting for confirmation: {status}")
    if record.workflow_type == "research":
        return await run_reviewed_workflow(
            runtime,
            str(values.get("question") or ""),
            thread_id=record.thread_id,
            memory_id=record.memory_id,
            session_id=record.session_id,
            registry=registry,
        )
    if record.workflow_type == "memory_note":
        return await run_memory_question(
            runtime,
            record.memory_id,
            str(values.get("question") or ""),
            session_id=record.session_id,
            workflow_id=record.thread_id,
            registry=registry,
        )
    if record.workflow_type == "memory_import":
        source = values.get("source")
        if not isinstance(source, Mapping):
            raise ValueError("checkpointed import source is invalid")
        return await run_memory_import_workflow(
            runtime,
            record.memory_id,
            source,
            session_id=record.session_id,
            workflow_id=record.thread_id,
            registry=registry,
        )
    if record.workflow_type == "legacy_migration":
        return await run_legacy_migration_workflow(
            runtime,
            str(values.get("target_memory_id") or ""),
            str(values.get("title") or ""),
            session_id=record.session_id,
            workflow_id=record.thread_id,
            registry=registry,
        )
    raise ValueError(f"unsupported workflow type: {record.workflow_type}")


async def _repl(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    vault_name = vault_name_from_config(config)
    session_id = args.session_id or f"session-{datetime.now():%Y%m%d-%H%M%S}"
    selected_memory_id = getattr(args, "memory_id", None)
    completed: list[Path] = []
    registry: RuntimeRegistry | None = None
    if build_research_runtime is _DEFAULT_BUILD_RESEARCH_RUNTIME:
        database = _checkpoint_db_path(config)
        chat_store = ChatStore(str(database))
        chat_store.set_meta(session_id, title=session_id)
        registry = RuntimeRegistry(database)
    print(f"PaperPilot session: {session_id}")
    print("Each question uses a new root thread. Enter 'help' for commands.")

    async with _open_runtime(config) as runtime:
        if registry is not None and registry.list(session_id=session_id):
            print("This session has checkpointed workflows; use 'workflows' or 'resume <id>'.")
        while True:
            try:
                memory_label = selected_memory_id or "none"
                query = input(f"[{session_id} | {memory_label}] > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not query:
                continue
            command = query.lower()
            if command in {"q", "quit", "exit"}:
                break
            if command == "help":
                print_help()
                continue
            if command == "ls":
                if completed:
                    print("\n".join(str(path) for path in completed))
                else:
                    print("No completed reports in this session.")
                continue
            if command == "workflows":
                if registry is None:
                    print("Workflow registry is unavailable for this injected Runtime.")
                else:
                    await _print_workflows(runtime, registry, session_id)
                continue

            if command.startswith("resume "):
                workflow_id = query.split(None, 1)[1].strip()
                try:
                    if registry is None:
                        raise ValueError("workflow registry is unavailable")
                    record = registry.get(workflow_id)
                    if record is None or record.session_id != session_id:
                        raise ValueError("workflow is not registered to this CLI session")
                    await _resume_workflow(runtime, registry, record)
                except UserCancelled:
                    print("Workflow cancelled.")
                except Exception as exc:
                    print(f"Workflow resume failed: {exc}")
                continue

            if command == "memories":
                items = (
                    runtime.list_memory_options()
                    if hasattr(runtime, "list_memory_options")
                    else tuple(
                        {
                            "memory_id": item.memory_id,
                            "title": item.title,
                            "read_only": False,
                        }
                        for item in runtime.list_memories()
                    )
                )
                if not items:
                    print("No Memories.")
                for item in items:
                    memory_id = item["memory_id"]
                    marker = "*" if memory_id == selected_memory_id else " "
                    suffix = "  [read-only]" if item.get("read_only") else ""
                    print(f"{marker} {memory_id}  {item['title']}{suffix}")
                continue

            if command.startswith("use "):
                candidate = query.split(None, 1)[1].strip()
                try:
                    selected_memory_id = require_memory(
                        runtime,
                        candidate,
                        writable=False,
                    )
                    print(f"Selected Memory: {selected_memory_id}")
                except (FileNotFoundError, ValueError) as exc:
                    print(f"Memory selection failed: {exc}")
                continue

            if command.startswith("migrate-legacy "):
                try:
                    _, target_memory_id, title = query.split(None, 2)
                    await run_legacy_migration_workflow(
                        runtime,
                        target_memory_id,
                        title,
                        session_id=session_id,
                        registry=registry,
                        input_fn=input,
                        output_fn=print,
                    )
                except ValueError as exc:
                    print(
                        "Legacy migration failed: use "
                        "migrate-legacy <target-id> <title>; "
                        f"{exc}"
                    )
                except Exception as exc:
                    print(f"Legacy migration failed: {exc}")
                continue

            if command.startswith("new-memory "):
                title = query.split(None, 1)[1].strip()
                try:
                    descriptor = runtime.create_memory(title)
                    selected_memory_id = descriptor.memory_id
                    print(f"Created and selected Memory: {selected_memory_id}")
                except (FileExistsError, ValueError) as exc:
                    print(f"Memory creation failed: {exc}")
                continue

            if command.startswith("ask "):
                try:
                    await run_memory_question(
                        runtime,
                        require_memory(
                            runtime,
                            selected_memory_id,
                            writable=False,
                        ),
                        query.split(None, 1)[1].strip(),
                        session_id=session_id,
                        registry=registry,
                        input_fn=input,
                        output_fn=print,
                    )
                except Exception as exc:
                    print(f"Memory question failed: {exc}")
                continue

            if command.startswith("import-file "):
                try:
                    memory_id = require_memory(runtime, selected_memory_id)
                    path = Path(query.split(None, 1)[1].strip().strip('"'))
                    await run_memory_import_workflow(
                        runtime,
                        memory_id,
                        {"kind": "file", "file_name": path.name, "content": path.read_bytes()},
                        session_id=session_id,
                        registry=registry,
                        input_fn=input,
                        output_fn=print,
                    )
                except Exception as exc:
                    print(f"Memory import failed: {exc}")
                continue

            if command.startswith("import-url "):
                try:
                    memory_id = require_memory(runtime, selected_memory_id)
                    await run_memory_import_workflow(
                        runtime,
                        memory_id,
                        {"kind": "url", "url": query.split(None, 1)[1].strip()},
                        session_id=session_id,
                        registry=registry,
                        input_fn=input,
                        output_fn=print,
                    )
                except Exception as exc:
                    print(f"Memory import failed: {exc}")
                continue

            if command.startswith("import-text "):
                try:
                    memory_id = require_memory(runtime, selected_memory_id)
                    title = query.split(None, 1)[1].strip()
                    text = input("Text: ")
                    await run_memory_import_workflow(
                        runtime,
                        memory_id,
                        {"kind": "text", "title": title, "text": text},
                        session_id=session_id,
                        registry=registry,
                        input_fn=input,
                        output_fn=print,
                    )
                except Exception as exc:
                    print(f"Memory import failed: {exc}")
                continue

            thread_id = _new_root_thread(runtime, session_id)
            try:
                selected_memory_id = require_memory(
                    runtime,
                    selected_memory_id,
                )
                result = await run_reviewed_workflow(
                    runtime,
                    query,
                    thread_id=thread_id,
                    memory_id=selected_memory_id,
                    session_id=session_id,
                    registry=registry,
                )
            except UserCancelled:
                print("Research cancelled.")
                continue
            except Exception as exc:
                print(f"Research failed: {exc}")
                continue

            path = report_path(runtime, result)
            completed.append(path)
            print(format_result_locations(runtime, result, vault_name=vault_name))


def main() -> None:
    parser = argparse.ArgumentParser(description="PaperPilot interactive research shell")
    parser.add_argument("--config", default=None, help="Configuration file")
    parser.add_argument("--session-id", default=None, help="Operator grouping label")
    parser.add_argument(
        "--memory-id",
        default=None,
        help="Optional existing Memory ID for research in this session",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()
    setup_logging(args.log_level)
    asyncio.run(_repl(args))


if __name__ == "__main__":
    main()
