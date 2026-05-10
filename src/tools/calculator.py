"""
计算器工具 (CalculatorTool)

设计理由：
  code_sandbox 虽然能做任意 Python 计算，但：
  1. 有安全风险（需要 AST 检查）
  2. 启动慢（即使简单加减也要走完整沙箱流程）
  3. 对 LLM 来说，调用成本高（需要写完整 Python 代码）

  CalculatorTool 提供轻量、安全、快速的确定性计算：
  - 直接 eval 数学表达式（无代码执行风险）
  - 支持单位换算、百分比计算、统计函数
  - 调用成本低：Agent 只需要传表达式字符串

与 code_sandbox 的关系：
  calculator:   简单算术（2+2, 15% of 300, average([1,2,3])）
  code_sandbox: 复杂逻辑（数据分析、模拟、算法实现）
"""
from __future__ import annotations

import ast
import math
import operator
import re
import statistics
from typing import Any


__all__ = ["CalculatorTool"]

# 允许的安全操作符和函数
_SAFE_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

_SAFE_FUNCS = {
    "abs": abs,
    "round": round,
    "max": max,
    "min": min,
    "sum": sum,
    "len": len,
    # 数学函数
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "ceil": math.ceil,
    "floor": math.floor,
    "factorial": math.factorial,
    # 统计函数
    "mean": statistics.mean,
    "median": statistics.median,
    "stdev": statistics.stdev,
    "variance": statistics.variance,
    # 常量
    "pi": math.pi,
    "e": math.e,
}


class CalculatorTool:
    """轻量计算器：安全地执行数学表达式。"""

    name: str = "calculator"
    description: str = (
        "Evaluate a mathematical expression safely. "
        "Use this for quick calculations instead of code_sandbox. "
        "Supports: +, -, *, /, **, %, abs, round, sqrt, sin, cos, log, mean, median, etc. "
        "Input: {'expression': str}. Output: result as string."
    )

    def get_openai_tool_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "expression": {
                            "type": "string",
                            "description": "Mathematical expression to evaluate, e.g. '(150 + 230) * 0.15' or 'mean([12, 15, 18, 21])'",
                        },
                    },
                    "required": ["expression"],
                },
            },
        }

    async def execute(self, expression: str) -> str:
        """计算数学表达式。

        Args:
            expression: 数学表达式字符串，如 "(150 + 230) * 0.15" 或 "mean([12, 15, 18, 21])"

        Returns:
            计算结果的字符串表示。
        """
        if not expression or not expression.strip():
            return "[Calculator Error] Empty expression"

        # 模拟异步 IO（实际计算是 CPU-bound，但保持接口一致）
        import asyncio
        await asyncio.sleep(0)

        try:
            # 预处理：将中文括号、百分号等转为标准格式
            expr = self._preprocess(expression)
            result = self._safe_eval(expr)
            return f"{result}"
        except ZeroDivisionError:
            return "[Calculator Error] Division by zero"
        except ValueError as e:
            return f"[Calculator Error] Invalid value: {e}"
        except Exception as e:
            return f"[Calculator Error] {type(e).__name__}: {e}"

    @staticmethod
    def _preprocess(expr: str) -> str:
        """预处理表达式：统一格式。"""
        # 中文括号 → 英文括号
        expr = expr.replace("（", "(").replace("）", ")")
        expr = expr.replace("【", "[").replace("】", "]")
        # 百分号处理：15% → 15/100
        expr = re.sub(r"(\d+(?:\.\d+)?)%", r"(\1/100)", expr)
        # 千分位逗号去除（只匹配数字间的逗号，如 1,000 → 1000；保留列表逗号）
        expr = re.sub(r"(\d),(?=\d)", r"\1", expr)
        return expr.strip()

    def _safe_eval(self, expr: str) -> Any:
        """安全 eval：只允许数学 AST 节点。"""
        tree = ast.parse(expr, mode="eval")
        return self._eval_node(tree.body)

    def _eval_node(self, node: ast.AST) -> Any:
        """递归求值 AST 节点。"""
        if isinstance(node, ast.Num):  # Python < 3.8
            return node.n
        if isinstance(node, ast.Constant):  # Python >= 3.8
            return node.value
        if isinstance(node, ast.BinOp):
            op_type = type(node.op)
            if op_type not in _SAFE_OPS:
                raise ValueError(f"Unsupported binary operator: {op_type.__name__}")
            return _SAFE_OPS[op_type](self._eval_node(node.left), self._eval_node(node.right))
        if isinstance(node, ast.UnaryOp):
            op_type = type(node.op)
            if op_type not in _SAFE_OPS:
                raise ValueError(f"Unsupported unary operator: {op_type.__name__}")
            return _SAFE_OPS[op_type](self._eval_node(node.operand))
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ValueError("Only simple function calls are allowed")
            func_name = node.func.id
            if func_name not in _SAFE_FUNCS:
                raise ValueError(f"Unsupported function: {func_name}")
            args = [self._eval_node(arg) for arg in node.args]
            return _SAFE_FUNCS[func_name](*args)
        if isinstance(node, ast.Name):
            if node.id not in _SAFE_FUNCS:
                raise ValueError(f"Unsupported name: {node.id}")
            return _SAFE_FUNCS[node.id]
        if isinstance(node, ast.List):
            return [self._eval_node(elt) for elt in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(self._eval_node(elt) for elt in node.elts)
        if isinstance(node, ast.Subscript):
            value = self._eval_node(node.value)
            slice_val = self._eval_node(node.slice)
            return value[slice_val]
        if isinstance(node, ast.Index):  # Python < 3.9 compatibility
            return self._eval_node(node.value)

        raise ValueError(f"Unsupported AST node: {type(node).__name__}")
