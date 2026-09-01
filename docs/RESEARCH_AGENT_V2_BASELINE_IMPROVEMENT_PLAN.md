# Research Agent V2 基线改进计划

## 当前决策

V2 生产路径已回滚到旧版高信息量流程：

```text
Core Question
→ Blue Worker
→ EvidenceItem
→ 轻量 Claim Hygiene
→ EvidenceClaim
→ Supervisor coverage
→ Red / supplemental research
→ Composer 综合
→ Citation Audit
→ Report
```

默认架构继续保持 Legacy；V2 仍为 opt-in 灰度架构。

严格 Proof Graph 保留为实验模块，但不接入 V2 生产主路径：

- SourceDocument / EvidencePassage
- CandidateClaim / SupportAssessment
- EvidenceRequirement coverage
- 独立 Support Verifier
- partial narrowing/reverification
- 离线 checkpoint replay

## 已保留的有效能力

- Supervisor / Blue Worker 派发
- 统一 `acquire_evidence` 获取器
- Tavily 与其他搜索后端回退
- 搜索后自动打开正文
- HTML → Markdown
- PDF → pypdf
- Docling 默认关闭、按需触发、超时熔断
- 结构化依赖启动检查
- Browser 原始错误透传
- 跨 Worker 文档缓存和 URL 去重
- 动态工具、时间和 token 预算
- Red 未解决问题披露
- Citation URL、表格和审计日志安全修复
- 官方域名推断
- preferred domain 未召回时的 `site:domain` 定向重搜

## 不再接入生产路径的严格机制

- Support Verifier 一票否决
- EvidenceRequirement 100% 强制完成门
- Requirement coverage 决定全部报告内容
- Verified Claim 数量上线硬门
- immutable Claim-only Composer
- Proof Graph 对 Red 和 Citation 的强制控制

## 基线评测

回滚完成后，首先使用独立 checkpoint、Vault 和 retrieval DB 运行固定三题：

1. `fin_006`
2. `law_002`
3. `med_004`

每题记录：

- Judge 总分及各维度
- Research/output/termination status
- Evidence 数量
- 独立来源数量
- 一手来源比例
- material citation coverage
- invalid citation count
- Red challenge acceptance/resolution/disclosure
- 报告正文长度和空章节数量
- 工具调用、token 和耗时
- acquisition candidates/opened/cache/fetch errors

这三份结果构成后续所有修改的不可退化基线。

## 阶段一：只改资料获取

目标：提高官方一手来源进入 Candidate URL 池并成功打开的概率。

流程：

```text
普通搜索
→ 排序并检查 preferred domains
→ 没有官方候选
→ 至多两个 site:domain 定向重搜
→ 合并、规范化 URL、去重
→ 优先打开官方正文
```

重点来源：

- 中国法律监管：CAC、NPC、gov.cn
- 欧盟法律监管：EUR-Lex、EDPB、European Commission
- 美国医疗监管：FDA
- 欧盟医疗监管：EMA / EPAR
- 临床注册：ClinicalTrials.gov

阶段门槛：

- 法律/监管 Requirement 官方来源召回 ≥ 70%
- 官方来源正文打开成功率 ≥ 80%
- 打开来源中明显无关内容 ≤ 20%
- Judge 不得低于回滚基线

未达到门槛时，只修 discovery/acquisition，不修改 Claim、Composer、Red 或 Citation。

## 阶段二：轻量 Claim Hygiene

只清除确定性污染：

- 导航和 cookie 文本
- HTML 标签
- Markdown 图片
- 裸 URL
- 完整链接目录
- 超长网页块
- 重复段落
- 明显与 Core Question 无关的标题页或元数据

不启用语义 Verifier，不做一票否决。

阶段门槛：

- Claim 数相较基线下降不得超过 20%
- 导航/URL/HTML 泄漏为 0
- 报告材料性内容长度不得显著下降
- 三题 Judge 均不得下降

## 阶段三：约束 Composer，保留综合能力

Composer 输出结构：

```json
{
  "sections": [
    {
      "heading": "...",
      "assertions": [
        {
          "text": "综合性结论",
          "claim_ids": ["C1", "C2"],
          "type": "fact | inference | recommendation"
        }
      ]
    }
  ],
  "unresolved": []
}
```

规则：

- `fact` 必须携带 Claim IDs
- `inference` 必须明确标注推断和限制
- `recommendation` 必须说明依据和适用范围
- 禁止裸 URL、未知 Evidence ID 和未声明来源
- 允许综合多个 Claims，不强制逐字复述 Claim

阶段门槛：

- 报告主要问题维度不得出现空章节
- material citation coverage ≥ 80%
- invalid citation = 0
- Judge 不得低于基线

## 阶段四：收敛 Citation Audit

Citation Audit 的职责：

- invalid ID / locator：删除或替换
- missing citation：补充引用或降为 provisional
- overclaim：缩窄措辞
- conflict：删除并披露
- 普通不确定性：保留并增加限制

禁止把大量报告正文作为“安全修复”直接删除。

阶段门槛：

- Citation Audit removal rate ≤ 5%
- invalid citation = 0
- material citation coverage ≥ 80%
- 表格结构完整
- audit ledger 不进入 Supported findings

## 阶段五：改进 Red 定向补研

每个 Red challenge 必须映射到：

```text
target_question_ids
target_claim_ids
missing evidence description
suggested query
preferred official domains
expected evidence type
```

每个问题最多一次定向补研。无法解决的 challenge 转为 `unresolved_disclosed`，不得反复消耗预算或伪造 resolved。

阶段门槛：

- high-severity challenge 必须 resolved、grounded rejected 或 disclosed
- unresolved disclosure rate = 100%
- challenge resolution rate 相较基线提升
- 补研后新增证据必须与目标 challenge 有直接 lineage

## 阶段六：跨领域验收

每完成一个阶段，都依次运行：

1. `fin_006`
2. `law_002`
3. `med_004`

最终 V2 切换门槛：

- 三题 Judge 均 ≥ 5
- material citation coverage 均 ≥ 80%
- invalid citation 均为 0
- 无空报告、无大面积空章节
- 非 budget-forced
- Core Question assignment = 100%
- Red 未解决问题披露率 = 100%
- 任一领域不得因另一领域提升而显著退化

## 执行纪律

- 每次只修改一个阶段
- 修改前记录基线，修改后使用相同问题、模型、预算和独立状态重跑
- Judge、报告长度、主要问题覆盖或来源质量明显下降时立即回退该项
- 不用增加总预算掩盖检索、选择或后处理问题
- 不在运行期间混入其他架构实验
- 未通过当前阶段门槛前，不进入下一阶段

## 下一步

运行回滚后的三题基线。基线完成后，只评估当前已保留的 preferred-domain `site:domain` 定向重搜是否提高一手来源召回；暂不修改 Claim、Composer、Citation 或 Red。
