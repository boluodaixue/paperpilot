#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/run_single.py
================================================================================
DeepResearch Agent 单条查询运行脚本。

Usage:
    python scripts/run_single.py --query "你的研究问题" [--config path/to/config.yaml]
================================================================================
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from src.core.runner import initialize_modules, load_config, run_research, save_report, setup_logging


def main() -> None:
    parser = argparse.ArgumentParser(
        description="DeepResearch Agent 单条查询运行脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/run_single.py --query "分析 2024 年 AI 芯片市场格局"
  python scripts/run_single.py --query "量子计算在密码学中的应用" --config configs/custom.yaml
        """,
    )
    parser.add_argument("--query", type=str, required=True, help="用户的研究问题（必填）")
    parser.add_argument("--config", type=str, default=None, help="自定义配置文件路径")
    parser.add_argument("--output_dir", type=str, default="outputs/reports", help="报告输出目录")
    parser.add_argument("--session_id", type=str, default="", help="会话 ID（用于 memory store 隔离）")
    parser.add_argument("--log_level", type=str, default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    # 将终端输出同时保存到报告目录下的日志文件
    import datetime as _dt
    os.makedirs(args.output_dir, exist_ok=True)
    log_filename = os.path.join(
        args.output_dir,
        f"run_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    )

    class Tee:
        """同时输出到终端和文件的包装器。"""
        def __init__(self, terminal, file):
            self.terminal = terminal
            self.file = file
        def write(self, message):
            self.terminal.write(message)
            self.file.write(message)
            self.file.flush()
        def flush(self):
            self.terminal.flush()
            self.file.flush()
        def isatty(self):
            return self.terminal.isatty()

    log_file = open(log_filename, "w", encoding="utf-8")
    sys.stdout = Tee(sys.stdout, log_file)
    sys.stderr = Tee(sys.stderr, log_file)
    print(f"[日志] 终端输出已同时保存到: {log_filename}")

    setup_logging(args.log_level)
    logger = logging.getLogger("main")

    try:
        config = load_config(args.config)
        logger.info(f"配置加载完成: {args.config or 'configs/default.yaml'}")

        modules = initialize_modules(config, session_id=args.session_id)
        report = asyncio.run(run_research(args.query, config, modules))

        filepath = save_report(report, args.query, args.output_dir)
        logger.info(f"报告已保存: {filepath}")

        print("\n" + "=" * 60)
        print("最终研究报告")
        print("=" * 60)
        print(report)
        print("=" * 60)

    except Exception as e:
        logger.exception("运行过程中发生错误")
        sys.exit(1)


if __name__ == "__main__":
    main()
