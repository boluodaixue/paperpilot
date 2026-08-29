"""
代码沙箱工具

模拟执行 Python 代码，返回 stdout / stderr / 返回值。
当前为安全模拟版：仅解析简单表达式或返回预定义结果，不真正执行任意代码。
生产环境可替换为 Docker 沙箱或受限子进程。
"""
from __future__ import annotations

import ast
import asyncio
import io
import random
import sys
import traceback
from contextlib import redirect_stdout, redirect_stderr
from typing import Any


__all__ = ["CodeSandboxTool"]


class CodeSandboxTool:
    """代码沙箱工具。

    安全策略:
      1. 默认仅允许 ast.parse 解析的纯表达式（无函数定义、无 import）
      2. 危险内置函数被黑名单过滤
      3. 超时保护（通过 asyncio.wait_for 在外层实现）
      4. 真正执行时限制 builtins 访问
    """

    name: str = "code_sandbox"
    description: str = (
        "Execute Python code in a sandboxed environment. "
        "Input: {'code': str, 'timeout': int(optional, default=10)}. "
        "Output: {'stdout': str, 'stderr': str, 'return_value': Any, 'success': bool}."
    )

    # 危险 builtins 黑名单
    _FORBIDDEN_NAMES = {
        "__import__", "open", "eval", "exec", "compile",
        "input", "raw_input", "reload", "exit", "quit",
        "os", "sys", "subprocess", "shutil", "socket",
    }

    def __init__(self, use_mock: bool = False) -> None:
        from ..utils.env_config import get_env_int

        self.use_mock = use_mock
        # 默认超时从 .env 读取，方便统一调整
        self.default_timeout = get_env_int("CODE_SANDBOX_TIMEOUT", 10)

    def get_openai_tool_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": "Python code to execute",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Execution timeout in seconds",
                            "default": 10,
                        },
                    },
                    "required": ["code"],
                },
            },
        }

    async def execute(self, code: str, timeout: int | None = None) -> dict[str, Any]:
        """在沙箱中执行 Python 代码。

        Args:
            code: Python 代码字符串。
            timeout: 超时秒数。

        Returns:
            包含 stdout, stderr, return_value, success 的字典。
        """
        actual_timeout = timeout if timeout is not None else self.default_timeout
        if self.use_mock:
            return await self._mock_execute(code)
        return await self._safe_execute(code, actual_timeout)

    async def _mock_execute(self, code: str) -> dict[str, Any]:
        """Mock 模式：模拟常见计算结果。"""
        await asyncio.sleep(random.randint(50, 200) / 1000.0)

        code_stripped = code.strip().lower()
        if "1+1" in code_stripped or "1 + 1" in code_stripped:
            return {
                "stdout": "2\n",
                "stderr": "",
                "return_value": 2,
                "success": True,
            }
        if "fibonacci" in code_stripped or "fib" in code_stripped:
            return {
                "stdout": "[0, 1, 1, 2, 3, 5, 8, 13, 21, 34]\n",
                "stderr": "",
                "return_value": [0, 1, 1, 2, 3, 5, 8, 13, 21, 34],
                "success": True,
            }
        return {
            "stdout": f"# Mock execution of:\n{code}\n",
            "stderr": "",
            "return_value": None,
            "success": True,
        }

    async def _safe_execute(self, code: str, timeout: int) -> dict[str, Any]:
        """受限执行模式。"""
        # 1. 语法检查
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return {
                "stdout": "",
                "stderr": f"SyntaxError: {e}",
                "return_value": None,
                "success": False,
            }

        # 2. 静态安全检查：遍历 AST 查找禁止节点
        for node in ast.walk(tree):
            if isinstance(node, ast.Import | ast.ImportFrom):
                return {
                    "stdout": "",
                    "stderr": "SecurityError: import statements are not allowed",
                    "return_value": None,
                    "success": False,
                }
            if isinstance(node, ast.Call):
                # 检查是否调用黑名单函数
                if isinstance(node.func, ast.Name) and node.func.id in self._FORBIDDEN_NAMES:
                    return {
                        "stdout": "",
                        "stderr": f"SecurityError: '{node.func.id}' is forbidden",
                        "return_value": None,
                        "success": False,
                    }

        # 3. 在受限环境中执行
        def _run() -> dict[str, Any]:
            safe_globals = {"__builtins__": {}}
            safe_locals: dict[str, Any] = {}
            stdout_buf = io.StringIO()
            stderr_buf = io.StringIO()
            try:
                with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                    result = eval(code, safe_globals, safe_locals)
                return {
                    "stdout": stdout_buf.getvalue(),
                    "stderr": stderr_buf.getvalue(),
                    "return_value": result,
                    "success": True,
                }
            except Exception:
                return {
                    "stdout": stdout_buf.getvalue(),
                    "stderr": traceback.format_exc(),
                    "return_value": None,
                    "success": False,
                }

        try:
            # 使用 asyncio 的 run_in_executor 避免阻塞事件循环
            loop = asyncio.get_running_loop()
            return await asyncio.wait_for(
                loop.run_in_executor(None, _run),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return {
                "stdout": "",
                "stderr": f"TimeoutError: execution exceeded {timeout}s",
                "return_value": None,
                "success": False,
            }
