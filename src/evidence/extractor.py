"""
EvidenceExtractor：LLM 驱动的证据提取。

输入论文摘要 + 研究问题，输出原子级 claim + 逐字证据摘录。
所有失败都降级为返回空列表，绝不把异常抛进主流程。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def _system_prompt(max_claims: int) -> str:
    return (
        "You extract atomic, fact-level evidence claims from a research paper abstract. "
        "For each claim, evidence_text MUST be a short verbatim contiguous quote taken "
        "directly from the abstract (exact wording, no paraphrasing). "
        "Output ONLY valid JSON, no markdown: "
        f'{{"claims": [{{"claim": "str", "evidence_text": "str", "confidence": 0.5}}]}} '
        f"Return 1 to {max_claims} claims."
    )


def _normalize_claim(c: dict) -> dict:
    """清洗单条 claim 字段，保证类型与空值兜底。"""
    try:
        confidence = float(c.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    return {
        "claim": str(c.get("claim", "")).strip(),
        "evidence_text": str(c.get("evidence_text", "")).strip(),
        "confidence": max(0.0, min(1.0, confidence)),
    }


def _parse_claims_json(text: str) -> list[dict]:
    """宽松解析 LLM 输出的 claims JSON：剥围栏 → json.loads → 平衡括号 → 正则扫描。"""
    if not text:
        return []
    text = text.strip()
    # 1. 剥离 markdown 代码围栏
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)

    def _extract(data: Any) -> list[dict]:
        if not isinstance(data, dict):
            return []
        claims = data.get("claims")
        if not isinstance(claims, list):
            return []
        return [_normalize_claim(c) for c in claims if isinstance(c, dict)]

    # 2. 直接解析
    try:
        return _extract(json.loads(text))
    except (json.JSONDecodeError, AttributeError):
        pass

    # 3. 找第一个平衡的 {...} 块
    start = text.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return _extract(json.loads(text[start : i + 1]))
                    except (json.JSONDecodeError, AttributeError):
                        break

    # 4. 正则扫描 claim/evidence_text 字段
    pattern = re.compile(
        r'"claim"\s*:\s*"([^"]*)"\s*,\s*"evidence_text"\s*:\s*"([^"]*)"', re.DOTALL
    )
    return [
        _normalize_claim({"claim": m.group(1), "evidence_text": m.group(2)})
        for m in pattern.finditer(text)
    ]


def extract_papers_from_result(result: Any, max_papers: int = 3) -> list[dict]:
    """从 AgentResult.trajectory 中收割 arxiv 论文 dict（防御式遍历，跳过 error 项）。"""
    papers: list[dict] = []
    seen_ids: set[str] = set()
    trajectory = getattr(result, "trajectory", None) or []
    for step in trajectory:
        if not isinstance(step, dict) or step.get("role") != "tool":
            continue
        res = step.get("result")
        if not isinstance(res, dict):
            continue
        paper_list = res.get("papers")
        if not isinstance(paper_list, list):
            continue
        for paper in paper_list:
            if not isinstance(paper, dict) or "error" in paper:
                continue
            pid = paper.get("id")
            if not pid or pid in seen_ids:
                continue
            seen_ids.add(pid)
            papers.append(paper)
            if len(papers) >= max_papers:
                return papers
    return papers


class EvidenceExtractor:
    """LLM 驱动的证据提取器。"""

    def __init__(
        self,
        policy: Any,
        max_claims_per_paper: int = 5,
        max_papers_per_result: int = 3,
    ) -> None:
        self.policy = policy
        self.max_claims_per_paper = max(1, max_claims_per_paper)
        self.max_papers_per_result = max(1, max_papers_per_result)

    async def extract_from_paper(self, paper: dict, query: str) -> list[dict]:
        """从单篇论文摘要提取 claim 列表。任何失败返回空列表。"""
        abstract = paper.get("summary", "") or paper.get("abstract", "") or ""
        title = paper.get("title", "")
        if not abstract.strip():
            return []

        messages = [
            {"role": "system", "content": _system_prompt(self.max_claims_per_paper)},
            {
                "role": "user",
                "content": (
                    f"Research question: {query}\n"
                    f"Paper title: {title}\n"
                    f"Abstract:\n{abstract[:3000]}\n\n"
                    "Extract the claims now."
                ),
            },
        ]
        try:
            # 同步 policy 放入线程池，避免阻塞 asyncio 事件循环
            response = await asyncio.to_thread(self.policy, messages)
            content = response.get("content", "") or ""
            claims = _parse_claims_json(content)
            claims = [
                c
                for c in claims
                if c["claim"] and c["evidence_text"]
            ][: self.max_claims_per_paper]
            if claims:
                logger.info(
                    f"[Evidence] {title[:60]}... → {len(claims)} claims"
                )
            return claims
        except Exception as e:
            logger.warning(
                f"[Evidence] 提取失败 ({title[:60]}...): {type(e).__name__}: {e}"
            )
            return []
