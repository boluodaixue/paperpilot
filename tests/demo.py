#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo.py — DeepResearch Agent 工具层演示脚本
================================================================================

用途：
  1. 一键验证 7 个工具是否正常工作
  2. 展示工具在多轮研究中的协作流程
  3. 作为面试演示：面试官可直接运行此脚本查看效果

运行模式：
  - 真实模式（默认）：调用真实 API / 本地执行
  - Mock 模式（调试）：无需任何 API Key，使用预设数据

切换方式：修改下方 USE_MOCK 变量

Usage:
    python demo.py                    # 真实模式（默认）
    # 或修改 USE_MOCK = False 后运行   # Mock 模式
================================================================================
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

# 将项目根目录加入 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# =============================================================================
# 运行模式开关
# =============================================================================
# 设置为 True 使用 Mock 模式（无需任何 API Key，用于快速演示）
# 设置为 False 使用真实模式（需要配置对应的 API Key）
USE_MOCK = False

# =============================================================================
# Mock 模式快速切换（正式运行时保持 False，调试/无网环境时设为 True）
# =============================================================================
# USE_MOCK = False   # <-- 取消注释此行即可切换到 Mock 模式（无需 API Key）


# =============================================================================
# 工具工厂：根据 USE_MOCK 创建真实或 Mock 工具
# =============================================================================
def create_tools(mock_mode: bool = USE_MOCK):
    """创建工具实例。

    Args:
        mock_mode: True 使用 Mock 工具，False 使用真实工具。

    Returns:
        dict: 工具名称到实例的映射。
    """
    from src.tools import (
        WebSearchTool, MockWebSearchTool,
        ArxivReaderTool, CodeSandboxTool,
        BrowserTool, MockBrowserTool,
        FileReaderTool, CalculatorTool, NotepadTool,
    )

    tools = {}

    # 1. web_search
    if mock_mode:
        tools["web_search"] = MockWebSearchTool()
    else:
        # 真实模式：WebSearchTool 自动从 .env / .env.local 读取 SERPAPI_KEY
        tools["web_search"] = WebSearchTool()

    # 2. browser
    if mock_mode:
        tools["browser"] = MockBrowserTool()
    else:
        tools["browser"] = BrowserTool()

    # 3. arxiv_reader（mock 模式通过 use_mock 参数控制）
    tools["arxiv_reader"] = ArxivReaderTool(use_mock=mock_mode)

    # 4. file_reader（始终本地运行，demo 中不限制目录）
    tools["file_reader"] = FileReaderTool(allowed_base_dir=None)

    # 5. code_sandbox（mock 模式通过 use_mock 参数控制）
    tools["code_sandbox"] = CodeSandboxTool(use_mock=mock_mode)

    # 6. calculator（始终本地运行）
    tools["calculator"] = CalculatorTool()

    # 7. notepad（始终本地运行）
    tools["notepad"] = NotepadTool()

    return tools


# =============================================================================
# 工具 Schema 打印
# =============================================================================
def print_tool_schemas(tools: dict) -> None:
    """打印所有工具的 OpenAI Function Calling Schema。"""
    print("\n" + "=" * 70)
    print("【工具 Schema 清单】（共 {} 个工具）".format(len(tools)))
    print("=" * 70)

    for name, tool in tools.items():
        schema = tool.get_openai_tool_schema()
        func = schema["function"]
        print(f"\n🔧 {func['name']}")
        print(f"   描述: {func['description'][:80]}...")
        params = func.get("parameters", {})
        props = params.get("properties", {})
        req = params.get("required", [])
        for pname, pdef in props.items():
            marker = "*" if pname in req else " "
            desc = pdef.get("description", "")[:50]
            print(f"   {marker} {pname}: {desc}")


# =============================================================================
# 单个工具功能测试
# =============================================================================
async def test_tools_individually(tools: dict) -> list[str]:
    """逐个测试每个工具的核心功能。"""
    errors: list[str] = []

    print("\n" + "=" * 70)
    print("【单工具功能测试】")
    print("=" * 70)

    # 1. web_search
    print("\n📡 web_search: 搜索 'transformer architecture' ...")
    try:
        r = await asyncio.wait_for(
            tools["web_search"].execute("transformer architecture", top_n=3),
            timeout=10,
        )
        results = r.get("results", [])
        print(f"   ✓ 返回 {len(results)} 条结果")
        for i, item in enumerate(results[:2], 1):
            print(f"     {i}. {item.get('title', 'N/A')[:50]}")
    except asyncio.TimeoutError:
        errors.append("web_search: 请求超时（网络慢或 API 无响应）")
        print(f"   ✗ 超时: 搜索请求超过 10 秒未返回")
    except Exception as e:
        errors.append(f"web_search: {e}")
        print(f"   ✗ 失败: {e}")

    # 2. browser
    test_url = (
        "https://example.com/ai-report-2024"
        if USE_MOCK
        else "https://httpbin.org/html"
    )
    print(f"\n🌐 browser: 打开 {test_url} ...")
    try:
        r = await asyncio.wait_for(
            tools["browser"].execute(test_url, max_chars=500),
            timeout=8,
        )
        preview = r[:120].replace("\n", " ")
        print(f"   ✓ 提取 {len(r)} 字符: {preview}...")
    except asyncio.TimeoutError:
        errors.append("browser: 请求超时（网页加载慢）")
        print(f"   ✗ 超时: 网页请求超过 8 秒未返回")
    except Exception as e:
        errors.append(f"browser: {e}")
        print(f"   ✗ 失败: {e}")

    # 3. arxiv_reader
    print("\n📄 arxiv_reader: 查询 'attention mechanism' ...")
    try:
        r = await asyncio.wait_for(
            tools["arxiv_reader"].execute(query="attention mechanism", max_results=2),
            timeout=12,
        )
        papers = r.get("papers", [])
        print(f"   ✓ 返回 {len(papers)} 篇论文")
        for i, p in enumerate(papers[:2], 1):
            print(f"     {i}. {p.get('title', 'N/A')[:50]}")
    except asyncio.TimeoutError:
        errors.append("arxiv_reader: 请求超时（ArXiv API 响应慢）")
        print(f"   ✗ 超时: ArXiv 请求超过 12 秒未返回")
    except Exception as e:
        errors.append(f"arxiv_reader: {e}")
        print(f"   ✗ 失败: {e}")

    # 4. file_reader（创建临时文件测试）
    print("\n📁 file_reader: 读取临时测试文件 ...")
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            f.write(
                "2024 年全球 AI 投资报告\n"
                "======================\n"
                "美国: 300 亿美元\n"
                "中国: 60 亿美元\n"
                "英国: 15 亿美元\n"
            )
            tmp_path = f.name

        r = await tools["file_reader"].execute(tmp_path)
        preview = r.split("\n")[-4:]  # 取最后几行数据
        print(f"   ✓ 读取成功")
        for line in preview:
            print(f"     {line}")
    except Exception as e:
        errors.append(f"file_reader: {e}")
        print(f"   ✗ 失败: {e}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    # 5. calculator
    print("\n🧮 calculator: 计算 '(300 + 60 + 15) * 0.15' ...")
    try:
        r = await tools["calculator"].execute("(300 + 60 + 15) * 0.15")
        print(f"   ✓ 结果: {r}")
    except Exception as e:
        errors.append(f"calculator: {e}")
        print(f"   ✗ 失败: {e}")

    # 6. code_sandbox
    print("\n💻 code_sandbox: 执行 'sum([300, 60, 15])' ...")
    try:
        r = await tools["code_sandbox"].execute("sum([300, 60, 15])")
        print(f"   ✓ stdout: {r.get('stdout', '').strip()}")
        print(f"   ✓ return_value: {r.get('return_value')}")
    except Exception as e:
        errors.append(f"code_sandbox: {e}")
        print(f"   ✗ 失败: {e}")

    # 7. notepad
    print("\n📝 notepad: 写笔记 → 读笔记 → 搜索笔记 ...")
    try:
        pad = tools["notepad"]
        await pad.execute(
            action="write",
            content="美国 2024 年 AI 投资约 300 亿美元",
            category="conclusion",
            source="示例数据",
        )
        await pad.execute(
            action="write",
            content="验证中国数据的官方来源",
            category="todo",
        )
        r = await pad.execute(action="read", max_entries=5)
        print(f"   ✓ 笔记记录成功")
        # 只打印第一行摘要
        first_line = r.split("\n")[0]
        print(f"     {first_line}")
    except Exception as e:
        errors.append(f"notepad: {e}")
        print(f"   ✗ 失败: {e}")

    return errors


# =============================================================================
# 模拟完整研究流程
# =============================================================================
async def simulate_research_flow(tools: dict) -> None:
    """模拟一个完整的多轮研究流程，展示工具协作。"""

    print("\n" + "=" * 70)
    print("【模拟研究流程】")
    print("=" * 70)
    print("研究主题: 2024 年全球生成式 AI 投资规模分析")
    print("=" * 70)

    notepad = tools["notepad"]

    # ---- 第 1 轮：广度搜索 ----
    print("\n🔍 第 1 轮：广度搜索（web_search）")
    print("   查询: 'global generative AI investment 2024'")
    try:
        r = await asyncio.wait_for(
            tools["web_search"].execute(
                "global generative AI investment 2024", top_n=5
            ),
            timeout=10,
        )
        results = r.get("results", [])
        print(f"   获得 {len(results)} 条搜索结果")
        for item in results[:2]:
            print(f"   - {item.get('title', 'N/A')[:55]}")
            # 把关键链接记下来，下一轮用 browser 读
            if "example.com" in item.get("url", "") or USE_MOCK:
                await notepad.execute(
                    action="write",
                    content=f"待阅读链接: {item.get('url')} — {item.get('title')}",
                    category="todo",
                )
    except asyncio.TimeoutError:
        print("   超时: 搜索请求超过 10 秒未返回")
    except Exception as e:
        print(f"   失败: {e}")

    # ---- 第 2 轮：深度阅读 ----
    print("\n📖 第 2 轮：深度阅读（browser）")
    url_to_read = (
        "https://example.com/ai-report-2024"
        if USE_MOCK
        else "https://httpbin.org/html"
    )
    print(f"   打开: {url_to_read}")
    try:
        r = await asyncio.wait_for(
            tools["browser"].execute(url_to_read, max_chars=1000),
            timeout=8,
        )
        # 提取关键数字（简化：找包含 "$" 或 "亿" 的行）
        lines = r.split("\n")
        key_lines = [l for l in lines if any(k in l for k in ["亿", "billion", "$", "投资"])][:3]
        if key_lines:
            print("   提取到关键信息:")
            for line in key_lines:
                print(f"     · {line[:70]}")
            await notepad.execute(
                action="write",
                content=f"从 {url_to_read} 提取: " + "; ".join(key_lines[:2]),
                category="conclusion",
                source=url_to_read,
            )
        else:
            print("   未提取到明显数字信息")
    except Exception as e:
        print(f"   失败: {e}")

    # ---- 第 3 轮：数值验证 ----
    print("\n🧮 第 3 轮：数值验证（calculator）")
    print("   计算: 美国 300 亿 + 中国 60 亿 + 英国 15 亿 = ?")
    try:
        r = await tools["calculator"].execute("300 + 60 + 15")
        total = r
        print(f"   三国总投资: {total} 亿美元")
        await notepad.execute(
            action="write",
            content=f"2024 年生成式 AI 投资（美+中+英）合计: {total} 亿美元",
            category="conclusion",
            source="calculator 汇总",
        )
    except Exception as e:
        print(f"   失败: {e}")

    # ---- 第 4 轮：复杂计算（code_sandbox）----
    print("\n💻 第 4 轮：复杂计算（code_sandbox）")
    print("   计算: 美国占比 = 300 / 375 * 100")
    try:
        code = "total = 300 + 60 + 15; us_ratio = 300 / total * 100; print(f'{us_ratio:.1f}%')"
        r = await tools["code_sandbox"].execute(code)
        stdout = r.get("stdout", "").strip()
        print(f"   美国投资占比: {stdout}")
    except Exception as e:
        print(f"   失败: {e}")

    # ---- 第 5 轮：学术论文交叉验证 ----
    print("\n📄 第 5 轮：学术验证（arxiv_reader）")
    print("   查询: 'generative AI venture capital survey'")
    try:
        r = await asyncio.wait_for(
            tools["arxiv_reader"].execute(
                query="generative AI venture capital", max_results=2
            ),
            timeout=12,
        )
        papers = r.get("papers", [])
        print(f"   找到 {len(papers)} 篇相关论文")
        for p in papers[:2]:
            print(f"   - {p.get('title', 'N/A')[:55]}")
    except Exception as e:
        print(f"   失败: {e}")

    # ---- 最终：回顾笔记 ----
    print("\n📝 最终：回顾研究笔记（notepad）")
    try:
        r = await notepad.execute(action="read", max_entries=10)
        # 只打印摘要
        lines = r.split("\n")
        for line in lines[:6]:
            print(f"   {line}")
        if len(lines) > 6:
            print(f"   ... ({len(lines) - 6} more lines)")
    except Exception as e:
        print(f"   失败: {e}")

    print("\n" + "=" * 70)
    print("【研究流程模拟完成】")
    print("=" * 70)


# =============================================================================
# 输出保存
# =============================================================================
import io
from datetime import datetime


class _TeeOutput:
    """同时输出到屏幕和内存缓冲区。"""
    def __init__(self) -> None:
        self.stdout = sys.stdout
        self.buf = io.StringIO()

    def write(self, text: str) -> None:
        self.stdout.write(text)
        self.buf.write(text)

    def flush(self) -> None:
        self.stdout.flush()

    def getvalue(self) -> str:
        return self.buf.getvalue()


def _save_outputs(tools: dict, errors: list[str], log_text: str) -> dict:
    """保存运行产出到 outputs/demo/ 目录。"""
    out_dir = Path("outputs/demo")
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    files_saved = {}

    # 1. 完整运行日志
    log_path = out_dir / f"demo_log_{timestamp}.txt"
    log_path.write_text(log_text, encoding="utf-8")
    files_saved["运行日志"] = str(log_path)

    # 2. Notepad 笔记导出
    notepad = tools.get("notepad")
    if notepad and hasattr(notepad, "to_dict"):
        notes = notepad.to_dict()
        if notes:
            note_path = out_dir / f"notepad_{timestamp}.json"
            import json
            note_path.write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")
            files_saved["研究笔记"] = str(note_path)

    # 3. 简洁总结报告
    summary_lines = [
        "# DeepResearch Agent — 演示运行报告",
        "",
        f"**运行时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"**运行模式**: {'Mock' if USE_MOCK else 'Real'}",
        f"**工具数量**: 7",
        f"**异常数量**: {len(errors)}",
        "",
        "## 工具状态",
        "",
        "| 工具 | 状态 |",
        "|------|------|",
    ]
    tool_status = {
        "web_search": "异常" if any("web_search" in e for e in errors) else "正常",
        "browser": "异常" if any("browser" in e for e in errors) else "正常",
        "arxiv_reader": "异常" if any("arxiv_reader" in e for e in errors) else "正常",
        "file_reader": "异常" if any("file_reader" in e for e in errors) else "正常",
        "calculator": "异常" if any("calculator" in e for e in errors) else "正常",
        "code_sandbox": "异常" if any("code_sandbox" in e for e in errors) else "正常",
        "notepad": "异常" if any("notepad" in e for e in errors) else "正常",
    }
    for name, status in tool_status.items():
        icon = "❌" if status == "异常" else "✅"
        summary_lines.append(f"| {name} | {icon} {status} |")

    if errors:
        summary_lines.extend(["", "## 异常详情", ""])
        for err in errors:
            summary_lines.append(f"- {err}")

    summary_lines.extend([
        "",
        "## 产出文件",
        "",
    ])
    for desc, path in files_saved.items():
        summary_lines.append(f"- **{desc}**: `{path}`")

    summary_path = out_dir / "summary.md"
    summary_path.write_text("\n".join(summary_lines), encoding="utf-8")
    files_saved["总结报告"] = str(summary_path)

    return files_saved


# =============================================================================
# 主函数
# =============================================================================
async def main() -> None:
    tee = _TeeOutput()
    sys.stdout = tee

    print("=" * 70)
    print("DeepResearch Agent — 工具层演示脚本")
    print("=" * 70)
    print(f"运行模式: {'Mock（模拟数据）' if USE_MOCK else 'Real（真实 API/本地执行）'}")
    print(f"项目根目录: {PROJECT_ROOT}")
    print("=" * 70)

    # 创建工具
    try:
        tools = create_tools()
    except Exception as e:
        print(f"\n❌ 工具初始化失败: {e}")
        print("\n提示: 如需使用 Mock 模式调试，请修改脚本顶部的 USE_MOCK = False")
        sys.stdout = tee.stdout
        sys.exit(1)

    # 打印 Schema
    print_tool_schemas(tools)

    # 单工具测试
    errors = await test_tools_individually(tools)

    # 模拟完整研究流程
    await simulate_research_flow(tools)

    # 最终总结
    print("\n" + "=" * 70)
    if errors:
        print(f"⚠️  演示完成，{len(errors)} 个工具出现异常:")
        for err in errors:
            print(f"   - {err}")
        print("\n提示:")
        print("   · 如果缺少 API Key，请设置环境变量（如 SERPAPI_KEY）")
        print("   · 或修改 USE_MOCK = False 切换到 Mock 模式")
    else:
        print("✅ 全部 7 个工具运行正常！")
    print("=" * 70)

    # 保存产出文件
    sys.stdout = tee.stdout
    files_saved = _save_outputs(tools, errors, tee.getvalue())

    print("\n" + "=" * 70)
    print("【产出文件已保存】")
    print("=" * 70)
    for desc, path in files_saved.items():
        print(f"  📄 {desc}: {path}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
