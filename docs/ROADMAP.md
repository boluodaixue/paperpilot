# PaperPilot Roadmap

> 基于两份原始设计（`docs/PaperPilot_Development_Plan.md` LangGraph 版草图、`docs/PaperPilot_DeepResearch_Based_Development_Plan.md` 深研改造版），下面是实际落地状态。

## 目标

Evidence-Centric Autonomous Research Agent：自主规划、并行探索论文、提取可验证 Evidence、构建 Evidence Graph、按知识缺口迭代补研究，直到覆盖充分（RCS）再合成报告。

## 已完成

| 项 | 说明 | 状态 |
|----|------|------|
| Phase 1 | Evidence 提取/存储（`src/evidence/`）+ 报告证据索引 | ✅ |
| Phase 2 | Evidence Graph：SUPPORTS/CONTRADICTS/EXTENDS 边 + SOURCED_FROM/ANSWERS 结构边（SQLite + NetworkX） | ✅ |
| Phase 2.5 | 报告"证据索引/证据关系"节、Web UI（FastAPI + SSE + 侧边栏 + 证据图可视化） | ✅ |
| Phase 3（部分） | 对抗循环优化；同质 Worker Agent 池 + DAG 分层并发（固定规模） | ✅ |
| 工程修复 | LLM 全线程池、UTF-8、env 覆盖、报告持久化、超时放宽 | ✅ |

## 进行中 / 已规划

| 项 | 说明 | 状态 |
|----|------|------|
| **Phase 3（完整）** | Research Manager 动态 fork：研究循环中按证据缺口动态补派同质子 Agent，带预算约束（max 总任务/轮数/饱和停止） | 🔄 |
| **Phase 4** | 图驱动 Research Loop：Gap Detection → 生成新任务 → 补研究 → 迭代 | 🔄 |
| Web UI | 进度流显示"第 N 轮研究 / 发现缺口 / 补派任务" | 🔄 |

## 未做

| 项 | 说明 |
|----|------|
| Phase 5 RCS | `0.5 Coverage + 0.3 Evidence Quality + 0.2 Saturation` 的完整自主停止评分（当前用循环预算兜底做了简版） |
| Abstract Screening | 论文摘要 KEEP/DROP 筛选，减少无效阅读 |
| Section-aware Reading | 优先读 Method/Experiment/Ablation/Limitation 章节，而非全文 |
| Paper 来源扩展 | Semantic Scholar / 用户上传 PDF / OpenAlex（已有 openalex 后端） |

## 关键数据流

```
用户问题 → Research Manager 规划（初始任务）→ 动态派发同质 Agent
→ 论文检索 → Evidence 提取（按子任务打标）→ Evidence Graph 更新
→ Gap Analysis（证据覆盖不足？）→ 是：补派任务再研究（循环，预算约束）
→ 否：合成带溯源引用的最终报告 → 证据索引/关系/图展示
```