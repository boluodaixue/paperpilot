"""
合成 Agent (SummarizerAgent)

将多个 SubTask 的执行结果合成为结构化的研究报告。
区别于 ResearcherAgent 的多轮 tool-calling，Summarizer 是单轮长上下文生成任务：
  - 把所有子结果按置信度排序后拼接为上下文
  - 调用 LLM 一次性生成 Markdown 格式报告
  - 提取引用来源，计算整体置信度
"""
from __future__ import annotations

import json
import re
from typing import Any

from .base_agent import BaseAgent
from ..orchestrator.schemas import SubTask, AgentResult, AgentStatus, ResearchReport
from ..utils.tracing import trace_agent


__all__ = ["SummarizerAgent"]


class SummarizerAgent(BaseAgent):
    """合成 Agent：将子任务结果合成为最终研究报告。

    Attributes:
        max_output_tokens: 报告生成的最大 token 数（通过 policy.max_tokens 控制）。
    """

    def __init__(self, name: str, policy, tools: list | None = None) -> None:
        super().__init__(name, policy, tools)

    @trace_agent(name="summarizer.run", tags=["agent", "summarizer"])
    async def run(self, task: SubTask, context: dict) -> AgentResult:
        """执行合成任务。

        Args:
            task: 通常是一个特殊的 "synthesize" 类型任务。
            context: 全局上下文，必须包含 "results" 和 "query" 键。
                results: list[AgentResult]
                query: str 原始研究问题

        Returns:
            AgentResult，output 字段为 ResearchReport 实例。
        """
        query = context.get("query", "")
        results: list[AgentResult] = context.get("results", [])
        evidence: list = context.get("evidence", [])
        evidence_relations: list = context.get("evidence_relations", [])
        evidence_relations_supports: list = context.get("evidence_relations_supports", [])
        evidence_relations_extends: list = context.get("evidence_relations_extends", [])

        if not results:
            report = ResearchReport(
                query=query,
                content="No sub-task results available to synthesize.",
                confidence=0.0,
            )
            return AgentResult(
                task_id=task.task_id,
                status=AgentStatus.FAILED,
                output=report,
                trajectory=[],
                token_usage=0,
                confidence=0.0,
            )

        # 构建 synthesis prompt
        prompt = self._build_synthesis_prompt(
            query,
            results,
            evidence,
            evidence_relations,
            evidence_relations_supports,
            evidence_relations_extends,
        )
        messages = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": prompt},
        ]

        try:
            # 合成任务不需要工具调用，临时禁用 tools 避免模型进入 tool-calling 模式
            old_tools = getattr(self.policy, "tools", None)
            self.policy.tools = None
            response = self.policy(messages)
            self.policy.tools = old_tools
        except RuntimeError as e:
            return AgentResult(
                task_id=task.task_id,
                status=AgentStatus.FAILED,
                output=str(e),
                trajectory=[{"error": str(e)}],
                token_usage=0,
                confidence=0.0,
            )

        content = response.get("content", "") or ""
        token_usage = len(content) // 3  # 简化估算

        # 解析报告内容，提取来源和置信度
        # 矛盾 + 支持 + 扩展关系一并写入 report.evidence_relations（_format_report 按 relation 分节渲染）
        report = self._parse_report(
            query,
            content,
            results,
            evidence,
            [*evidence_relations, *evidence_relations_supports, *evidence_relations_extends],
        )

        return AgentResult(
            task_id=task.task_id,
            status=AgentStatus.SUCCESS,
            output=report,
            trajectory=[{"role": "assistant", "content": content}],
            token_usage=token_usage,
            confidence=report.confidence,
        )

    def _system_prompt(self) -> str:
        return (
            "You are an expert research synthesizer. "
            "Your task is to integrate multiple research findings into a coherent, well-structured report. "
            "Use Markdown formatting. Cite sources explicitly. "
            "The report body MUST be at least 3000 Chinese characters (or 2000 English words) long. "
            "Write in depth: include background, key findings, detailed analysis, comparisons, and implications. "
            "DO NOT describe what you will do — directly output the synthesized report. "
            "At the end, provide an overall confidence score (0-1) and a summary of key sources."
        )

    def _build_synthesis_prompt(
        self,
        query: str,
        results: list[AgentResult],
        evidence: list | None = None,
        evidence_relations: list | None = None,
        evidence_relations_supports: list | None = None,
        evidence_relations_extends: list | None = None,
    ) -> str:
        """构建 synthesis prompt，按置信度降序排列结果，并注入证据与关系。"""
        sorted_results = sorted(results, key=lambda r: r.confidence, reverse=True)
        evidence = evidence or []
        evidence_relations = evidence_relations or []
        evidence_relations_supports = evidence_relations_supports or []
        evidence_relations_extends = evidence_relations_extends or []

        parts = [
            f"# Research Question\n{query}\n",
            f"# Sub-task Results ({len(results)} total)\n",
        ]
        for i, r in enumerate(sorted_results, 1):
            status_icon = "✓" if r.status == AgentStatus.SUCCESS else "✗"
            parts.append(
                f"## Result {i} [{status_icon}] (confidence: {r.confidence:.2f})\n"
                f"Task: {r.task_id}\n"
                f"Output:\n{r.output}\n"
            )

        if evidence:
            parts.append("\n# Evidence Items (cite these)\n")
            for ev in evidence:
                parts.append(f"- [{ev.evidence_id}] {ev.claim} — ({ev.paper_title})")

        if evidence_relations:
            parts.append("\n# Known Contradictions (resolve these)\n")
            for r in evidence_relations:
                parts.append(
                    f"- [{r.get('source_id')}] vs [{r.get('target_id')}] "
                    f"(weight {r.get('weight', 0):.2f}): {r.get('source_claim', '')} — "
                    f"{r.get('target_claim', '')} ({r.get('source_paper', '')} vs "
                    f"{r.get('target_paper', '')})"
                )

        if evidence_relations_supports:
            parts.append("\n# Supporting Relations\n")
            for r in evidence_relations_supports:
                parts.append(
                    f"- [{r.get('source_id')}] SUPPORTS [{r.get('target_id')}] "
                    f"(weight {r.get('weight', 0):.2f}): {r.get('source_claim', '')} — "
                    f"{r.get('target_claim', '')}"
                )

        if evidence_relations_extends:
            parts.append("\n# Related Extensions\n")
            for r in evidence_relations_extends:
                parts.append(
                    f"- [{r.get('source_id')}] EXTENDS [{r.get('target_id')}] "
                    f"(weight {r.get('weight', 0):.2f}): {r.get('source_claim', '')} — "
                    f"{r.get('target_claim', '')}"
                )

        instructions = [
            "1. Directly write the synthesized report based on the findings above. Do NOT say 'I will synthesize'.\n"
            "2. The report MUST be comprehensive and detailed (at least 3000 Chinese characters or 2000 English words).\n"
            "3. Structure: Executive Summary → Background → Key Findings (with details) → Analysis → Comparisons → Implications → Conclusion.\n"
            "4. Resolve any contradictions between sources.\n"
            "5. Explicitly list all sources cited.\n"
            "6. End with: Overall Confidence: X.XX",
        ]
        # 指令仅在证据存在时给出，避免模型对不存在的 Evidence Items 编造引用
        if evidence:
            instructions.append(
                "7. When a statement is supported by one of the Evidence Items above, "
                "cite it inline as [E-<id>] immediately after that sentence."
            )
        if evidence_relations:
            instructions.append(
                "8. Explicitly address every item in Known Contradictions: for each, state which "
                "side is better supported by the evidence and why."
            )
        parts.append("\n# Instructions\n" + "\n".join(instructions))
        return "\n".join(parts)

    def _parse_report(
        self,
        query: str,
        content: str,
        results: list[AgentResult],
        evidence: list | None = None,
        evidence_relations: list | None = None,
    ) -> ResearchReport:
        """从 LLM 输出中解析 ResearchReport，并基于子任务成功率校准置信度。"""
        evidence = evidence or []
        evidence_relations = evidence_relations or []
        # 1. 从文本中提取 LLM 自评置信度
        llm_confidence = 0.5
        m = re.search(r"[Oo]verall\s+[Cc]onfidence[:\s]+(0\.\d+|1\.0|1)", content)
        if m:
            try:
                llm_confidence = float(m.group(1))
            except ValueError:
                pass

        # 2. 基于子任务成功率计算客观置信度
        total = len(results)
        success = sum(1 for r in results if r.status == AgentStatus.SUCCESS)
        success_rate = success / max(total, 1)

        # 3. 综合置信度 = LLM 自评 × 成功率开根（降低成功率的影响权重）
        confidence = llm_confidence * (success_rate ** 0.5)
        confidence = round(max(0.0, min(1.0, confidence)), 2)

        # 收集来源（从各个子结果的轨迹中提取）
        sources: list[dict] = []
        for r in results:
            if r.status != AgentStatus.SUCCESS:
                continue
            # 简单启发式：从 trajectory 的 tool 结果中提取 url
            for step in r.trajectory:
                if step.get("role") == "tool" and isinstance(step.get("result"), dict):
                    res = step["result"]
                    if "results" in res and isinstance(res["results"], list):
                        for item in res["results"]:
                            if isinstance(item, dict) and "url" in item:
                                sources.append({
                                    "url": item["url"],
                                    "title": item.get("title", ""),
                                    "snippet": item.get("snippet", ""),
                                    "task_id": r.task_id,
                                })
                    elif "papers" in res and isinstance(res["papers"], list):
                        for paper in res["papers"]:
                            if isinstance(paper, dict) and "pdf_url" in paper:
                                sources.append({
                                    "url": paper["pdf_url"],
                                    "title": paper.get("title", ""),
                                    "snippet": paper.get("summary", "")[:200],
                                    "task_id": r.task_id,
                                })

        # 去重
        seen = set()
        unique_sources = []
        for s in sources:
            key = s["url"]
            if key not in seen:
                seen.add(key)
                unique_sources.append(s)

        # 统计实际工具调用次数（遍历所有子任务的 trajectory）
        num_searches = sum(
            len([t for t in r.trajectory if t.get("role") == "tool"])
            for r in results
        )

        return ResearchReport(
            query=query,
            content=content,
            sources=unique_sources,
            confidence=confidence,
            num_searches=num_searches,
            evidence=[ev.to_report_dict() for ev in evidence],
            evidence_relations=[dict(r) for r in evidence_relations],
        )
