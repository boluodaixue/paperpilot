#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/viz_evidence_graph.py
================================================================================
PaperPilot: 证据关系图可视化。

从 SQLite 加载指定 session 的 Evidence Graph，导出为可交互 HTML
（vis-network 力导向图：拖拽 / 缩放 / 悬停详情）。

Usage:
    python scripts/viz_evidence_graph.py --session_id smoke-7
    python scripts/viz_evidence_graph.py --session_id smoke-7 --output viz.html
================================================================================
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.evidence.graph import EvidenceGraph

# 节点类型 → 颜色
NODE_COLORS = {
    "evidence": {"background": "#4a90d9", "border": "#2c5f8a", "label": "证据"},
    "paper": {"background": "#7bc96f", "border": "#4a9e3d", "label": "论文"},
    "topic": {"background": "#b07cd9", "border": "#8557ab", "label": "主题"},
}
# 关系 → 颜色
EDGE_COLORS = {
    "SOURCED_FROM": "#a0a0a0",
    "ANSWERS": "#a0a0a0",
    "SUPPORTS": "#2ecc71",
    "CONTRADICTS": "#e74c3c",
    "EXTENDS": "#f39c12",
}


def build_html(data: dict, session_id: str, evidence_count: int, edge_count: int) -> str:
    """把 export_json 数据渲染成 vis-network HTML。"""
    nodes_js = []
    for n in data["nodes"]:
        ntype = n.get("type", "")
        color = NODE_COLORS.get(ntype, NODE_COLORS["evidence"])
        label = n.get("label") or n.get("id", "")
        title = f"<b>{n.get('id', '')}</b><br>类型: {color['label']}<br>{label}"
        nodes_js.append(
            {"id": n["id"], "label": str(label)[:40], "group": ntype,
             "color": color, "title": title}
        )
    edges_js = []
    for e in data["edges"]:
        rel = e.get("relation", "")
        edges_js.append({
            "from": e["source"], "to": e["target"],
            "label": f"{rel[:4]} {e.get('weight', '')}",
            "color": {"color": EDGE_COLORS.get(rel, "#999")},
            "title": f"{e['source']} → {e['target']}<br>{rel} (w={e.get('weight', '')})",
        })

    graph_json = json.dumps({"nodes": nodes_js, "edges": edges_js}, ensure_ascii=False)
    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>证据关系图 — {session_id}</title>
<script type="text/javascript" src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<style>
  body {{ margin: 0; font-family: "Microsoft YaHei", sans-serif; }}
  #header {{ padding: 10px 16px; background: #1f2937; color: #fff; }}
  #header h1 {{ margin: 0; font-size: 18px; }}
  #header p {{ margin: 4px 0 0; font-size: 12px; color: #9ca3af; }}
  #graph {{ width: 100%; height: calc(100vh - 60px); }}
  #legend {{ position: fixed; right: 12px; top: 70px; background: rgba(255,255,255,.95);
             padding: 10px 14px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,.15);
             font-size: 12px; line-height: 1.9; }}
  #legend span {{ display: inline-block; width: 12px; height: 12px; border-radius: 3px;
                  margin-right: 6px; vertical-align: middle; }}
</style>
</head>
<body>
<div id="header">
  <h1>证据关系图 — {session_id}</h1>
  <p>{evidence_count} 条证据 · {edge_count} 条关系边（拖拽/滚轮缩放/悬停查看详情）</p>
</div>
<div id="graph"></div>
<div id="legend">
  <div><span style="background:#4a90d9"></span>证据 (Evidence)</div>
  <div><span style="background:#7bc96f"></span>论文 (Paper)</div>
  <div><span style="background:#b07cd9"></span>主题 (Topic)</div>
  <div><span style="background:#2ecc71"></span>SUPPORTS 支持</div>
  <div><span style="background:#e74c3c"></span>CONTRADICTS 矛盾</div>
  <div><span style="background:#f39c12"></span>EXTENDS 扩展</div>
  <div><span style="background:#a0a0a0"></span>结构边（来源于/回答）</div>
</div>
<script>
const data = {graph_json};
const container = document.getElementById('graph');
const options = {{
  nodes: {{ shape: 'dot', size: 14, font: {{ size: 12, face: 'Microsoft YaHei' }} }},
  edges: {{ arrows: 'to', font: {{ size: 10, strokeWidth: 0 }}, smooth: {{ enabled: true }} }},
  physics: {{ barnesHut: {{ gravitationalConstant: -3000, springLength: 120 }} }},
  interaction: {{ hover: true, tooltipDelay: 100 }}
}};
new vis.Network(container, data, options);
</script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="证据关系图可视化")
    parser.add_argument("--session_id", required=True, help="要可视化的 session（如 smoke-7）")
    parser.add_argument("--db", default="data/memory.db", help="SQLite 数据库路径")
    parser.add_argument("--output", default="", help="输出 HTML 路径（默认 outputs/viz_<session>.html）")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"数据库不存在: {args.db}")
        sys.exit(1)

    graph = EvidenceGraph(db_path=args.db, session_id=args.session_id)
    data = graph.export_json()
    if not data["nodes"]:
        print(f"session '{args.session_id}' 没有证据节点。可用的 session：")
        import sqlite3
        conn = sqlite3.connect(args.db)
        rows = conn.execute(
            "SELECT DISTINCT session_id FROM evidence"
        ).fetchall()
        conn.close()
        for (sid,) in rows:
            print(f"  {sid}")
        sys.exit(1)

    stats = graph.graph_stats()
    evidence_count = stats["nodes"]["evidence"]
    edge_count = len(data["edges"])

    output = args.output or os.path.join("outputs", f"viz_{args.session_id}.html")
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    html = build_html(data, args.session_id, evidence_count, edge_count)
    with open(output, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"已生成: {output}（{evidence_count} 证据 / {edge_count} 边）")
    print(f"在浏览器打开即可交互查看。若无法联网加载图库，请开启 VPN。")


if __name__ == "__main__":
    main()
