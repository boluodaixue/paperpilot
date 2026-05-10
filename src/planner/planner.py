"""
自适应规划器 (Adaptive Planner)

LLM 驱动的规划器，负责将研究问题分解为结构化的子任务 DAG。
核心能力:
  - 初始规划: 生成 3-8 个子问题的 DAG
  - 增量重规划: 保留 confidence≥0.6 的成功结果，仅修改失败子问题
  - 健壮性 JSON 解析: 支持 markdown 代码块、多余换行等噪声
"""
from __future__ import annotations

import json
import re
from typing import Any

from .dag import DAG, DAGCycleError
from .budget_tracker import BudgetTracker
from ..orchestrator.schemas import SubTask, TaskType, AgentResult
from ..utils.tracing import trace_chain


__all__ = ["Planner", "PlanParseError"]


class PlanParseError(Exception):
    """规划结果解析失败时抛出。"""
    pass


# ============================================================================
# Prompt 常量
# ============================================================================

INITIAL_PLAN_PROMPT = """\
You are an expert research planner. Your task is to decompose a complex research question into a directed acyclic graph (DAG) of sub-tasks.

## Input
Research Question: {query}

## Output Format
Return a JSON object with this exact structure (no markdown, no extra text):
{{
  "sub_tasks": [
    {{
      "task_id": "task_1",
      "task_type": "search",
      "description": "What is ...",
      "dependencies": [],
      "context_keys": [],
      "timeout_seconds": 120,
      "priority": 1,
      "expected_type": "factual",
      "search_hints": ["keyword1", "keyword2"]
    }}
  ]
}}

## Rules
1. task_type must be one of: search, analyze, verify
2. dependencies must reference existing task_id values
3. The graph must be a DAG (no cycles)
4. Generate 3 to 8 sub_tasks
5. More fundamental/information-gathering tasks should have fewer dependencies
6. Verification tasks should depend on analysis tasks
7. Use concise but clear descriptions
8. CRITICAL — RELEVANCE CONSTRAINT: Each sub-task description MUST directly address the research question. If the user asks about 'internship/job application', do NOT generate tasks about 'technology trends', 'annual news summary', or 'science breakthroughs'.
9. The search_hints field MUST contain keywords directly from the query. Do NOT invent unrelated keywords.
10. Prefer specific, actionable queries over broad, vague ones.

## Anti-examples (DO NOT do this)
- Query: "How to find an internship at a big tech company" → BAD tasks: "2025 technology trends", "annual science news", "latest AI breakthroughs"
- Query: "How to prepare for post-training LLM engineer internship" → GOOD tasks: "Big tech post-training intern JD requirements", "LLM post-training intern interview experience", "Resume tips for LLM algorithm intern"

## Context (if any)
{memory_context}
"""

REPLAN_PROMPT = """\
You are an expert research planner. Some sub-tasks failed and need to be re-planned.

## Original Question
{query}

## Failed Tasks
{failed_tasks_json}

## Successful Results to Preserve (confidence >= 0.6)
{preserved_results_json}

## Reason for Failure
{reason}

## Output Format
Return a JSON object with new sub_tasks. You may:
1. Modify failed tasks (new task_id, same or different description)
2. Add new tasks to fill gaps
3. Remove tasks that are no longer needed
4. Keep dependencies consistent

Structure:
{{
  "sub_tasks": [...]
}}

Only return the JSON. No markdown, no extra text.
"""


class Planner:
    """自适应规划器。

    Attributes:
        policy: VLLMPolicy 实例，用于调用 LLM。
        budget_tracker: 可选的预算追踪器，监控 planning 阶段的 token 消耗。
    """

    def __init__(self, policy, budget_tracker: BudgetTracker | None = None) -> None:
        self.policy = policy
        self.budget_tracker = budget_tracker or BudgetTracker()
        self._last_raw_json: str = ""

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    @trace_chain(name="planner.generate_plan", tags=["m2", "planner"])
    def generate_plan(self, query: str, memory_context: str = "") -> DAG:
        """生成初始执行计划（DAG）。

        Args:
            query: 原始研究问题。
            memory_context: 历史上下文（首次规划为空字符串）。

        Returns:
            DAG: 子任务依赖图。

        Raises:
            PlanParseError: LLM 输出无法解析为合法 DAG 时抛出。
        """
        prompt = self._build_prompt(query, memory_context)
        messages = [
            {"role": "system", "content": "You are a research planning assistant. Output valid JSON only."},
            {"role": "user", "content": prompt},
        ]

        try:
            response = self.policy(messages)
        except RuntimeError as e:
            raise PlanParseError(f"LLM call failed during planning: {e}") from e

        content = response.get("content", "") or ""
        self._last_raw_json = content
        # 估算 planning token 消耗
        self.budget_tracker.track(len(content) // 3)

        return self._parse_plan(content)

    @trace_chain(name="planner.replan", tags=["m2", "planner"])
    def replan(
        self,
        query: str,
        failed_tasks: list[SubTask],
        existing_results: list[AgentResult],
        reason: str,
    ) -> DAG:
        """增量重规划：保留高置信度结果，修改失败任务。

        Args:
            query: 原始研究问题。
            failed_tasks: 执行失败的 SubTask 列表。
            existing_results: 所有历史执行结果。
            reason: 失败原因描述。

        Returns:
            DAG: 新的执行计划。
        """
        # 筛选保留的结果（confidence >= 0.6 且状态为 SUCCESS）
        preserved = [
            {
                "task_id": r.task_id,
                "output": str(r.output)[:500] if r.output else "",
                "confidence": r.confidence,
            }
            for r in existing_results
            if r.status.value == "success" and r.confidence >= 0.6
        ]

        failed_json = json.dumps(
            [{"task_id": t.task_id, "description": t.description, "type": t.task_type.value} for t in failed_tasks],
            ensure_ascii=False,
            indent=2,
        )
        preserved_json = json.dumps(preserved, ensure_ascii=False, indent=2)

        prompt = REPLAN_PROMPT.format(
            query=query,
            failed_tasks_json=failed_json,
            preserved_results_json=preserved_json,
            reason=reason,
        )
        messages = [
            {"role": "system", "content": "You are a research planning assistant. Output valid JSON only."},
            {"role": "user", "content": prompt},
        ]

        try:
            response = self.policy(messages)
        except RuntimeError as e:
            raise PlanParseError(f"LLM call failed during replanning: {e}") from e

        content = response.get("content", "") or ""
        self._last_raw_json = content
        self.budget_tracker.track(len(content) // 3)

        return self._parse_plan(content)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _build_prompt(self, query: str, memory: str) -> str:
        """构建初始规划 prompt。"""
        # 首次运行（无历史记忆）时，提示 Planner 更激进地拆解子任务
        has_memory = bool(memory and memory.strip() and memory != "None")
        if not has_memory:
            extra_hint = (
                "\n## Note\n"
                "No previous research memory is available for this topic. "
                "Please be MORE AGGRESSIVE in decomposition: generate 6-10 sub_tasks to thoroughly cover the topic, "
                "rather than the usual 3-5. Each sub-task should focus on a distinct angle or data source.\n"
                "IMPORTANT: Each sub-task description must directly reflect the user's original intent. "
                "If the user asks about 'internship application strategies', do NOT generate tasks about '2025 tech trends' or 'annual science summary'."
            )
        else:
            extra_hint = (
                "\n## Note\n"
                "Use the preserved successful results above to inform new sub-tasks. "
                "New tasks should fill gaps and avoid duplicating existing coverage."
            )
        return INITIAL_PLAN_PROMPT.format(query=query, memory_context=memory or "None") + extra_hint

    def _parse_plan(self, json_str: str) -> DAG:
        """健壮性 JSON 解析：处理 markdown 代码块、多余换行等噪声。

        解析策略:
          1. 先尝试直接 json.loads
          2. 失败则提取 markdown 代码块内容
          3. 清理常见噪声（尾部逗号、注释等）
          4. 验证 DAG 无环
        """
        raw = json_str.strip()

        # 尝试提取 markdown 代码块
        if raw.startswith("```"):
            # 去掉首行 ```json 或 ```
            lines = raw.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            raw = "\n".join(lines).strip()

        # 尝试提取 ```json...``` 中间的内容（即使不在开头）
        code_block_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if code_block_match:
            raw = code_block_match.group(1).strip()

        # 尝试直接找最外层的 JSON 对象
        if not raw.startswith("{"):
            obj_match = re.search(r"(\{.*\})", raw, re.DOTALL)
            if obj_match:
                raw = obj_match.group(1).strip()

        # 清理尾部逗号（JSON 不允许 trailing comma）
        raw = re.sub(r",(\s*[}\]])", r"\1", raw)

        # 解析 JSON
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            # 最后尝试：逐行修复（去掉注释）
            cleaned_lines = []
            for line in raw.splitlines():
                # 去掉 // 注释
                if "//" in line:
                    line = line[: line.index("//")]
                cleaned_lines.append(line)
            try:
                data = json.loads("\n".join(cleaned_lines))
            except json.JSONDecodeError:
                raise PlanParseError(
                    f"Failed to parse planner output as JSON. Raw snippet: {json_str[:500]}"
                ) from e

        if not isinstance(data, dict) or "sub_tasks" not in data:
            raise PlanParseError(f"Planner output missing 'sub_tasks' key. Keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")

        sub_tasks_raw = data["sub_tasks"]
        if not isinstance(sub_tasks_raw, list):
            raise PlanParseError(f"'sub_tasks' must be a list, got {type(sub_tasks_raw)}")

        dag = DAG()
        for item in sub_tasks_raw:
            task = self._deserialize_subtask(item)
            dag.add_node(task.task_id)

        # 第二遍添加边
        for item in sub_tasks_raw:
            task_id = item.get("task_id", "")
            for dep in item.get("dependencies", []):
                if not dag.has_node(dep):
                    # 依赖指向不存在的任务，创建占位节点
                    dag.add_node(dep)
                dag.add_edge(dep, task_id)  # dep -> task_id (task_id 依赖 dep)

        # 验证无环
        try:
            dag.topological_sort()
        except DAGCycleError as e:
            raise PlanParseError(f"Planner generated a cyclic graph: {e}") from e

        return dag

    def _deserialize_subtask(self, item: dict[str, Any]) -> SubTask:
        """将 JSON dict 反序列化为 SubTask。"""
        task_type_str = item.get("task_type", "search")
        try:
            task_type = TaskType(task_type_str)
        except ValueError:
            task_type = TaskType.SEARCH  # 默认值降级

        return SubTask(
            task_id=item.get("task_id", "unknown"),
            task_type=task_type,
            description=item.get("description", ""),
            dependencies=list(item.get("dependencies", [])),
            context_keys=list(item.get("context_keys", [])),
            timeout_seconds=int(item.get("timeout_seconds", 120)),
            priority=int(item.get("priority", 1)),
            expected_type=item.get("expected_type", "factual"),
            search_hints=list(item.get("search_hints", [])),
        )

    def get_task_map_from_dag(self, dag: DAG, raw_json: str) -> dict[str, SubTask]:
        """从 DAG 和原始 JSON 重建 task_id -> SubTask 映射。

        通常在 generate_plan 后由编排器调用。
        """
        # 复用 _parse_plan 中的解析逻辑，但返回映射
        # 这里重新解析 raw_json 以获取完整 SubTask 信息
        raw = raw_json.strip()
        if raw.startswith("```"):
            lines = raw.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            raw = "\n".join(lines).strip()
        code_block_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if code_block_match:
            raw = code_block_match.group(1).strip()
        if not raw.startswith("{"):
            obj_match = re.search(r"(\{.*\})", raw, re.DOTALL)
            if obj_match:
                raw = obj_match.group(1).strip()
        raw = re.sub(r",(\s*[}\]])", r"\1", raw)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}

        sub_tasks_raw = data.get("sub_tasks", [])
        return {item.get("task_id", f"task_{i}"): self._deserialize_subtask(item)
                for i, item in enumerate(sub_tasks_raw)}
