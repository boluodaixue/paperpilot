#!/usr/bin/env python3
"""Interactive PaperPilot shell; every question starts an independent root thread."""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts._workflow_cli import (
    UserCancelled,
    confirm_legacy_memory_migration,
    confirm_memory_import,
    format_result_locations,
    report_path,
    require_memory,
    run_memory_question,
    run_reviewed_workflow,
    vault_name_from_config,
)
from src.research.runtime import build_research_runtime, load_config, setup_logging


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
        "  ls               list reports completed in this shell session\n"
        "  help             show this help\n"
        "  q / quit / exit  exit"
    )


async def _repl(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    vault_name = vault_name_from_config(config)
    runtime = build_research_runtime(config=config)
    session_id = args.session_id or f"session-{datetime.now():%Y%m%d-%H%M%S}"
    selected_memory_id = getattr(args, "memory_id", None)
    completed: list[Path] = []
    print(f"PaperPilot session: {session_id}")
    print("Each question uses a new root thread. Enter 'help' for commands.")

    try:
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
                    proposal = runtime.prepare_legacy_memory_migration(
                        title,
                        target_memory_id,
                    )
                    confirm_legacy_memory_migration(
                        runtime,
                        proposal,
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
                    proposal = await runtime.prepare_memory_file_import(
                        memory_id, path.name, path.read_bytes()
                    )
                    confirm_memory_import(
                        runtime, proposal, input_fn=input, output_fn=print
                    )
                except Exception as exc:
                    print(f"Memory import failed: {exc}")
                continue

            if command.startswith("import-url "):
                try:
                    memory_id = require_memory(runtime, selected_memory_id)
                    proposal = await runtime.prepare_memory_url_import(
                        memory_id, query.split(None, 1)[1].strip()
                    )
                    confirm_memory_import(
                        runtime, proposal, input_fn=input, output_fn=print
                    )
                except Exception as exc:
                    print(f"Memory import failed: {exc}")
                continue

            if command.startswith("import-text "):
                try:
                    memory_id = require_memory(runtime, selected_memory_id)
                    title = query.split(None, 1)[1].strip()
                    text = input("Text: ")
                    proposal = await runtime.prepare_memory_text_import(
                        memory_id, title, text
                    )
                    confirm_memory_import(
                        runtime, proposal, input_fn=input, output_fn=print
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
    finally:
        await runtime.close(shutdown=True)


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
