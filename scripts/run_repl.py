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
    format_result_locations,
    report_path,
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
        "  ls               list reports completed in this shell session\n"
        "  help             show this help\n"
        "  q / quit / exit  exit"
    )


async def _repl(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    vault_name = vault_name_from_config(config)
    runtime = build_research_runtime(config=config)
    session_id = args.session_id or f"session-{datetime.now():%Y%m%d-%H%M%S}"
    completed: list[Path] = []
    print(f"PaperPilot session: {session_id}")
    print("Each question uses a new root thread. Enter 'help' for commands.")

    try:
        while True:
            try:
                query = input(f"[{session_id}] > ").strip()
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

            thread_id = _new_root_thread(runtime, session_id)
            try:
                result = await run_reviewed_workflow(
                    runtime,
                    query,
                    thread_id=thread_id,
                    memory_id=getattr(args, "memory_id", None),
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
