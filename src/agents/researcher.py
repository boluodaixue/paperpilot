"""
研究员 Agent (ResearcherAgent)

执行搜索和分析类 SubTask，实现多轮 tool-calling 循环。
设计为项目一 ToolAgentLoop 的简化版：
  - 单 trajectory，无批处理
  - 支持 7 种工具：web_search, arxiv_reader, code_sandbox, browser,
    file_reader, calculator, notepad
  - 通过 VLLMPolicy 进行 LLM 调用
  - 工具结果回写后自动继续，直到模型不再调用工具或达到 max_turns
"""
from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

from .base_agent import BaseAgent
from ..orchestrator.schemas import SubTask, AgentResult, AgentStatus, TaskType
from ..utils.tracing import trace_agent, trace_block


__all__ = ["ResearcherAgent"]


class ResearcherAgent(BaseAgent):
    """研究员 Agent：负责搜索、分析、验证类任务。

    可用工具（7 个）：
      - web_search:   网页搜索，返回标题/链接/摘要
      - browser:      网页阅读器，打开 URL 提取正文
      - arxiv_reader: ArXiv 论文元数据检索
      - file_reader:  本地文件阅读（.txt/.md/.pdf/.csv/.json/.docx）
      - code_sandbox: Python 代码沙箱执行
      - calculator:   轻量数学计算（比沙箱更快更安全）
      - notepad:      草稿笔记（记录中间结论/待办/搜索策略）

    Attributes:
        max_turns: 最大交互轮数，防止无限循环。
        tool_map: 工具名称到工具实例的映射。
    """

    def __init__(
        self,
        name: str,
        policy,
        tools: list | None = None,
        max_turns: int = 10,
        tool_max_attempts: int = 2,
        tool_retry_delay_seconds: float = 0.25,
        tool_fallbacks: dict[str, list[str]] | None = None,
    ) -> None:
        super().__init__(name, policy, tools)
        self.max_turns = max_turns
        self.tool_max_attempts = max(1, int(tool_max_attempts))
        self.tool_retry_delay_seconds = max(0.0, float(tool_retry_delay_seconds))
        self.tool_fallbacks = {
            str(name): [str(fallback) for fallback in fallbacks]
            for name, fallbacks in (tool_fallbacks or {}).items()
        }
        self.tool_map: dict[str, Any] = {t.name: t for t in (tools or [])}

    @trace_agent(name="researcher.run", tags=["agent", "researcher"])
    async def run(self, task: SubTask, context: dict) -> AgentResult:
        """执行 Researcher 任务。

        流程:
          1. 构建初始 system + user messages
          2. 循环调用 policy，解析 tool_calls
          3. 执行工具，将结果追加为 tool message
          4. 直到无 tool_calls 或达到 max_turns
        """
        trajectory: list[dict] = []
        total_tokens: int = 0

        # 构建任务描述
        task_desc = self._build_task_prompt(task, context)

        # SEARCH 必须取得有效来源；只有分析/验证任务允许走纯分析路径。
        if task.task_type != TaskType.SEARCH and self._is_non_searchable(task, context):
            messages = [
                {"role": "system", "content": self._system_prompt_direct_analysis()},
                {"role": "user", "content": task_desc},
            ]
            try:
                response = await self._call_policy(messages, tools=[])
                content = response.get("content", "") or ""
                return AgentResult(
                    task_id=task.task_id,
                    status=AgentStatus.SUCCESS,
                    output=content,
                    trajectory=[{"role": "assistant", "content": content}],
                    token_usage=len(content) // 3,
                    confidence=self._extract_confidence(content),
                )
            except Exception as e:
                return AgentResult(
                    task_id=task.task_id,
                    status=AgentStatus.FAILED,
                    output=f"Direct analysis failed: {e}",
                    trajectory=[{"error": str(e)}],
                    token_usage=0,
                    confidence=0.0,
                )

        messages = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": task_desc},
        ]
        schemas = [t.get_openai_tool_schema() for t in self.tools]

        # 根据任务类型确定 fallback 工具
        desc_lower = (task.description or "").lower()
        academic_keywords = ["论文", "paper", "publication", "学术", "arxiv", "neurips", "icml", "iclr", "scholar", "citation", "文献"]
        fallback_tool = "arxiv_reader" if any(kw in desc_lower for kw in academic_keywords) else "web_search"
        if fallback_tool not in self.tool_map:
            fallback_tool = "web_search" if "web_search" in self.tool_map else next(iter(self.tool_map), fallback_tool)
        
        has_valid_source = False
        had_tool_attempt = False
        has_usable_tool_result = False
        last_content = ""

        for turn in range(self.max_turns):
            try:
                response = await self._call_policy(messages, tools=schemas)
            except RuntimeError as e:
                # 上下文长度超限等致命错误
                trajectory.append({"turn": turn, "error": str(e)})
                return AgentResult(
                    task_id=task.task_id,
                    status=AgentStatus.FAILED,
                    output=str(e),
                    trajectory=trajectory,
                    token_usage=total_tokens,
                    confidence=0.0,
                )

            content = response.get("content", "") or ""
            last_content = content
            tool_calls = response.get("tool_calls", []) or []

            trajectory.append({
                "turn": turn,
                "role": "assistant",
                "content": content,
                "tool_calls": [dict(tc) for tc in tool_calls],
            })

            # 估算 token（简化：字符数 / 3）
            total_tokens += len(json.dumps(messages, ensure_ascii=False)) // 3

            # SEARCH 未取得有效来源时，模型不得提前结束；写回回答并强制下一轮检索。
            if not tool_calls:
                if task.task_type == TaskType.SEARCH and not has_valid_source:
                    messages.append({"role": "assistant", "content": content})
                    messages.append({
                        "role": "user",
                        "content": (
                            f"No valid source has been collected. You MUST call the "
                            f"'{fallback_tool}' tool now. A SEARCH task cannot finish without "
                            "at least one verifiable source."
                        ),
                    })
                    continue
                if self._is_tool_failure_explanation(content):
                    return self._failed_result(task, content, trajectory, total_tokens)
                confidence = self._extract_confidence(content)
                return AgentResult(
                    task_id=task.task_id,
                    status=AgentStatus.SUCCESS,
                    output=content,
                    trajectory=trajectory,
                    token_usage=total_tokens,
                    confidence=confidence,
                )

            tool_results: list[dict[str, Any]] = []
            for tc in tool_calls:
                func = tc.get("function", {})
                tool_name = func.get("name", "")
                try:
                    args = json.loads(func.get("arguments", "{}"))
                except json.JSONDecodeError:
                    args = {}

                executions = await self._execute_tool_with_recovery(tool_name, args)
                had_tool_attempt = True
                for execution in executions:
                    entry = {
                        "turn": turn,
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        **execution,
                    }
                    trajectory.append(entry)

                final_execution = executions[-1]
                result = final_execution["result"]
                actual_tool = final_execution["actual_tool"]
                has_usable_tool_result = has_usable_tool_result or not self._is_tool_error(result)
                has_valid_source = has_valid_source or self._is_valid_source(actual_tool, result, args)
                tool_results.append({
                    "tool_call_id": tc.get("id", ""),
                    "name": actual_tool,
                    "requested_tool": tool_name,
                    "result": result,
                    "executions": executions,
                })

            # 将 assistant message 和 tool results 追加到 messages
            assistant_msg = {
                "role": "assistant",
                "content": content,
            }
            if tool_calls:
                assistant_msg["tool_calls"] = [dict(tc) for tc in tool_calls]
            # 保留 reasoning_content（DeepSeek 推理模型需要传回）
            if response.get("reasoning_content"):
                assistant_msg["reasoning_content"] = response["reasoning_content"]
            messages.append(assistant_msg)

            for tr in tool_results:
                msg_content = json.dumps(
                    {
                        "requested_tool": tr["requested_tool"],
                        "actual_tool": tr["name"],
                        "executions": tr["executions"],
                    },
                    ensure_ascii=False,
                    default=str,
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tr["tool_call_id"],
                    "name": tr["name"],
                    "content": msg_content,
                })

        if task.task_type == TaskType.SEARCH and not has_valid_source:
            return self._failed_result(
                task,
                last_content or "No valid source was collected.",
                trajectory,
                total_tokens,
            )
        if had_tool_attempt and not has_usable_tool_result:
            return self._failed_result(
                task,
                last_content or "No tool produced a usable result.",
                trajectory,
                total_tokens,
            )
        return AgentResult(
            task_id=task.task_id,
            status=AgentStatus.TIMEOUT,
            output="Reached max_turns without final answer.",
            trajectory=trajectory,
            token_usage=total_tokens,
            confidence=0.0,
        )

    async def _call_policy(self, messages: list[dict], *, tools: list[dict]) -> dict:
        """显式传递本轮工具，且只对确实不支持该参数的旧 policy 降级。"""
        try:
            signature = inspect.signature(self.policy)
        except (TypeError, ValueError):
            accepts_tools = True
        else:
            parameters = signature.parameters.values()
            accepts_tools = "tools" in signature.parameters or any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )

        if accepts_tools:
            return await asyncio.to_thread(self.policy, messages, tools=tools)
        return await asyncio.to_thread(self.policy, messages)

    async def _execute_tool_with_recovery(
        self,
        requested_tool: str,
        args: dict,
    ) -> list[dict[str, Any]]:
        """重试主工具，仍失败时执行第一个已配置且实际可用的替代工具。"""
        executions: list[dict[str, Any]] = []
        for attempt in range(1, self.tool_max_attempts + 1):
            result = await self._execute_tool(requested_tool, args)
            execution = {
                "requested_tool": requested_tool,
                "actual_tool": requested_tool,
                "attempt": attempt,
                "fallback_from": None,
                "result": result,
            }
            if self._is_tool_error(result):
                execution["error"] = self._tool_error_message(result)
            executions.append(execution)
            if not self._is_tool_error(result):
                return executions
            if attempt < self.tool_max_attempts and self.tool_retry_delay_seconds:
                await asyncio.sleep(self.tool_retry_delay_seconds)

        fallback = next(
            (
                name
                for name in self.tool_fallbacks.get(requested_tool, [])
                if name in self.tool_map and name != requested_tool
            ),
            None,
        )
        if fallback is None:
            return executions

        fallback_args = self._map_fallback_args(requested_tool, fallback, args)
        result = await self._execute_tool(fallback, fallback_args)
        execution = {
            "requested_tool": requested_tool,
            "actual_tool": fallback,
            "attempt": 1,
            "fallback_from": requested_tool,
            "result": result,
        }
        if self._is_tool_error(result):
            execution["error"] = self._tool_error_message(result)
        executions.append(execution)
        return executions

    @staticmethod
    def _map_fallback_args(requested_tool: str, fallback: str, args: dict) -> dict:
        if requested_tool == "web_search" and fallback == "arxiv_reader":
            mapped = {"query": args.get("query", "")}
            limit = args.get("top_n", args.get("max_results"))
            if limit is not None:
                mapped["max_results"] = limit
            return mapped
        if requested_tool == "arxiv_reader" and fallback == "web_search":
            mapped = {"query": args.get("query") or args.get("paper_id", "")}
            limit = args.get("max_results", args.get("top_n"))
            if limit is not None:
                mapped["top_n"] = limit
            return mapped
        if requested_tool == "browser" and fallback == "web_search":
            return {"query": args.get("url", "")}
        return dict(args)

    @staticmethod
    def _is_tool_error(result: Any) -> bool:
        if isinstance(result, dict) and result.get("error"):
            return True
        if isinstance(result, str):
            lowered = result.strip().lower()
            return lowered.startswith((
                "error",
                "warning",
                "[browser error]",
                "[browser warning]",
                "[filereader error]",
                "[filereader warning]",
            ))
        return False

    @staticmethod
    def _tool_error_message(result: Any) -> str:
        if isinstance(result, dict):
            return str(result.get("error", result))
        return str(result)

    @classmethod
    def _is_valid_source(cls, tool_name: str, result: Any, args: dict) -> bool:
        if cls._is_tool_error(result):
            return False
        if tool_name == "web_search":
            results = result.get("results", []) if isinstance(result, dict) else []
            return any(
                isinstance(item, dict)
                and bool(str(item.get("url", "")).strip())
                and bool(str(item.get("title", "")).strip() or str(item.get("snippet", "")).strip())
                for item in results
            )
        if tool_name == "arxiv_reader":
            papers = result.get("papers", []) if isinstance(result, dict) else []
            return any(
                isinstance(paper, dict)
                and bool(str(paper.get("id", "")).strip() or str(paper.get("pdf_url", "")).strip())
                and bool(str(paper.get("title", "")).strip() or str(paper.get("summary", "")).strip())
                for paper in papers
            )
        if tool_name == "browser":
            url = str(args.get("url", "")).strip().lower()
            return url.startswith(("http://", "https://")) and cls._has_substantive_text(result)
        if tool_name == "file_reader":
            return cls._has_substantive_text(result)
        return False

    @classmethod
    def _has_substantive_text(cls, result: Any) -> bool:
        if not isinstance(result, str) or cls._is_tool_error(result):
            return False
        text = result.strip()
        return len(text) >= 10 and any(character.isalnum() for character in text)

    @staticmethod
    def _failed_result(
        task: SubTask,
        output: str,
        trajectory: list[dict],
        total_tokens: int,
    ) -> AgentResult:
        return AgentResult(
            task_id=task.task_id,
            status=AgentStatus.FAILED,
            output=output,
            trajectory=trajectory,
            token_usage=total_tokens,
            confidence=0.0,
        )

    def _system_prompt(self) -> str:
        return (
            "You are a meticulous research assistant. "
            "Your job is to gather and analyze information using the RIGHT tool for each task. "
            "\n\nAVAILABLE TOOLS:\n"
            "- web_search: General web search for news, market data, industry reports, current events. "
            "  Use this as the FIRST tool for most tasks.\n"
            "- arxiv_reader: Academic paper search (ArXiv / Semantic Scholar). "
            "  USE when the task involves: papers, publications, academic research, citation counts.\n"
            "- browser: Open a URL and extract full webpage text. "
            "  USE after web_search when search results are too short and you need to read the original article in depth.\n"
            "- code_sandbox: Execute Python code for calculations, data processing, simulations. "
            "  USE when the task requires: computing FLOPs, memory usage, statistical analysis, data transformation.\n"
            "- calculator: Quick math evaluation (+, -, *, /, sqrt, log, mean, etc.). "
            "  USE for simple calculations instead of code_sandbox.\n"
            "- notepad: Write/read intermediate notes to avoid forgetting findings during multi-step research. "
            "  USE to record key numbers, conclusions, or next search queries.\n"
            "- file_reader: Read local files (.txt, .md, .pdf, .csv, .json, .docx). "
            "  USE only when the task explicitly references a local file path.\n"
            "\nIMPORTANT RULES:\n"
            "1. You MUST use a tool to find factual information. Do NOT answer from your own knowledge.\n"
            "2. Choose the RIGHT tool based on the task type. You can use MULTIPLE tools in sequence.\n"
            "3. For most research tasks, START with web_search or arxiv_reader.\n"
            "4. If search results are too short, use browser to read the full article.\n"
            "5. If the task involves numbers/calculations, use calculator or code_sandbox.\n"
            "6. You may call tools AT MOST 2 times total. After that you MUST summarize.\n"
            "7. Only after gathering information, provide a concise summary with a confidence score (0-1).\n"
            "8. NEVER greet the user or ask what they want to search — just execute immediately.\n"
            "9. If you have already performed 2 tool calls, do NOT call more — write the final summary now."
        )

    def _system_prompt_direct_analysis(self) -> str:
        return (
            "You are a thoughtful analyst. "
            "The user has asked a question that cannot be answered by web search "
            "(e.g., analyzing a specific private individual, personal advice, or subjective judgment). "
            "Your job is to provide a reasoned analysis based ONLY on the information already provided in the context. "
            "Do NOT make up facts. Clearly state what is known, what can be reasonably inferred, and what remains unknown. "
            "End with a confidence score (0-1)."
        )

    def _is_non_searchable(self, task: SubTask, context: dict) -> bool:
        """启发式判断任务是否无法通过网络搜索获取答案。"""
        desc = (task.description or "").lower()
        query = context.get("query", "").lower()
        combined = desc + " " + query

        # 模式 1：分析/评价特定私人个体（姓名 + 描述性分析）
        if "朋友" in combined or "同学" in combined or "同事" in combined:
            if any(w in combined for w in ["分析", "评价", "是什么样", "性格", "人品"]):
                return True

        # 模式 2：主观建议类（基于个人情况）
        if any(w in combined for w in ["建议我", "我该怎么", "适合我吗", "要不要"]):
            if "朋友" in combined or "我" in query:
                return True

        # 模式 3：明显的个人隐私分析
        if "叫" in combined and any(w in combined for w in ["分析", "评价", "是什么样"]):
            return True

        return False

    def _build_task_prompt(self, task: SubTask, context: dict) -> str:
        """根据 SubTask 和全局上下文构建 user prompt。"""
        desc_lower = (task.description or "").lower()
        
        # 智能工具推荐：根据任务描述关键词匹配
        tool_recommendations = []
        
        # 学术论文类
        academic_keywords = ["论文", "paper", "publication", "学术", "arxiv", "neurips", "icml", "iclr", "scholar", "citation", "文献"]
        if any(kw in desc_lower for kw in academic_keywords):
            tool_recommendations.append("arxiv_reader")
        
        # 计算/数学类
        calc_keywords = ["计算", "flops", "显存", "内存", "参数量", "延迟", "成本", "公式", "数值", "统计", "数学", "公式", "推导"]
        if any(kw in desc_lower for kw in calc_keywords):
            tool_recommendations.append("calculator")
            tool_recommendations.append("code_sandbox")
        
        # 深度阅读类（需要读原文）
        browser_keywords = ["详细", "原文", "全文", "深度", "详细内容", "网页内容", "文章正文"]
        if any(kw in desc_lower for kw in browser_keywords):
            tool_recommendations.append("browser")
        
        # 文件类
        file_keywords = ["文件", "文档", "dataset", "数据集", "pdf", "csv", "json"]
        if any(kw in desc_lower for kw in file_keywords):
            tool_recommendations.append("file_reader")
        
        # 确定首选工具：学术论文类优先用 arxiv_reader，其他先用 web_search
        is_academic = "arxiv_reader" in tool_recommendations
        if is_academic:
            # 学术论文任务：arxiv_reader 优先，web_search 备选
            tool_recommendations = ["arxiv_reader"] + [t for t in tool_recommendations if t != "arxiv_reader"]
        elif not tool_recommendations:
            tool_recommendations.insert(0, "web_search")
        
        primary_tool = tool_recommendations[0]
        secondary_tools = tool_recommendations[1:]
        
        lines = [
            f"## Task: {task.description}",
            f"Type: {task.task_type.value}",
            f"Expected output: {task.expected_type}",
            "",
            f"## RECOMMENDED TOOLS (in priority order): {', '.join(tool_recommendations)}",
        ]
        
        if secondary_tools:
            lines.append(f"Start with '{primary_tool}'. If the task involves numbers/calculations, also use {', '.join(secondary_tools)}.")
        else:
            lines.append(f"Use '{primary_tool}' to gather information.")
        
        lines.extend([
            "",
            "## INSTRUCTIONS:",
            f"1. First, call the '{primary_tool}' tool with a relevant query to gather information.",
            "2. Review the results.",
            f"3. If needed, call '{primary_tool}' ONE MORE time with a refined query.",
            "   You may call tools AT MOST 2 times total. After the 2nd call, you MUST write the final summary.",
            "4. If search results are too short, you may use 'browser' to read the full article (counts as 1 tool call).",
            "5. If calculations are needed, use 'calculator' or 'code_sandbox' (counts as 1 tool call).",
            "6. Finally, summarize your findings in Chinese with a confidence score (0-1).",
            "7. DO NOT greet the user or ask clarifying questions — just execute immediately.",
            "8. IMPORTANT: Your query MUST directly address the task description.",
        ])
        if task.search_hints:
            lines.insert(1, f"Search hints (MUST use these as primary keywords): {', '.join(task.search_hints)}")
        if task.context_keys:
            ctx_parts = []
            for key in task.context_keys:
                if key in context:
                    ctx_parts.append(f"- {key}: {context[key]}")
            if ctx_parts:
                lines.append("\n## Context:")
                lines.extend(ctx_parts)
        return "\n".join(lines)

    async def _execute_tool(self, tool_name: str, args: dict) -> dict:
        """调用具体工具实例。"""
        tool = self.tool_map.get(tool_name)
        if tool is None:
            return {"error": f"Tool '{tool_name}' not found"}
        with trace_block(
            name=f"tool.{tool_name}",
            run_type="tool",
            inputs=args,
            tags=["tool", tool_name],
        ) as trace:
            try:
                result = await tool.execute(**args)
                trace.add_metadata({"status": "success"})
                return result
            except Exception as e:
                message = f"{type(e).__name__}: {e}"
                trace.set_error(message)
                return {"error": message}

    def _is_tool_failure_explanation(self, content: str) -> bool:
        """检测 LLM 回复是否是工具失败的解释说明而非真实研究结果。

        常见模式：额度用完、无法连接、无法搜索等。
        """
        if not content:
            return False
        c = content.lower()
        failure_keywords = [
            "无法通过", "无法执行", "无法使用", "无法获取", "无法访问",
            "额度已用尽", "配额已用完", "额度已用完", "搜索配额",
            "cannot search", "unable to search", "quota exceeded",
            "api key", "额度不足", "余额不足", "余额为", "余额：0",
            "网络错误", "连接失败", "无法连接到",
        ]
        return any(kw in c for kw in failure_keywords)

    def _extract_confidence(self, content: str) -> float:
        """从输出文本中尝试提取置信度分数。"""
        import re
        # 匹配 "Confidence: 0.85" 或 "置信度: 0.85"
        patterns = [
            r"[Cc]onfidence[:\s]+(0\.\d+|1\.0|1)",
            r"置信度[:\s]+(0\.\d+|1\.0|1)",
        ]
        for pat in patterns:
            m = re.search(pat, content)
            if m:
                try:
                    return float(m.group(1))
                except ValueError:
                    continue
        # 默认中等置信度
        return 0.6
