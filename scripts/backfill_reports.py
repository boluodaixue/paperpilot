#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/backfill_reports.py
================================================================================
一次性回填：把 outputs/reports/*.md 的历史报告导入 chat_messages，
让 Web UI 侧边栏能回溯旧会话（会话消息持久化功能上线前的报告）。

用法：
    python scripts/backfill_reports.py [--sessions smoke-6 smoke-7]
    （不带参数则自动按 query 匹配所有缺失报告的会话）
================================================================================
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def sanitize(q: str, n: int = 20) -> str:
    """与 run_single.save_report 相同的文件名清洗逻辑。"""
    return "".join(c if c.isalnum() or c in "_-" else "_" for c in q[:n])


def main() -> None:
    parser = argparse.ArgumentParser(description="历史报告回填到会话消息")
    parser.add_argument("--sessions", nargs="*", default=[], help="只回填指定 session（默认自动匹配全部）")
    parser.add_argument("--db", default="data/memory.db", help="数据库路径")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # 1. 找出有证据但聊天里没有 report 的会话
    have_report = {
        r["session_id"]
        for r in conn.execute(
            "SELECT DISTINCT session_id FROM chat_messages WHERE kind='report'"
        )
    }
    targets = {}
    for r in conn.execute("SELECT session_id, query FROM evidence GROUP BY session_id"):
        if r["session_id"] in have_report:
            continue
        if args.sessions and r["session_id"] not in args.sessions:
            continue
        targets[r["session_id"]] = r["query"]

    if not targets:
        print("没有需要回填的会话。")
        return

    report_dir = PROJECT_ROOT / "outputs" / "reports"
    files = sorted(report_dir.glob("report_*.md"))

    for sid, query in sorted(targets.items()):
        key = sanitize(query)
        candidates = [f for f in files if key in f.name or sanitize(query, 25) in f.name]
        if not candidates:
            print(f"[跳过] {sid}（query={query[:30]}...）无匹配报告文件")
            continue
        latest = candidates[-1]  # 文件名含时间戳，已排序
        content = latest.read_text(encoding="utf-8")

        # 生成 message_id（会话内递增）
        rows = conn.execute(
            "SELECT message_id FROM chat_messages WHERE session_id=?", (sid,)
        ).fetchall()
        max_seq = 0
        for (mid,) in rows:
            m = re.match(r"M-(\d+)", str(mid))
            if m:
                max_seq = max(max_seq, int(m.group(1)))
        mid = f"M-{max_seq + 1}"

        import time

        conn.execute(
            "INSERT INTO chat_messages (session_id, message_id, role, kind, content, timestamp) "
            "VALUES (?, ?, 'assistant', 'report', ?, ?)",
            (sid, mid, content, time.time()),
        )
        print(f"[回填] {sid} ← {latest.name}（{len(content)} 字符）")

    conn.commit()
    conn.close()
    print("完成。")


if __name__ == "__main__":
    main()
