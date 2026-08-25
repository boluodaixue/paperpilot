#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
web/run.py
================================================================================
启动 PaperPilot Web 服务器。

Usage:
    python web/run.py [--port 8000]
================================================================================
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> None:
    # 中文 Windows 控制台默认 GBK，先重配为 UTF-8，避免 ✓/✗ 打印崩溃
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="启动 PaperPilot Web 服务器")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8000, help="端口（默认 8000）")
    args = parser.parse_args()

    import uvicorn

    print(f"PaperPilot Web 服务器启动: http://{args.host}:{args.port}")
    uvicorn.run("web.server:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
