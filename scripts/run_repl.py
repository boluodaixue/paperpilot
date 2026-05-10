#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/run_repl.py
================================================================================
DeepResearch Agent 交互式 REPL 单会话脚本。

功能：
  1. 启动时列出已有 sessions，支持新建或继承
  2. 在单个进程内连续提问，共享同一个 Orchestrator + Memory Store
  3. 所有数据按 session 隔离存储于 SQLite
  4. 输入 q/quit/exit 退出，Ctrl+C 优雅中断

Usage:
    python scripts/run_repl.py [--config path/to/config.yaml]
================================================================================
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.runner import initialize_modules, load_config, run_research, save_report, setup_logging
from src.memory.memory_store import SharedMemoryStore


def list_sessions(db_path: str) -> list[dict]:
    """列出数据库中所有 session。"""
    if not os.path.exists(db_path):
        return []
    store = SharedMemoryStore(db_path=db_path, session_id="")
    return store.list_sessions()


def print_help() -> None:
    print("""
可用命令:
  <任意问题>   执行深度研究
  ls          查看当前 session 已存储的记忆数
  sessions    查看所有 session 列表
  save        保存上一条报告到文件
  help        显示此帮助
  q / quit / exit  退出 REPL
""")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="DeepResearch Agent 交互式 REPL",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", type=str, default=None, help="配置文件路径")
    parser.add_argument("--log_level", type=str, default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--session_id", type=str, default=None, help="直接指定 session_id，跳过交互选择")
    args = parser.parse_args()

    setup_logging(args.log_level)
    logger = logging.getLogger("repl")

    config = load_config(args.config)
    db_path = config.get("memory", {}).get("db_path", "data/memory.db")

    # ------------------------------------------------------------------
    # Session 选择
    # ------------------------------------------------------------------
    if args.session_id:
        session_id = args.session_id
        print(f"[REPL] 已指定 session: {session_id}")
    else:
        sessions = list_sessions(db_path)

        print("=" * 50)
        print("DeepResearch Agent 交互式 REPL")
        print("=" * 50)

        if sessions:
            print("\n已有 Sessions:")
            for i, s in enumerate(sessions, 1):
                ts = datetime.fromtimestamp(s["last_update"]).strftime("%Y-%m-%d %H:%M")
                print(f"  [{i}] {s['session_id']:25s} ({s['count']:3d} 条记忆, 最后更新 {ts})")
            print("  [N] 新建 session")
            choice = input("\n选择 (编号或 N): ").strip()
            if choice.lower() == "n":
                session_id = f"sess_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            else:
                try:
                    idx = int(choice) - 1
                    session_id = sessions[idx]["session_id"]
                except (ValueError, IndexError):
                    print("无效选择，新建 session")
                    session_id = f"sess_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        else:
            print("\n暂无历史 session，新建一个...")
            session_id = f"sess_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        print(f"\n[REPL] 当前 session: {session_id}")

    # ------------------------------------------------------------------
    # 初始化模块（只初始化一次，整个 REPL 生命周期复用）
    # ------------------------------------------------------------------
    print("[REPL] 正在初始化模块...")
    modules = initialize_modules(config, session_id=session_id)
    print(f"[REPL] 模块初始化完成，输入 'help' 查看命令，'q' 退出\n")

    last_report: str | None = None

    # ------------------------------------------------------------------
    # REPL 循环
    # ------------------------------------------------------------------
    while True:
        try:
            query = input(f"[{session_id}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[REPL] 收到中断信号，退出...")
            break

        if not query:
            continue

        cmd = query.lower()

        if cmd in ("q", "quit", "exit"):
            break
        elif cmd == "help":
            print_help()
            continue
        elif cmd == "ls":
            count = len(modules["memory_store"])
            print(f"  当前 session '{session_id}' 共有 {count} 条记忆")
            continue
        elif cmd == "sessions":
            all_sessions = list_sessions(db_path)
            if not all_sessions:
                print("  暂无 session")
            else:
                for s in all_sessions:
                    ts = datetime.fromtimestamp(s["last_update"]).strftime("%Y-%m-%d %H:%M")
                    marker = " <- 当前" if s["session_id"] == session_id else ""
                    print(f"  {s['session_id']:25s} ({s['count']:3d} 条) {ts}{marker}")
            continue
        elif cmd == "save":
            if last_report:
                filepath = save_report(last_report, "repl_report", "outputs/reports")
                print(f"  报告已保存: {filepath}")
            else:
                print("  暂无报告可保存")
            continue

        # ------------------------------------------------------------------
        # 执行深度研究
        # ------------------------------------------------------------------
        print(f"[REPL] 正在研究: {query[:60]}...")
        start = time.time()
        try:
            report = asyncio.run(run_research(query, config, modules))
            elapsed = time.time() - start
            last_report = report

            # 解析元信息（从报告尾部提取）
            confidence = 0.0
            num_searches = 0
            for line in report.splitlines():
                if "**置信度**:" in line:
                    try:
                        confidence = float(line.split(":")[-1].strip())
                    except ValueError:
                        pass
                if "**搜索轮数**:" in line:
                    try:
                        num_searches = int(line.split(":")[-1].strip())
                    except ValueError:
                        pass

            print(f"\n  ✓ 报告完成 | {len(report)} 字 | 置信度 {confidence:.2f} | "
                  f"搜索 {num_searches} 轮 | 耗时 {elapsed:.1f}s")
            print(f"  输入 'save' 保存报告，'ls' 查看当前 session 记忆数\n")

        except Exception as e:
            logger.exception("研究执行失败")
            print(f"\n  ✗ 执行失败: {e}\n")

    # ------------------------------------------------------------------
    # 退出清理
    # ------------------------------------------------------------------
    print(f"\n[REPL] Session '{session_id}' 的数据已持久化到 {db_path}")
    print("[REPL] 下次运行可用 --session_id 参数直接继承，或在交互菜单中选择。")
    print("[REPL] 再见！")


if __name__ == "__main__":
    main()
