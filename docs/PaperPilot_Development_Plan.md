# PaperPilot：Evidence-Guided Autonomous AI Paper Research Agent

## 1. 项目定位

PaperPilot 是一个面向 AI 论文研究场景的 Multi-Agent Research System。

核心目标：

> 让 Agent
> 不只是搜索论文并生成总结，而是能够自主规划研究方向、并行探索论文、提取可验证
> Evidence、构建 Evidence Graph，并根据知识缺口继续深入研究。

项目定位：

-   不是 ChatGPT Paper Summary
-   不是简单 RAG
-   不是固定 Workflow

而是：

**Evidence-Centric Autonomous Research Agent**

------------------------------------------------------------------------

# 2. 总体设计理念

## 2.1 核心问题

传统 Research Agent：

    Question
     ↓
    Search
     ↓
    Summarize
     ↓
    Answer

存在问题：

-   搜索方向固定
-   缺少研究规划
-   结论缺少证据链
-   无法判断是否研究充分
-   无法发现知识缺口

PaperPilot：

    Research Question

    ↓

    Research Planning

    ↓

    Dynamic Agent Forking

    ↓

    Paper Discovery

    ↓

    Evidence Extraction

    ↓

    Evidence Graph

    ↓

    Gap Detection

    ↓

    Further Research

    ↓

    Verified Report

------------------------------------------------------------------------

# 3. 核心创新点

## 3.1 Evidence Graph

核心知识结构。

不是：

    Paper → Summary

而是：

    Paper

     ↓

    Evidence

     ↓

    Claim

每个观点都有来源和证据。

Graph 节点：

-   Research Question
-   Topic
-   Paper
-   Evidence
-   Claim

Graph 边：

-   SUPPORTS
-   CONTRADICTS
-   EXTENDS
-   ANSWERS

------------------------------------------------------------------------

## 3.2 Dynamic Agent Forking

不是固定角色 Agent。

由 Research Manager 根据任务动态生成同质 Research Agent。

例如：

用户：

"Analyze AI Agent Memory evolution"

自动拆：

    Agent 1:
    Memory Architecture

    Agent 2:
    Retrieval Memory

    Agent 3:
    Memory Evaluation

限制：

-   最大 Agent 数量
-   最大递归深度
-   Token / 时间预算

------------------------------------------------------------------------

## 3.3 Research Completion Score (RCS)

用于判断研究是否完成。

目标：

避免：

-   搜太少
-   搜重复
-   无限搜索

第一版：

    RCS =
    0.5 Coverage
    +
    0.3 Evidence Quality
    +
    0.2 Saturation

指标：

Coverage: 研究任务覆盖程度

Evidence Quality: 论文来源质量

Saturation: 新增信息是否减少

------------------------------------------------------------------------

# 4. 总 Agent 架构

                        User Query

                             |

                             v

                  Research Manager Agent

                             |

                      Research Plan

                             |

                  Dynamic Fork Controller

                             |

            +----------------+----------------+

            |                |                |

     Research Agent   Research Agent   Research Agent

            |                |                |

     Paper Search      Paper Search      Paper Search

            |                |                |

     Abstract Screening

            |

     Full Paper Reading

            |

     Evidence Extraction

            |

     Evidence Graph Update

            |

     Gap Detection

            |

     New Research Task

            |

     (Loop)

            |

     Synthesis Agent

            |

     Research Report

            |

     Graph Explorer

------------------------------------------------------------------------

# 5. Agent 设计

## 5.1 Research Manager Agent

职责：

-   理解用户问题
-   生成 Research Plan
-   创建 Research Task
-   分配 Agent

输出：

``` json
{
"task":
"Analyze memory architecture",

"priority":
0.8
}
```

------------------------------------------------------------------------

## 5.2 Research Agent

核心执行 Agent。

流程：

    Search Paper

    ↓

    Abstract Screening

    ↓

    Select Relevant Papers

    ↓

    Read Sections

    ↓

    Extract Claims

    ↓

    Store Evidence

输出：

Claim + Evidence。

------------------------------------------------------------------------

## 5.3 Graph Analyst Agent

职责：

-   分析 Evidence Graph
-   找知识缺口
-   发现冲突

例如：

发现：

    Architecture:
    Enough evidence

    Evaluation:
    Insufficient evidence

生成新的 Research Task。

------------------------------------------------------------------------

## 5.4 Synthesis Agent

职责：

-   读取 Evidence Graph
-   生成最终 Research Report
-   保证引用可追溯

------------------------------------------------------------------------

# 6. Paper Reading Pipeline

## Step 1: Paper Discovery

来源：

-   Arxiv API
-   Semantic Scholar API
-   用户上传 PDF

------------------------------------------------------------------------

## Step 2: Abstract Screening

目的：

减少无效论文阅读。

输出：

    KEEP / DROP

------------------------------------------------------------------------

## Step 3: Section-aware Reading

优先阅读：

-   Method
-   Experiment
-   Ablation
-   Limitation

避免全文无差别输入。

------------------------------------------------------------------------

## Step 4: Evidence Extraction

输出：

``` json
{
"claim":
"Retrieval improves long horizon reasoning",

"evidence":
"Experiment section shows improvement",

"source":
"Paper X"
}
```

------------------------------------------------------------------------

# 7. 技术栈

## Backend

Python

## Agent Framework

LangGraph

负责：

-   Agent state
-   Workflow
-   Loop

## LLM

Claude SDK

负责：

-   Reasoning
-   Tool Calling
-   Paper Understanding

## Paper Source

第一版：

-   Arxiv API

后续：

-   Semantic Scholar
-   OpenAlex

## PDF Processing

PyMuPDF

## Database

SQLite

存储：

-   Paper
-   Claim
-   Evidence
-   Graph Edge

## Graph

NetworkX

## Frontend Demo

Streamlit

------------------------------------------------------------------------

# 8. 开发迭代计划

## Iteration 1：Single Research Agent MVP

目标：

跑通最小闭环。

实现：

    Question

    ↓

    Search Paper

    ↓

    Read Abstract

    ↓

    Extract Claim

    ↓

    Generate Report

暂不做：

-   Multi Agent
-   Graph
-   RCS

时间：

1-2 周

------------------------------------------------------------------------

# Iteration 2：Evidence Graph

目标：

让研究结果可追溯。

实现：

数据库：

    Paper

    Evidence

    Claim

功能：

-   Graph 构建
-   Evidence 查询
-   Citation 展示

时间：

1 周

------------------------------------------------------------------------

# Iteration 3：Multi-Agent Research

目标：

实现动态并行研究。

加入：

Research Manager Agent

流程：

    Question

    ↓

    Research Plan

    ↓

    Fork Agents

    ↓

    Parallel Research

    ↓

    Merge Evidence

时间：

1-2 周

------------------------------------------------------------------------

# Iteration 4：Graph-driven Research Loop

目标：

让 Agent 自主继续研究。

加入：

Graph Analyst Agent

流程：

    Graph

    ↓

    Find Gap

    ↓

    Generate Task

    ↓

    Fork Agent

时间：

1-2 周

------------------------------------------------------------------------

# Iteration 5：Research Completion Score

目标：

实现自主停止。

加入：

RCS。

停止条件：

    RCS > threshold

或者：

Budget exhausted。

时间：

1 周

------------------------------------------------------------------------

# Iteration 6：Demo 与 Evaluation

目标：

形成简历级项目。

实现：

-   Streamlit Graph Explorer
-   Research Trace
-   Benchmark

评价指标：

## Citation Accuracy

引用是否支持观点

## Evidence Coverage

关键方向覆盖程度

## Efficiency

-   Token
-   Time
-   Paper Count

------------------------------------------------------------------------

# 9. 第一版最终 Demo

输入：

    Analyze the evolution of AI Agent Memory.

输出：

## Research Plan

    Memory Architecture
    Retrieval Mechanism
    Evaluation Benchmark

------------------------------------------------------------------------

## Agent Trace

    Agent 1:
    Architecture research

    Agent 2:
    Retrieval research

    Agent 3:
    Benchmark research

------------------------------------------------------------------------

## Evidence Graph

    AI Agent Memory

     ├── Architecture
     │      |
     │      Paper A
     │
     ├── Retrieval
     │      |
     │      Paper B
     │
     └── Evaluation
            |
            Paper C

------------------------------------------------------------------------

## Final Report

带：

-   Claims
-   Citations
-   Evidence Links

------------------------------------------------------------------------

# 10. 项目最终简历描述

PaperPilot:

Built an evidence-guided autonomous literature research agent using
multi-agent planning and dynamic task decomposition. Developed an
Evidence Graph to connect papers, evidence, and claims, enabling
iterative research expansion and citation-grounded report generation.

技术关键词：

-   Multi-Agent System
-   LangGraph
-   Evidence Graph
-   LLM Agent
-   Literature Research
-   Retrieval
-   Knowledge Graph
-   Autonomous Research
