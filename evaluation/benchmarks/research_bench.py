#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
evaluation/benchmarks/research_bench.py
================================================================================
自建深度研究评测集 (ResearchBench)。

包含 20 道跨领域（科技、医疗、金融等）深度研究题目。
每道题附带 expected_topics（期望覆盖的子主题）和 ground_truth（关键事实）。
================================================================================
"""

from __future__ import annotations

import json
import os
from typing import Any


class ResearchBench:
    """
    自建深度研究评测集。
    """

    # 内置 20 道评测题
    DEFAULT_QUESTIONS: list[dict[str, Any]] = [
        {
            "id": "tech_001",
            "domain": "科技",
            "query": "对比分析 2024 年主流大语言模型（GPT-4o、Claude 3.5、Gemini 1.5、Qwen2.5）在中文推理、代码生成和长上下文任务上的表现差异，并分析其技术路线差异。",
            "expected_topics": ["中文推理", "代码生成", "长上下文", "技术路线", "GPT-4o", "Claude 3.5", "Gemini 1.5", "Qwen2.5"],
            "ground_truth": {
                "GPT-4o": "OpenAI 发布于 2024 年 5 月，原生多模态",
                "Claude 3.5 Sonnet": "Anthropic 发布于 2024 年 6 月，Artifacts 功能",
                "Gemini 1.5 Pro": "Google 发布，1M+ token 上下文窗口",
                "Qwen2.5": "阿里巴巴发布，开源并支持 128K 上下文",
            },
        },
        {
            "id": "tech_002",
            "domain": "科技",
            "query": "分析当前 AI 芯片市场格局，比较 NVIDIA、AMD、Intel 及中国厂商（华为昇腾、寒武纪）在训练与推理场景下的竞争力。",
            "expected_topics": ["NVIDIA", "AMD", "Intel", "华为昇腾", "寒武纪", "训练", "推理", "AI芯片"],
            "ground_truth": {
                "NVIDIA": "H100/H200 占据训练市场主导地位",
                "AMD": "MI300 系列作为追赶者",
                "昇腾": "华为 AI 芯片，受限于先进制程",
            },
        },
        {
            "id": "med_001",
            "domain": "医疗",
            "query": "综述 2023-2024 年 mRNA 癌症疫苗临床试验进展，分析其技术原理、关键试验数据及面临的挑战。",
            "expected_topics": ["mRNA", "癌症疫苗", "临床试验", "技术原理", "关键数据", "挑战"],
            "ground_truth": {
                "Moderna": "mRNA-4157 联合 Keytruda 的 2b 期临床结果",
                "BioNTech": "BNT122 在结直肠癌中的研究",
                "技术原理": "个性化新抗原疫苗",
            },
        },
        {
            "id": "med_002",
            "domain": "医疗",
            "query": "评估 GLP-1 受体激动剂（司美格鲁肽、替尔泊肽）在减重以外的潜在医疗应用，包括心血管保护、认知功能和成瘾治疗。",
            "expected_topics": ["GLP-1", "司美格鲁肽", "替尔泊肽", "心血管", "认知功能", "成瘾治疗"],
            "ground_truth": {
                "SELECT 试验": "司美格鲁肽降低心血管风险 20%",
                "SURMOUNT": "替尔泊肽减重效果",
            },
        },
        {
            "id": "fin_001",
            "domain": "金融",
            "query": "分析美联储 2024 年降息周期对全球资本流动、新兴市场汇率和中国货币政策的影响路径。",
            "expected_topics": ["美联储", "降息", "资本流动", "新兴市场", "汇率", "中国货币政策"],
            "ground_truth": {
                "降息": "2024 年 9 月首次降息 50bp",
                "新兴市场": "资本回流与汇率波动",
            },
        },
        {
            "id": "fin_002",
            "domain": "金融",
            "query": "比较比特币现货 ETF 与黄金 ETF 在机构资产配置中的异同，分析其风险收益特征和监管差异。",
            "expected_topics": ["比特币ETF", "黄金ETF", "机构配置", "风险收益", "监管"],
            "ground_truth": {
                "现货比特币 ETF": "2024 年 1 月美国 SEC 批准",
                "IBIT": "BlackRock 比特币 ETF 规模",
            },
        },
        {
            "id": "tech_003",
            "domain": "科技",
            "query": "探讨具身智能（Embodied AI）在机器人领域的最新进展，分析世界模型、触觉感知和任务规划三大技术瓶颈。",
            "expected_topics": ["具身智能", "机器人", "世界模型", "触觉感知", "任务规划"],
            "ground_truth": {
                "Figure AI": "人形机器人 Figure 02",
                "Tesla Optimus": "特斯拉人形机器人进展",
            },
        },
        {
            "id": "tech_004",
            "domain": "科技",
            "query": "分析 RISC-V 架构在服务器和 AI 加速器领域的生态发展现状，对比 x86 和 ARM 的优劣势。",
            "expected_topics": ["RISC-V", "服务器", "AI加速器", "x86", "ARM"],
            "ground_truth": {
                "RISC-V": "开源指令集架构",
                "SiFive": "高性能 RISC-V 处理器",
            },
        },
        {
            "id": "med_003",
            "domain": "医疗",
            "query": "综述阿尔茨海默病早期诊断生物标志物（血液 p-tau217、Aβ42/40 比值）的最新临床验证进展。",
            "expected_topics": ["阿尔茨海默病", "早期诊断", "p-tau217", "Aβ42/40", "生物标志物"],
            "ground_truth": {
                "p-tau217": "血液检测灵敏度超过 90%",
                "Aβ PET": "与血液标志物的一致性",
            },
        },
        {
            "id": "med_004",
            "domain": "医疗",
            "query": "评估 CRISPR 基因编辑疗法在镰状细胞病和 β 地中海贫血中的临床疗效与长期安全性数据。",
            "expected_topics": ["CRISPR", "基因编辑", "镰状细胞病", "地中海贫血", "安全性"],
            "ground_truth": {
                "Casgevy": "首个获批的 CRISPR 基因编辑疗法",
                "Vertex": "与 CRISPR Therapeutics 合作",
            },
        },
        {
            "id": "fin_003",
            "domain": "金融",
            "query": "分析中国地方政府债务化解的最新政策工具（特殊再融资债券、债务置换）及其对银行体系的影响。",
            "expected_topics": ["地方政府债务", "再融资债券", "债务置换", "银行体系"],
            "ground_truth": {
                "特殊再融资债券": "2023-2024 年大规模发行",
                "化债": "一揽子化债方案",
            },
        },
        {
            "id": "fin_004",
            "domain": "金融",
            "query": "比较被动指数基金（ETF）与主动管理基金在 2020-2024 年间的风险调整后收益表现，分析其背后的市场结构变化。",
            "expected_topics": ["ETF", "主动管理基金", "风险调整后收益", "市场结构"],
            "ground_truth": {
                "SPIVA": "标普指数 vs 主动基金长期业绩对比",
                "费率": "ETF 低费率优势",
            },
        },
        {
            "id": "tech_005",
            "domain": "科技",
            "query": "探讨空间计算（Spatial Computing）和 Apple Vision Pro 对混合现实产业生态的影响，分析内容创作、企业应用和消费级落地的关键障碍。",
            "expected_topics": ["空间计算", "Apple Vision Pro", "混合现实", "内容创作", "企业应用"],
            "ground_truth": {
                "Vision Pro": "苹果首款空间计算设备，2024 年发售",
                "passthrough": "VST 视频透视技术",
            },
        },
        {
            "id": "tech_006",
            "domain": "科技",
            "query": "分析自动驾驶 L3/L4 级别在 2024 年的商业化落地进展，比较 Waymo、特斯拉 FSD 和中国厂商（百度、小鹏、华为）的技术路线差异。",
            "expected_topics": ["自动驾驶", "L3", "L4", "Waymo", "特斯拉FSD", "百度", "小鹏", "华为"],
            "ground_truth": {
                "Waymo": "纯视觉+激光雷达融合方案，Robotaxi 运营",
                "特斯拉 FSD": "端到端神经网络，纯视觉方案",
            },
        },
        {
            "id": "med_005",
            "domain": "医疗",
            "query": "综述 2024 年 WHO 关注的 X 疾病（Disease X）大流行防范准备框架，分析疫苗平台技术、监测网络和全球治理机制。",
            "expected_topics": ["Disease X", "WHO", "大流行防范", "疫苗平台", "监测网络", "全球治理"],
            "ground_truth": {
                "Disease X": "WHO 定义的下一次未知大流行病原体",
                "100 Days Mission": "100 天内开发疫苗的目标",
            },
        },
        {
            "id": "med_006",
            "domain": "医疗",
            "query": "评估数字疗法（Digital Therapeutics）在慢病管理（糖尿病、高血压、抑郁症）中的临床证据、监管路径和商业化挑战。",
            "expected_topics": ["数字疗法", "慢病管理", "糖尿病", "高血压", "抑郁症", "监管"],
            "ground_truth": {
                "DTx": "经临床验证的软件干预手段",
                "Pear Therapeutics": "破产案例与商业化困境",
            },
        },
        {
            "id": "fin_005",
            "domain": "金融",
            "query": "分析人工智能对保险行业精算、承保和理赔环节的影响，评估保险科技（InsurTech）初创企业的竞争格局。",
            "expected_topics": ["人工智能", "保险", "精算", "承保", "理赔", "InsurTech"],
            "ground_truth": {
                " Lemonade": "AI 驱动的保险理赔",
                " telematics": "UBI 基于使用的保险",
            },
        },
        {
            "id": "fin_006",
            "domain": "金融",
            "query": "比较绿色债券（Green Bond）与可持续发展挂钩债券（SLB）在募集资金用途、信息披露和投资者保护方面的差异。",
            "expected_topics": ["绿色债券", "可持续发展挂钩债券", "募集资金", "信息披露", "投资者保护"],
            "ground_truth": {
                "ICMA": "绿色债券原则 GBP",
                "SLB": "票率与可持续发展 KPI 挂钩",
            },
        },
        {
            "id": "tech_007",
            "domain": "科技",
            "query": "探讨量子计算在密码学（后量子密码 PQC）和药物发现领域的应用前景，分析 NIST 标准化进程和当前技术瓶颈。",
            "expected_topics": ["量子计算", "后量子密码", "PQC", "药物发现", "NIST"],
            "ground_truth": {
                "NIST PQC": "2024 年发布首批标准化算法",
                "CRYSTALS-Kyber": "密钥封装机制标准",
            },
        },
        {
            "id": "tech_008",
            "domain": "科技",
            "query": "分析生成式 AI 在软件开发领域的应用现状（GitHub Copilot、Devin 等），评估其对开发者生产力、代码质量和软件工程教育的影响。",
            "expected_topics": ["生成式AI", "软件开发", "GitHub Copilot", "Devin", "开发者生产力", "代码质量"],
            "ground_truth": {
                "GitHub Copilot": "基于 OpenAI Codex 的代码补全工具",
                "Devin": "Cognition AI 发布的全自主 AI 软件工程师",
            },
        },
        # ------------------------------------------------------------------
        # 教育 (2题)
        # ------------------------------------------------------------------
        {
            "id": "edu_001",
            "domain": "教育",
            "query": "评估自适应学习系统（如 Khan Academy、松鼠 AI）在 K-12 数学教育中的效果，分析其个性化推荐算法、学习效果量化指标和师生接受度。",
            "expected_topics": ["自适应学习", "K-12", "数学教育", "个性化推荐", "学习效果", "Khan Academy", "松鼠AI"],
            "ground_truth": {
                "Khan Academy": "非营利性教育平台，提供免费个性化练习",
                "松鼠 AI": "中国自适应学习公司，智适应教育系统",
                "效果": "自适应学习平均提升成绩 10-20%",
            },
        },
        {
            "id": "edu_002",
            "domain": "教育",
            "query": "对比分析中美两国 STEM 教育的政策差异、课程设计和师资培养模式，评估中国「双减」政策对 STEM 课外培训的影响。",
            "expected_topics": ["STEM教育", "中美对比", "双减政策", "课程设计", "师资培养", "课外培训"],
            "ground_truth": {
                "双减": "2021 年中国减轻义务教育阶段学生作业和校外培训负担",
                "STEM": "科学、技术、工程、数学跨学科教育",
                "Next Generation Science Standards": "美国 K-12 科学教育标准",
            },
        },
        # ------------------------------------------------------------------
        # 法律 (2题)
        # ------------------------------------------------------------------
        {
            "id": "law_001",
            "domain": "法律",
            "query": "分析生成式 AI 训练数据中的版权问题，比较美国合理使用原则（Fair Use）与欧盟《AI 法案》在训练数据授权方面的法律冲突。",
            "expected_topics": ["AI版权", "训练数据", "Fair Use", "AI法案", "欧盟", "纽约时报诉OpenAI"],
            "ground_truth": {
                "纽约时报诉OpenAI": "2023 年纽约时报起诉 OpenAI 和微软侵犯版权",
                "欧盟AI法案": "2024 年生效的全球首部全面 AI 监管法规",
                "Fair Use": "美国版权法中的合理使用抗辩",
            },
        },
        {
            "id": "law_002",
            "domain": "法律",
            "query": "评估中国《个人信息保护法》和欧盟 GDPR 在数据跨境传输规则上的差异，分析对企业出海合规成本的影响。",
            "expected_topics": ["个人信息保护法", "GDPR", "数据跨境", "合规成本", "企业出海", "标准合同条款"],
            "ground_truth": {
                "GDPR": "欧盟通用数据保护条例，2018 年生效",
                "个人信息保护法": "中国 2021 年生效的数据隐私法规",
                "标准合同条款": "SCCs，数据跨境传输的主要合规工具",
            },
        },
        # ------------------------------------------------------------------
        # 能源 (2题)
        # ------------------------------------------------------------------
        {
            "id": "energy_001",
            "domain": "能源",
            "query": "对比固态电池、钠离子电池和磷酸铁锂电池在能量密度、安全性和成本上的技术路线差异，评估其对电动车产业的影响。",
            "expected_topics": ["固态电池", "钠离子电池", "磷酸铁锂", "能量密度", "电动车", "宁德时代", "QuantumScape"],
            "ground_truth": {
                "固态电池": "能量密度可达 500 Wh/kg，预计 2027-2030 量产",
                "宁德时代": "全球动力电池龙头，发布凝聚态电池",
                "磷酸铁锂": "成本低、安全性高，但能量密度约 160 Wh/kg",
            },
        },
        {
            "id": "energy_002",
            "domain": "能源",
            "query": "分析中国光伏产业从硅料到组件的全产业链竞争力，评估美国《通胀削减法案》(IRA) 对中国光伏出口的影响。",
            "expected_topics": ["光伏", "硅料", "组件", "IRA", "通胀削减法案", "隆基绿能", "通威股份"],
            "ground_truth": {
                "隆基绿能": "全球最大单晶硅片制造商",
                "IRA": "美国 2022 年通胀削减法案，提供光伏税收抵免",
                "中国光伏": "占全球组件产能 80% 以上",
            },
        },
        # ------------------------------------------------------------------
        # 消费零售 (2题)
        # ------------------------------------------------------------------
        {
            "id": "retail_001",
            "domain": "消费",
            "query": "评估直播电商（抖音电商、淘宝直播）对传统货架电商的替代效应，分析其供应链模式、主播佣金结构和退货率问题。",
            "expected_topics": ["直播电商", "抖音电商", "淘宝直播", "货架电商", "主播佣金", "退货率"],
            "ground_truth": {
                "直播电商": "2024 年中国直播电商规模预计超 5 万亿元",
                "退货率": "直播电商退货率 30-50%，远高于传统电商",
                "东方甄选": "新东方旗下直播带货品牌",
            },
        },
        {
            "id": "retail_002",
            "domain": "消费",
            "query": "分析中国新消费品牌（喜茶、完美日记、泡泡玛特）的崛起路径、供应链策略和海外扩张挑战。",
            "expected_topics": ["新消费", "喜茶", "完美日记", "泡泡玛特", "DTC", "海外扩张"],
            "ground_truth": {
                "泡泡玛特": "盲盒潮玩龙头，海外收入占比持续提升",
                "喜茶": "新茶饮代表品牌，开放加盟加速下沉",
                "完美日记": "逸仙电商旗下美妆品牌，面临盈利压力",
            },
        },
        # ------------------------------------------------------------------
        # 汽车 (2题)
        # ------------------------------------------------------------------
        {
            "id": "auto_001",
            "domain": "汽车",
            "query": "对比比亚迪、特斯拉和理想汽车在增程式/纯电技术路线、智能驾驶能力和全球化策略上的差异。",
            "expected_topics": ["比亚迪", "特斯拉", "理想汽车", "增程式", "智能驾驶", "全球化"],
            "ground_truth": {
                "比亚迪": "2024 年全球新能源车销量第一，垂直整合模式",
                "理想汽车": "增程式 SUV 路线，家庭用户定位",
                "FSD": "特斯拉完全自动驾驶能力，端到端神经网络",
            },
        },
        {
            "id": "auto_002",
            "domain": "汽车",
            "query": "评估 2024-2025 年中国新能源车渗透率超过 50% 后对燃油车产业链（经销商、加油站、零部件）的冲击和转型路径。",
            "expected_topics": ["新能源车渗透率", "燃油车", "经销商", "加油站", "零部件", "转型"],
            "ground_truth": {
                "渗透率": "2024 年中国新能源车零售渗透率突破 50%",
                "经销商": "传统 4S 店大面积关闭或转型新能源",
                "充电桩": "公共充电桩数量快速增长",
            },
        },
        # ------------------------------------------------------------------
        # 游戏 (2题)
        # ------------------------------------------------------------------
        {
            "id": "game_001",
            "domain": "游戏",
            "query": "分析 AI NPC、程序化内容生成（PCG）和动态难度调整（DDA）在游戏开发中的应用现状，评估其对游戏体验和开发成本的影响。",
            "expected_topics": ["AI NPC", "PCG", "程序化生成", "动态难度", "游戏体验", "开发成本"],
            "ground_truth": {
                "AI NPC": "英伟达 ACE、网易伏羲等 AI 角色技术",
                "PCG": "《无人深空》《我的世界》为代表的程序化生成",
                "DDA": "动态难度调整，根据玩家能力实时调节",
            },
        },
        {
            "id": "game_002",
            "domain": "游戏",
            "query": "评估中国游戏出海（《原神》《PUBG Mobile》《黑神话：悟空》）的全球化策略、文化本地化挑战和各国监管差异。",
            "expected_topics": ["游戏出海", "原神", "PUBG Mobile", "黑神话悟空", "本地化", "监管"],
            "ground_truth": {
                "原神": "米哈游开发，全球收入最高的国产游戏之一",
                "黑神话悟空": "游戏科学开发，2024 年 3A 动作游戏",
                "版号": "中国游戏出版审批制度",
            },
        },
        # ------------------------------------------------------------------
        # 传媒 (1题)
        # ------------------------------------------------------------------
        {
            "id": "media_001",
            "domain": "传媒",
            "query": "分析短视频平台（TikTok / 抖音）推荐算法的核心机制，评估其对用户注意力、内容创作生态和信息茧房的影响。",
            "expected_topics": ["TikTok", "抖音", "推荐算法", "注意力经济", "内容生态", "信息茧房"],
            "ground_truth": {
                "算法": "协同过滤 + 深度学习排序，多目标优化",
                "信息茧房": "算法推荐导致用户视野窄化",
                "TikTok": "全球月活超 15 亿，字节跳动旗下",
            },
        },
        {
            "id": "cross_001",
            "domain": "交叉",
            "query": "分析全球半导体供应链的地缘政治风险，评估台积电、三星和 Intel 在先进制程上的产能分布及各国的「芯片法案」补贴效果。",
            "expected_topics": ["半导体供应链", "地缘政治", "台积电", "三星", "Intel", "芯片法案", "先进制程"],
            "ground_truth": {
                "台积电": "全球最先进制程（3nm/2nm）主要制造商",
                "CHIPS Act": "美国 2022 年芯片法案，补贴 520 亿美元",
                "产能分布": "台湾占全球先进芯片代工 90% 以上",
            },
        },
        {
            "id": "cross_002",
            "domain": "交叉",
            "query": "评估 AI 制药（AlphaFold、Atomwise 等）在靶点发现和临床试验设计中的进展，分析其对传统 Pharma 研发投入回报率的潜在改变。",
            "expected_topics": ["AI制药", "AlphaFold", "靶点发现", "临床试验", "Pharma", "研发回报率"],
            "ground_truth": {
                "AlphaFold": "DeepMind 开发的蛋白质结构预测系统",
                "AI制药": "可缩短药物发现周期 30-50%",
                "Atomwise": "AI 驱动的虚拟筛选平台",
            },
        },
    ]

    def __init__(self, data_path: str | None = None) -> None:
        """
        初始化评测集。

        Args:
            data_path: 外部 JSON 文件路径。若为 None 则使用内置题库。
        """
        if data_path and os.path.exists(data_path):
            with open(data_path, "r", encoding="utf-8") as f:
                self.questions = json.load(f)
        else:
            self.questions = self.DEFAULT_QUESTIONS

    def get_questions(
        self,
        domain: str | None = None,
        n: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        获取评测题目。

        Args:
            domain: 按领域过滤（科技/医疗/金融）。
            n: 返回前 n 道题。

        Returns:
            题目列表。
        """
        result = self.questions
        if domain:
            result = [q for q in result if q.get("domain") == domain]
        if n is not None:
            result = result[:n]
        return result

    def evaluate_report(
        self,
        report: str,
        question_id: str,
        metrics_weights: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """
        对单篇研究报告进行评测。

        Args:
            report: 生成的研究报告文本。
            question_id: 对应题目的 ID。
            metrics_weights: 自定义指标权重。

        Returns:
            包含各维度得分和综合得分的字典。
        """
        from evaluation.metrics.rule_based import RuleBasedMetrics

        q = next((x for x in self.questions if x["id"] == question_id), None)
        if q is None:
            raise ValueError(f"未找到题目 ID: {question_id}")

        expected_topics = q.get("expected_topics", [])
        ground_truth = q.get("ground_truth", {})

        factual_str = RuleBasedMetrics.fact_accuracy(report, ground_truth)
        factual_sem = RuleBasedMetrics.semantic_fact_accuracy(report, ground_truth, threshold=0.65)
        hallucination = RuleBasedMetrics.hallucination_rate(report)
        citation = RuleBasedMetrics.citation_coverage(report)
        logic = RuleBasedMetrics.logical_consistency(report)
        comprehensive = RuleBasedMetrics.comprehensiveness(report, expected_topics)

        # bias 维度用 (1 - hallucination_rate) 作为代理
        bias_score = max(0.0, 1.0 - hallucination)

        metrics = {
            "factual_accuracy_str": factual_str,
            "factual_accuracy_sem": factual_sem,
            "logical_consistency": logic,
            "citation_coverage": citation,
            "bias": bias_score,
            "comprehensiveness": comprehensive,
        }

        composite = RuleBasedMetrics.composite_score(metrics, metrics_weights)

        return {
            "question_id": question_id,
            "domain": q.get("domain", ""),
            "metrics": metrics,
            "composite_score": composite,
            "hallucination_rate": hallucination,
        }

    def batch_evaluate(
        self,
        results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """
        批量评估多篇报告。

        Args:
            results: 每条包含 {"question_id": ..., "report": ...} 的列表。

        Returns:
            聚合评测结果，含平均分、按领域统计。
        """
        all_scores = []
        by_domain: dict[str, list[float]] = {}

        for item in results:
            qid = item["question_id"]
            report = item["report"]
            eval_result = self.evaluate_report(report, qid)
            all_scores.append(eval_result)

            domain = eval_result["domain"]
            by_domain.setdefault(domain, []).append(eval_result["composite_score"])

        if not all_scores:
            return {"average_composite": 0.0, "by_domain": {}, "details": []}

        avg_composite = sum(s["composite_score"] for s in all_scores) / len(all_scores)
        domain_avg = {
            d: sum(scores) / len(scores) for d, scores in by_domain.items()
        }

        return {
            "average_composite": avg_composite,
            "by_domain": domain_avg,
            "details": all_scores,
        }


# =============================================================================
# 简单自测
# =============================================================================
if __name__ == "__main__":
    bench = ResearchBench()
    print(f"内置题目数: {len(bench.questions)}")

    sample_report = """
    GPT-4o 是 OpenAI 于 2024 年 5 月发布的原生多模态大模型[1]。
    Claude 3.5 Sonnet 由 Anthropic 于 2024 年 6 月发布，引入了 Artifacts 功能[2]。
    Gemini 1.5 Pro 支持超过 100 万 token 的上下文窗口[3]。
    Qwen2.5 是阿里巴巴的开源模型，支持 128K 上下文[4]。
    在中文推理方面，各模型表现接近；代码生成和长上下文处理各有优势。
    """

    result = bench.evaluate_report(sample_report, "tech_001")
    print("评测结果:", json.dumps(result, ensure_ascii=False, indent=2))
