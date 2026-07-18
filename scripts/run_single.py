#!/usr/bin/env python3
"""Run one PaperPilot research workflow with an explicit brief review."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts._workflow_cli import UserCancelled, report_path, run_reviewed_workflow
from src.research.runtime import build_research_runtime, load_config, setup_logging


async def _run(args: argparse.Namespace) -> Path:
    config = load_config(args.config)
    runtime = build_research_runtime(config=config)
    thread_id = args.thread_id or runtime.new_thread_id()
    try:
        result = await run_reviewed_workflow(
            runtime,
            args.query,
            thread_id=thread_id,
            auto_confirm=args.yes,
        )
        return report_path(runtime, result)
    finally:
        await runtime.close(shutdown=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one PaperPilot research workflow")
    parser.add_argument("--query", required=True, help="Research question")
    parser.add_argument("--config", default=None, help="Configuration file")
    parser.add_argument("--thread-id", default=None, help="Optional root thread identity")
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
        manifest_path = asyncio.run(_run(args))
    except UserCancelled:
        logging.getLogger("run_single").info("Research cancelled before confirmation")
        raise SystemExit(2) from None
    except Exception:
        logging.getLogger("run_single").exception("Research workflow failed")
        raise SystemExit(1) from None

    # The workflow already persisted the report; expose its canonical manifest path only.
    print(manifest_path)


if __name__ == "__main__":
    main()
