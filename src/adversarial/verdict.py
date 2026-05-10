"""
M5 Red-Blue 对抗降噪循环 — 评判与数据结构层

本模块定义对抗循环中所有核心数据结构（Issue / RedVerdict / FixOperation）
以及评分引擎 VerdictEngine。所有分数区间统一为 [0.0, 10.0]，便于与人类直觉对齐。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


__all__ = [
    "Severity",
    "FixType",
    "Dimension",
    "Issue",
    "RedVerdict",
    "FixOperation",
    "VerdictEngine",
]


# ============================================================================
# 枚举定义
# ============================================================================

class Severity(Enum):
    """问题严重级别，用于计算修复优先级。"""
    CRITICAL = "critical"  # 事实性错误、核心幻觉
    MAJOR = "major"        # 显著不一致、重要遗漏
    MINOR = "minor"        # 措辞偏差、次要来源问题


class FixType(Enum):
    """Blue Agent 修复策略类型。"""
    IN_PLACE = "in_place"      # 原地修正：数字/日期/名字等直接替换
    SUPPLEMENTARY = "search"   # 补充搜索：unsourced claims → 触发新搜索
    REMOVAL = "removal"        # 移除：高置信幻觉段落直接删除


class Dimension(Enum):
    """Red Agent 五维度攻击维度。"""
    FACTUAL = "fact_check"      # 事实核查
    HALLUCINATION = "hallucination"  # 幻觉检测
    LOGICAL = "logical"         # 逻辑一致性
    SOURCE_CREDIBILITY = "source_credibility"  # 来源可信度
    COVERAGE = "coverage"       # 覆盖完整度


# ============================================================================
# 数据类定义
# ============================================================================

@dataclass
class Issue:
    """Red Agent 发现的单条问题。

    Attributes:
        severity: 严重级别 (critical / major / minor)。
        dimension: 所属攻击维度。
        description: 自然语言描述，传给 Blue Agent 的指导信息。
        location: 问题在报告中的位置标记，如段落索引或引用标记。
        fix_type: 建议的修复类型。
        evidence: 支撑该 issue 判定的证据片段（如 source 原文）。
    """
    severity: Severity
    dimension: Dimension
    description: str
    location: str = ""
    fix_type: FixType = FixType.IN_PLACE
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity.value,
            "dimension": self.dimension.value,
            "description": self.description,
            "location": self.location,
            "fix_type": self.fix_type.value,
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Issue":
        return cls(
            severity=Severity(data.get("severity", "minor")),
            dimension=Dimension(data.get("dimension", "fact_check")),
            description=data.get("description", ""),
            location=data.get("location", ""),
            fix_type=FixType(data.get("fix_type", "in_place")),
            evidence=data.get("evidence", ""),
        )

    def __hash__(self) -> int:
        """用于 resolved_issues 集合去重：基于核心字段生成确定性 hash。"""
        return hash((self.severity, self.dimension, self.description, self.location, self.fix_type))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Issue):
            return NotImplemented
        return (
            self.severity == other.severity
            and self.dimension == other.dimension
            and self.description == other.description
            and self.location == other.location
            and self.fix_type == other.fix_type
        )


@dataclass
class RedVerdict:
    """Red Agent 对单份报告的完整攻击结果。

    Attributes:
        dimension_scores: 五维度分数，键为 Dimension，值为 [0, 10] 浮点数。
        overall_score: 加权综合分。
        issues: 发现的所有问题列表。
        raw_feedback: 原始模型输出，用于审计和调试。
    """
    dimension_scores: dict[Dimension, float] = field(default_factory=dict)
    overall_score: float = 0.0
    issues: list[Issue] = field(default_factory=list)
    raw_feedback: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension_scores": {k.value: v for k, v in self.dimension_scores.items()},
            "overall_score": self.overall_score,
            "issues": [i.to_dict() for i in self.issues],
            "raw_feedback": self.raw_feedback,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RedVerdict":
        return cls(
            dimension_scores={
                Dimension(k): v
                for k, v in data.get("dimension_scores", {}).items()
            },
            overall_score=data.get("overall_score", 0.0),
            issues=[Issue.from_dict(i) for i in data.get("issues", [])],
            raw_feedback=data.get("raw_feedback", ""),
        )


@dataclass
class FixOperation:
    """Blue Agent 执行的单次修复操作记录。

    Attributes:
        issue: 被修复的原始问题。
        action: 实际采取的动作描述。
        success: 修复是否成功通过 self_verify。
        detail: 详细变更内容，如修改前后对比。
    """
    issue: Issue
    action: str = ""
    success: bool = False
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue": self.issue.to_dict(),
            "action": self.action,
            "success": self.success,
            "detail": self.detail,
        }


# ============================================================================
# 评分引擎
# ============================================================================

class VerdictEngine:
    """Red-Blue 对抗循环的评分引擎。

    设计决策：
    1. 五维度权重与项目计划严格对齐，总和为 1.0。
    2. 所有输入分数假设已归一化到 [0.0, 10.0]。
    3. 提供 round-trip 序列化（dict / json）以便持久化审计。
    """

    # 五维度权重（与项目计划一致）
    DIMENSION_WEIGHTS: dict[Dimension, float] = {
        Dimension.FACTUAL: 0.30,
        Dimension.HALLUCINATION: 0.25,
        Dimension.LOGICAL: 0.20,
        Dimension.SOURCE_CREDIBILITY: 0.15,
        Dimension.COVERAGE: 0.10,
    }

    # severity → 数值映射（用于优先级计算）
    SEVERITY_WEIGHTS: dict[Severity, float] = {
        Severity.CRITICAL: 10.0,
        Severity.MAJOR: 5.0,
        Severity.MINOR: 1.0,
    }

    # fix_type → 难度系数（用于优先级计算）
    FIX_DIFFICULTY: dict[FixType, float] = {
        FixType.IN_PLACE: 1.0,
        FixType.REMOVAL: 0.8,
        FixType.SUPPLEMENTARY: 0.6,
    }

    @classmethod
    def compute_overall(cls, dimension_scores: dict[Dimension, float]) -> float:
        """计算加权综合分。

        Args:
            dimension_scores: 五维度分数字典，每个值应在 [0.0, 10.0]。

        Returns:
            加权平均分，范围 [0.0, 10.0]。
        """
        if not dimension_scores:
            return 0.0
        total = 0.0
        weight_sum = 0.0
        for dim, score in dimension_scores.items():
            w = cls.DIMENSION_WEIGHTS.get(dim, 0.0)
            total += w * max(0.0, min(10.0, score))
            weight_sum += w
        if weight_sum == 0.0:
            return 0.0
        return total / weight_sum

    @classmethod
    def compute_delta(
        cls,
        prev: dict[Dimension, float],
        curr: dict[Dimension, float],
    ) -> float:
        """计算两轮间的分数变化量 Δ（欧氏距离）。

        设计决策：使用欧氏距离而非简单绝对差，能同时捕捉多维度波动。
        如果某维度在某一字典中缺失，以 0.0 补齐。

        Args:
            prev: 上一轮五维度分数。
            curr: 当前轮五维度分数。

        Returns:
            非负浮点数，越小表示变化越平缓。
        """
        all_dims = set(prev.keys()) | set(curr.keys())
        if not all_dims:
            return 0.0
        sq_sum = 0.0
        for dim in all_dims:
            p = max(0.0, min(10.0, prev.get(dim, 0.0)))
            c = max(0.0, min(10.0, curr.get(dim, 0.0)))
            sq_sum += (c - p) ** 2
        return math.sqrt(sq_sum)

    @classmethod
    def compute_priority(cls, issue: Issue) -> float:
        """计算 Issue 的修复优先级。

        公式: priority = severity_weight × dimension_weight × fix_difficulty
        优先级越高，越应该优先处理。

        Args:
            issue: 待计算优先级的问题。

        Returns:
            优先级分数（无上限，越大越优先）。
        """
        sw = cls.SEVERITY_WEIGHTS.get(issue.severity, 1.0)
        dw = cls.DIMENSION_WEIGHTS.get(issue.dimension, 0.1)
        fd = cls.FIX_DIFFICULTY.get(issue.fix_type, 1.0)
        return sw * dw * fd

    @staticmethod
    def to_json(verdict: RedVerdict, indent: int = 2) -> str:
        """将 RedVerdict 序列化为 JSON 字符串。"""
        return json.dumps(verdict.to_dict(), ensure_ascii=False, indent=indent)

    @staticmethod
    def from_json(raw: str) -> RedVerdict:
        """从 JSON 字符串反序列化 RedVerdict。"""
        return RedVerdict.from_dict(json.loads(raw))
