#!/usr/bin/env python3
"""验证 .env / .env.local 配置是否正确"""
from __future__ import annotations

import os
import sys

# 加载 .env
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.getcwd(), ".env"))
local_env = os.path.join(os.getcwd(), ".env.local")
if os.path.exists(local_env):
    load_dotenv(dotenv_path=local_env, override=True)


def check(key: str, expected_prefix: str = "", optional: bool = False) -> str:
    """检查环境变量。"""
    val = os.getenv(key)
    if not val:
        if optional:
            return f"  ⚪ {key}: 未设置（可选，使用默认值）"
        return f"  ❌ {key}: 未设置（必须配置）"

    # 脱敏显示
    if "key" in key.lower() or "token" in key.lower():
        display = val[:6] + "***" + val[-4:] if len(val) > 10 else "***"
    else:
        display = val

    status = "✅"
    if expected_prefix and not val.startswith(expected_prefix):
        status = "⚠️ "
        display += f" （建议以 {expected_prefix} 开头）"

    return f"  {status} {key}: {display}"


print("=" * 60)
print("LLM 后端配置检查")
print("=" * 60)
print(check("DEEPSEEK_API_KEY", optional=True))
print(check("DEEPSEEK_BASE_URL", expected_prefix="https://"))
print(check("MIMO_API_KEY", optional=True))
print(check("MIMO_BASE_URL", expected_prefix="https://"))
print(check("MIMO_MODEL"))

print()
print("=" * 60)
print("工具层配置检查")
print("=" * 60)
print(check("SERPAPI_KEY", optional=True))
print(check("SEARCH_BACKEND", optional=True))
print(check("BING_SEARCH_KEY", optional=True))
print(check("ARXIV_READER_BACKEND", optional=True))
print(check("SEMANTIC_SCHOLAR_API_KEY", optional=True))

print()
print("=" * 60)
print("LangSmith 追踪配置（可选）")
print("=" * 60)
print(check("LANGSMITH_TRACING", optional=True))
print(check("LANGSMITH_API_KEY", optional=True))

print()
print("=" * 60)
print("关键配置建议")
print("=" * 60)

# 检查 mimo 配置
mimo_url = os.getenv("MIMO_BASE_URL", "")
mimo_model = os.getenv("MIMO_MODEL", "")
if "openrouter" in mimo_url and not mimo_model.startswith("xiaomi/"):
    print("  ⚠️  MIMO 配置不匹配：")
    print("      你用了 OpenRouter 的 URL，但模型名没加 xiaomi/ 前缀")
    print("      修复方案：")
    print("        方案 A（推荐）: MIMO_BASE_URL=https://token-plan-cn.xiaomimimo.com/v1")
    print("        方案 B: MIMO_MODEL=xiaomi/mimo-v2.5-pro")
elif "xiaomimimo" in mimo_url and mimo_model.startswith("xiaomi/"):
    print("  ⚠️  MIMO 配置不匹配：")
    print("      你用了小米官方 URL，但模型名加了 xiaomi/ 前缀（这是 OpenRouter 格式）")
    print("      修复: MIMO_MODEL=mimo-v2.5-pro")
else:
    print("  ✅ MIMO 配置看起来正确")

# 检查 search 后端
search_backend = os.getenv("SEARCH_BACKEND", "serpapi").lower()
if search_backend == "serpapi" and not os.getenv("SERPAPI_KEY"):
    print("  ⚠️  SEARCH_BACKEND=serpapi，但 SERPAPI_KEY 未设置")
elif search_backend == "bing" and not os.getenv("BING_SEARCH_KEY"):
    print("  ⚠️  SEARCH_BACKEND=bing，但 BING_SEARCH_KEY 未设置")
else:
    print("  ✅ 搜索后端配置正确")

print()
