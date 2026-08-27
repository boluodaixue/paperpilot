# PaperPilot 基于 DeepResearch-Agent 的开发路线调整方案

## 1. 调整后的项目定位

PaperPilot 不从零构建完整 Deep Research Agent，而是基于现有 Deep
Research Agent 框架进行增强。

核心定位：

> Evidence-Guided Autonomous Research Extension

即：

在已有 Deep Research Agent 的基础上，引入 Evidence Graph、Research Loop
和 Research Completion Score，使 Agent
能够进行可验证、可持续推进的论文研究。

------------------------------------------------------------------------

## 2. 基础框架策略

采用 qiqihezh/deepresearch-agent 作为基础系统。

已有能力复用：

-   Agent orchestration
-   Research planning
-   Multi-agent execution
-   Search pipeline
-   Report generation

PaperPilot 聚焦新增能力：

-   Evidence Extraction
-   Evidence Graph
-   Research Gap Detection
-   Research Completion Score

------------------------------------------------------------------------

## 3. 新架构设计

    User Question

    ↓

    Research Manager

    ↓

    Deep Research Engine

    ↓

    Dynamic Research Agents

    ↓

    Evidence Extraction Layer

    ↓

    Evidence Graph

    ↓

    Research Gap Analyzer

    ↓

    Research Completion Score

    ↓

    Continue Research?

    ↓

    Final Verified Report

------------------------------------------------------------------------

## 4. 核心改造方向

### 4.1 Report-centric → Evidence-centric

传统流程：

    Research Agent

    ↓

    Final Answer

PaperPilot：

    Research Agent

    ↓

    Claim Extraction

    ↓

    Evidence Extraction

    ↓

    Evidence Storage

    ↓

    Report Generation

每个结论都需要对应：

-   Paper
-   Evidence
-   Claim
-   Relation

------------------------------------------------------------------------

### 4.2 增加 Evidence Graph

数据结构：

    Paper

    ↓

    Evidence

    ↓

    Claim

关系：

-   SUPPORTS
-   CONTRADICTS
-   EXTENDS
-   ANSWERS

用于：

-   证据追踪
-   冲突发现
-   知识缺口分析

------------------------------------------------------------------------

### 4.3 增加 Research Loop

传统 Deep Research：

    Research

    ↓

    Report

    ↓

    End

PaperPilot：

    Research

    ↓

    Evidence Graph Analysis

    ↓

    Gap Detection

    ↓

    Generate New Research Task

    ↓

    Continue Research

    ↓

    Final Report

------------------------------------------------------------------------

### 4.4 Research Completion Score

RCS 不作为项目唯一核心，而作为自主停止机制。

作用：

-   判断研究是否充分
-   避免无限搜索
-   指导下一轮研究

示例：

    RCS = 55%

    缺少：
    - Evaluation evidence
    - Recent papers

    Action:
    继续研究

    ↓

    RCS = 85%

    停止

------------------------------------------------------------------------

## 5. 开发路线

### Phase 1：基于 DeepResearch Agent 跑通 MVP

目标：

快速获得可运行系统。

实现：

-   Question input
-   Paper search
-   Abstract analysis
-   Evidence extraction
-   Report generation

------------------------------------------------------------------------

### Phase 2：Evidence Graph

加入：

-   Paper database
-   Claim database
-   Evidence database
-   Graph relation

技术：

-   SQLite
-   NetworkX

------------------------------------------------------------------------

### Phase 3：Research Manager + Dynamic Research

加入：

-   Research planning
-   Task decomposition
-   Parallel research agents
-   Evidence merge

------------------------------------------------------------------------

### Phase 4：Research Loop

加入：

-   Graph Analyst Agent
-   Gap Detection
-   New research task generation

------------------------------------------------------------------------

### Phase 5：RCS Evaluation

加入：

-   Coverage
-   Evidence Quality
-   Saturation

用于：

-   自动停止
-   实验评价

------------------------------------------------------------------------

## 6. 实验设计

Baseline:

原始 Deep Research Agent

比较：

1.  DeepResearch Agent
2.  -   Evidence Graph
3.  -   Research Loop
4.  -   RCS

评价：

-   Citation Accuracy
-   Evidence Coverage
-   Research Completeness
-   Token Cost
-   Runtime

------------------------------------------------------------------------

## 7. 项目最终描述

PaperPilot:

Built an evidence-guided autonomous literature research agent based on a
Deep Research framework. Enhanced the system with Evidence Graph
construction, research gap detection, and Research Completion Score
driven iterative research.

关键词：

-   Multi-Agent System
-   Deep Research Agent
-   Evidence Graph
-   Autonomous Research
-   LLM Agent
-   Knowledge Graph
-   Literature Research
