#!/usr/bin/env python3
"""Run one PaperPilot research workflow with an explicit brief review."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts._workflow_cli import (
    UserCancelled,
    format_result_locations,
    require_memory,
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
from src.research.runtime_registry import RuntimeRegistry


_DEFAULT_BUILD_RESEARCH_RUNTIME = build_research_runtime


def _checkpoint_db_path(config: dict) -> Path:
    configured = Path(str(config.get("chat", {}).get("db_path", "data/chat.db")))
    return configured if configured.is_absolute() else PROJECT_ROOT / configured


@asynccontextmanager
async def _open_runtime(config: dict) -> AsyncIterator[ResearchRuntime]:
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


async def _run(args: argparse.Namespace) -> str:
    config = load_config(args.config)
    vault_name = vault_name_from_config(config)
    async with _open_runtime(config) as runtime:
        thread_id = args.thread_id or runtime.new_thread_id()
        session_id = f"cli-single-{thread_id}"
        registry: RuntimeRegistry | None = None
        if build_research_runtime is _DEFAULT_BUILD_RESEARCH_RUNTIME:
            database = _checkpoint_db_path(config)
            chat_store = ChatStore(str(database))
            chat_store.set_meta(session_id, title=session_id)
            registry = RuntimeRegistry(database)
        memory_id = require_memory(
            runtime,
            getattr(args, "memory_id", None),
        )
        result = await run_reviewed_workflow(
            runtime,
            args.query,
            thread_id=thread_id,
            memory_id=memory_id,
            session_id=session_id,
            registry=registry,
            auto_confirm=args.yes,
        )
        return format_result_locations(
            runtime,
            result,
            vault_name=vault_name,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one PaperPilot research workflow")
    parser.add_argument("--query", required=True, help="Research question")
    parser.add_argument("--config", default=None, help="Configuration file")
    parser.add_argument("--thread-id", default=None, help="Optional root thread identity")
    parser.add_argument(
        "--memory-id",
        required=True,
        help="Required explicit managed Memory ID for this research result",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the generated brief automatically (for automation)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()
    setup_logging(args.log_level)

    try:
        locations = asyncio.run(_run(args))
    except UserCancelled:
        logging.getLogger("run_single").info("Research cancelled before confirmation")
        raise SystemExit(2) from None
    except Exception:
        logging.getLogger("run_single").exception("Research workflow failed")
        raise SystemExit(1) from None

    print(locations)


if __name__ == "__main__":
    main()
