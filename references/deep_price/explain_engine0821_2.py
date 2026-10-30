import json
import os
import re
import pandas as pd
from typing import Dict, Any, List
from openai import OpenAI


class ExplainEngine:
    """
    Top5关键属性价格形成机理分析

    依据：
    1. feature_importance.csv中重要性最高的前5个属性
    2. 当前样本实际属性值
    3. 原始样本数据范围
    4. 当前样本局部规则树
    5. 知识图谱因果链
    6. 保守工程常识
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model_name: str = "qwen3.7",
        feature_importance_file: str = "feature_importance.csv",
        sample_data_file: str = None
    ):
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model_name = model_name
        self.feature_importance_file = feature_importance_file
        self.sample_data_file = sample_data_file

    # =====================================================
    # 读取重要性Top5
    # =====================================================
    def load_top_features(self, top_n=5):
        if not os.path.exists(self.feature_importance_file):
            raise FileNotFoundError(
                f"找不到特征重要性文件：{self.feature_importance_file}"
            )

        df = pd.read_csv(self.feature_importance_file, encoding="utf-8-sig")

        if "feature" not in df.columns:
            raise ValueError(
                "feature_importance.csv中没有找到feature列。\n"
                f"当前列名：{list(df.columns)}"
            )

        if "importance" not in df.columns:
            raise ValueError(
                "feature_importance.csv中没有找到importance列。\n"
                f"当前列名：{list(df.columns)}"
            )

        df = df.dropna(subset=["feature", "importance"])
        df["importance"] = pd.to_numeric(df["importance"], errors="coerce")
        df = df.dropna(subset=["importance"])
        df = df.sort_values(by="importance", ascending=False).head(top_n)

        return [
            {
                "rank": rank,
                "feature": str(row["feature"]).strip(),
                "importance": float(row["importance"])
            }
            for rank, (_, row) in enumerate(df.iterrows(), start=1)
        ]

    # =====================================================
    # 根据样本范围判断当前属性水平
    # =====================================================
    def get_sample_levels(self, raw_features, important_features):
        """
        数值型属性：
        以样本min~max范围划分为三个等宽区间：
        前1/3 = 较低水平
        中1/3 = 中等水平
        后1/3 = 较高水平

        超出样本范围时：
        小于min = 低于样本范围
        大于max = 高于样本范围

        类别型/无法可靠转为连续数值的属性不强行判断高低。
        """
        result = {}

        if not self.sample_data_file:
            return result

        if not os.path.exists(self.sample_data_file):
            print(f"警告：找不到样本数据文件：{self.sample_data_file}")
            return result

        sample_df = pd.read_csv(self.sample_data_file, encoding="utf-8-sig")

        for item in important_features:
            feature = item.get("feature")

            if feature not in sample_df.columns or feature not in raw_features:
                continue

            series = pd.to_numeric(sample_df[feature], errors="coerce").dropna()

            # 唯一值过少时，很可能是分类/离散编码，不强行解释成高低水平
            if len(series) == 0 or series.nunique() < 5:
                result[feature] = {
                    "current_value": raw_features.get(feature),
                    "level": "不评价高低水平",
                    "reason": "该属性在样本中不适合按连续数值范围判断高低"
                }
                continue

            try:
                current_value = float(raw_features[feature])
            except Exception:
                result[feature] = {
                    "current_value": raw_features.get(feature),
                    "level": "不评价高低水平",
                    "reason": "当前属性不是可连续比较的数值"
                }
                continue

            min_value = float(series.min())
            max_value = float(series.max())
            mean_value = float(series.mean())

            if max_value == min_value:
                level = "样本中基本无变化"
                position = None
            else:
                position = (current_value - min_value) / (max_value - min_value)

                if current_value < min_value:
                    level = "低于样本范围"
                elif current_value > max_value:
                    level = "高于样本范围"
                elif position <= 1 / 3:
                    level = "较低水平"
                elif position < 2 / 3:
                    level = "中等水平"
                else:
                    level = "较高水平"

            result[feature] = {
                "current_value": raw_features.get(feature),
                "sample_min": min_value,
                "sample_max": max_value,
                "sample_mean": mean_value,
                "relative_position": (
                    round(position, 4)
                    if position is not None
                    else None
                ),
                "level": level,
                "level_basis":
                    "依据当前数据集中该属性的样本最小值和最大值进行相对水平判断"
            }

        return result

    # =====================================================
    # 构造Prompt
    # =====================================================
    def build_prompt(
        self,
        features,
        raw_features,
        pred_price,
        chains,
        rule=None,
        important_features=None,
        sample_levels=None
    ):
        chains_text = ""
        for c in chains:
            chains_text += (
                f"chain_id: {c.get('chain_id', 'unknown')}\n"
                f"因果链: {c.get('chain_structure', '')}\n"
                f"说明: {c.get('narrative', '')}\n"
                f"三元组: {c.get('triples', '')}\n\n"
            )
        if not chains_text:
            chains_text = "未检索到相关知识链。"

        rule_text = (
            json.dumps(rule, ensure_ascii=False, indent=2)
            if rule
            else "{}"
        )

        important_text = (
            json.dumps(important_features, ensure_ascii=False, indent=2)
            if important_features
            else "未提供关键属性。"
        )

        sample_level_text = (
            json.dumps(sample_levels, ensure_ascii=False, indent=2)
            if sample_levels
            else "{}"
        )

        raw_features_text = json.dumps(
            raw_features, ensure_ascii=False, indent=2
        )
        features_text = json.dumps(
            features, ensure_ascii=False, indent=2
        )

        prompt = f"""
你是一名工程产品结构、材料、制造工艺和成本形成机理分析专家。

任务：
针对价格预测模型中重要性最高的5个属性，结合当前实际属性值、样本数据范围、
局部规则树和知识图谱因果链，解释这些属性如何通过结构、材料、设计、制造或其他
有可靠依据的中间环节影响成本，最终影响价格。

本任务可能会更换不同产品、不同部件和不同属性数据集。
因此禁止预设当前产品一定具有某种具体结构、材料、部件或工艺。
只能根据本次输入的属性、规则树和知识链进行分析。

每个属性控制在2到3句话左右，不要太长。

==================================================
【当前预测价格】
==================================================
{pred_price:.4f} 万元

==================================================
【当前样本真实属性】
==================================================
{raw_features_text}

涉及当前数值时必须使用这里的真实值。
禁止把归一化值、编码值当成真实属性值。

==================================================
【模型内部使用属性】
==================================================
{features_text}

该部分只用于辅助理解模型输入。
实际属性值始终以【当前样本真实属性】为准。

==================================================
【重要性最高的前5个属性】
==================================================
{important_text}

这里已经按照feature_importance.csv中的importance从高到低排序。
必须严格按照rank=1到rank=5依次分析这5个属性。
不得删除、替换、重新排序或增加新的核心属性。

==================================================
【当前属性在样本数据中的相对水平】
==================================================
{sample_level_text}

对于数值型属性，当前水平必须优先使用这里给出的level。

水平判断含义：
1. 较低水平：当前值位于样本最小值到最大值范围的前1/3；
2. 中等水平：当前值位于样本范围的中间1/3；
3. 较高水平：当前值位于样本范围的后1/3；
4. 低于样本范围：当前值小于该属性的样本最小值；
5. 高于样本范围：当前值大于该属性的样本最大值。

这里的“较高、较低、中等”仅表示：
“相对于当前样本数据范围所处的位置”。

禁止把它写成：
行业较高水平；
行业较低水平；
设计标准；
正常范围；
允许范围；
先进水平。

推荐表达：
“当前该属性为XX，处于较高水平。”

或者：
“当前该属性为XX，处于中等水平。”

如果level为“不评价高低水平”或没有level信息，直接省略水平描述，只写当前属性及其实际值；不要说明为什么省略。
类别属性应直接描述当前类别，不评价数值高低。

==================================================
【当前样本局部规则树】
==================================================
{rule_text}

规则树必须参与解释，但其作用与样本范围判断要区分：

样本数据范围：
用于判断当前属性相对于历史样本属于较低、中等还是较高水平。

规则树：
用于说明当前属性落在模型哪个局部条件区间，以及当前参数组合对应的价格分支。

规则树阈值不能称为行业标准或正常值。

如果Top5属性没有出现在规则树中，不要虚构规则阈值。
没有匹配规则或该属性未出现在规则树中时，直接省略规则内容；禁止在最终答案中说“未匹配到规则”“没有规则”或类似的话。

==================================================
【知识图谱因果知识】
==================================================
{chains_text}

知识图谱是机理解释的主要知识依据。

对于每个Top5属性，优先寻找：
当前属性状态
→ 结构/材料/设计/性能等中间变化
→ 制造难度、材料投入、装配工作量或其他成本因素
→ 成本变化
→ 价格变化

知识链中如果存在更具体、更可靠的传递路径，必须优先采用知识链。

==================================================
【通用工程机理原则】
==================================================
本任务不能预设具体产品结构。

允许使用非常保守、跨工程产品通常成立的常识补充一层逻辑，例如：

结构规模增加 → 通常可能增加材料投入或制造工作量；
结构复杂程度提高 → 通常可能增加加工、装配或检测工作量；
材料要求提高 → 通常可能增加材料或制造成本；
可靠性要求提高 → 通常可能增加设计、制造、检验或验证要求；
精度要求提高 → 通常可能提高加工、装配或质量控制要求；
零部件或功能配置增加 → 通常可能增加材料和装配成本。

但是这些只能作为逻辑连接，不能创造当前产品不存在的事实。

==================================================
【禁止产品特定臆测】
==================================================
如果输入没有明确证据，禁止自行说出：

某个具体部件一定变大或变厚；
一定采用某一种具体材料；
一定增加某一种结构；
一定采用某一种加工方法；
一定增加某个具体零部件；
一定采用某种密封、涂层、热处理或连接形式。

尤其禁止根据属性名称联想到某个固定产品模板后再套用机理。

只有当前输入、规则树或知识链明确出现的具体结构和材料，
才可以在输出中具体提及。

==================================================
【每条分析的推荐结构】
==================================================
每条优先按照以下顺序：

第一句：
“当前XX属性为XXX，处于较高/中等/较低水平。”
只有明确提供level时才写“处于较高/中等/较低水平”；没有可靠level时只写“当前XX属性为XXX”。

第二句：
结合规则树和知识链说明该水平意味着什么结构、材料、设计或制造变化。不要出现相较于当前样本范围这些的话

第三句：
说明上述变化如何影响成本，并最终使价格上升、下降或形成相应价格压力。

如果知识链不足以支持完整三层关系，
不要为了凑句子编造中间环节。

==================================================
【价格方向判断】
==================================================
价格方向按以下优先级判断：

第一优先级：规则树中当前样本所在分支和价格叶节点；
第二优先级：知识链中明确存在的成本或价格方向；
第三优先级：证据充分且保守的工程成本传递逻辑。

如果无法可靠判断方向，不要使用“一定导致”。
可以使用：
“可能形成价格上行压力”
“可能降低相应成本压力”
“与较高价格水平相对应”
“与较低价格水平相对应”。

禁止因为预测价格高，就反过来证明结构复杂或材料更高级。

==================================================
【避免重复】
==================================================
5条分析必须针对各自属性建立不同的知识链或工程路径。

不能全部机械写成：
“属性较高→制造要求提高→价格上涨”。

每条应优先使用该属性自身对应的知识链和规则信息。

==================================================
【输出格式】
==================================================
只输出JSON，不要Markdown、代码块或额外说明。

严格输出：
{{
    "mechanism_analysis": [
        "重要性第1名属性的机理分析",
        "重要性第2名属性的机理分析",
        "重要性第3名属性的机理分析",
        "重要性第4名属性的机理分析",
        "重要性第5名属性的机理分析"
    ]
}}

==================================================
【最终硬性要求】
==================================================
1. 必须严格输出5条，依次对应rank=1到rank=5。
2. 每条只能有一个核心属性。
3. 每条必须先写当前实际取值。
4. 对明确提供有效level的数值属性，紧接着说明其在当前样本范围中的水平；没有有效level时完全省略水平判断。
5. “较高/中等/较低”只能基于【当前属性在样本数据中的相对水平】判断。
6. 如果规则树包含该属性，应进一步使用规则树局部区间信息。
7. 如果知识链包含该属性，应优先使用知识链解释。
8. 重点从结构、材料、设计、制造和成本传递角度解释，但不能强行套用某种具体产品结构。
9. 不允许编造具体材料、结构、尺寸、工艺或定量成本比例。
10. 禁止输出“未匹配到规则”“没有匹配规则”“无法判断”“无法确定”“缺少依据”“信息不足”等过程性或否定性提示；遇到这些情况直接省略对应内容。
10. 不允许把样本内水平说成行业水平。
11. 不允许把相关关系夸大为确定因果。
12. 最终表达应说明：
    当前值是多少
    → 相对于当前样本属于什么水平
    → 为什么会影响结构/材料/设计/制造
    → 为什么进一步影响成本和价格。
"""
        return prompt

    # =====================================================
    # 调用大模型
    # =====================================================
    def generate(
        self,
        features: Dict[str, Any],
        raw_features: Dict[str, Any],
        pred_price: float,
        related_chains: List[Dict[str, Any]],
        rule=None,
        important_features=None
    ):
        if important_features is None:
            important_features = self.load_top_features(top_n=5)
        else:
            try:
                important_features = sorted(
                    important_features,
                    key=lambda x: float(x.get("importance", 0)),
                    reverse=True
                )[:5]
                for i, item in enumerate(important_features, start=1):
                    item["rank"] = i
            except Exception:
                important_features = self.load_top_features(top_n=5)

        valid_features = []
        for item in important_features:
            feature_name = item.get("feature", "")
            item_new = dict(item)
            item_new["current_value"] = raw_features.get(feature_name, None)
            valid_features.append(item_new)
        important_features = valid_features

        sample_levels = self.get_sample_levels(
            raw_features=raw_features,
            important_features=important_features
        )

        print("\n" + "=" * 60)
        print("价格预测模型重要性最高的前5个属性")
        print("=" * 60)
        for item in important_features:
            feature = item.get("feature")
            level = sample_levels.get(feature, {}).get("level", "无样本范围信息")
            print(
                f"Top{item.get('rank')} | "
                f"{feature} | "
                f"importance={item.get('importance'):.6f} | "
                f"当前值={item.get('current_value')} | "
                f"样本水平={level}"
            )
        print("=" * 60 + "\n")

        prompt = self.build_prompt(
            features=features,
            raw_features=raw_features,
            pred_price=pred_price,
            chains=related_chains,
            rule=rule,
            important_features=important_features,
            sample_levels=sample_levels
        )

        response = self.client.chat.completions.create(
            model=self.model_name,
            messages=[
                {
                    "role": "system",
                    "content":
                    "你是工程产品结构、材料、制造工艺和成本形成机理分析专家。"
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=0.1,
            max_tokens=8000
        )

        content = response.choices[0].message.content.strip()

        try:
            result = json.loads(content)
        except Exception:
            content = (
                content
                .replace("```json", "")
                .replace("```JSON", "")
                .replace("```", "")
                .strip()
            )
            try:
                result = json.loads(content)
            except Exception:
                result = {"mechanism_analysis": [content]}

        if not isinstance(result, dict):
            result = {"mechanism_analysis": [str(result)]}

        mechanism_analysis = result.get("mechanism_analysis", [])
        if isinstance(mechanism_analysis, str):
            mechanism_analysis = [mechanism_analysis]
        if not isinstance(mechanism_analysis, list):
            mechanism_analysis = [str(mechanism_analysis)]

        forbidden_status = re.compile(
            r"[^。！？；]*(?:未匹配到(?:局部决策)?规则|没有匹配规则|无法判断|无法确定|缺少依据|信息不足)[^。！？；]*[。！？；]?"
        )
        mechanism_analysis = [
            forbidden_status.sub("", str(x)).strip()
            for x in mechanism_analysis
            if forbidden_status.sub("", str(x)).strip()
        ][:5]

        return {
            "mechanism_analysis": mechanism_analysis
        }
