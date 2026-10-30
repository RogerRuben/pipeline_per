from typing import Dict, Any
import json
import pandas as pd
from predictor0730 import EnsemblePricePredictor
from retriever import KnowledgeRetriever
from explain_engine0821_2 import ExplainEngine


class PriceExplainPipeline:
    """
    价格预测 + 大模型机理解释

    工作流：

    predict_price()
        |
        |-- pkl模型预测
        |-- 规则匹配
        |
        ↓
        立即返回价格

    explain()
        |
        |-- 读取feature_importance.csv
        |-- 固定选择重要性Top5
        |-- 知识链检索
        |-- 结合规则树
        |-- LLM机理解释
        |
        ↓
        返回关键参数机理分析
    """

    def __init__(
        self,
        model_paths: dict,
        weights_path: str,
        feature_order: list,
        scaler,
        chain_csv_path: str,
        api_key: str,
        base_url: str,
        model_name: str,
        feature_importance_path: str,
        use_outlier_filter=False,
        rule_path=None
    ):

        # =====================================================
        # 1. 价格预测模型
        # =====================================================
        self.predictor = EnsemblePricePredictor(
            model_paths=model_paths,
            weights_path=weights_path,
            feature_order=feature_order,
            scaler=scaler,
            use_outlier_filter=use_outlier_filter
        )

        # =====================================================
        # 2. 知识链检索
        # =====================================================
        self.retriever = KnowledgeRetriever(
            chain_csv_path
        )

        # =====================================================
        # 3. 特征重要性文件
        # =====================================================
        self.feature_importance_path = (
            feature_importance_path
        )

        # =====================================================
        # 4. 大模型解释器
        # =====================================================
        self.explainer = ExplainEngine(
            api_key=api_key,
            base_url=base_url,
            model_name=model_name,
            feature_importance_file=feature_importance_path,
            sample_data_file="样本1.csv"
        )

        # =====================================================
        # 5. 规则树
        # =====================================================
        self.rules = []

        if rule_path:

            with open(
                rule_path,
                "r",
                encoding="utf-8"
            ) as f:

                self.rules = json.load(f)


    # =====================================================
    # 读取Top5特征重要性
    # =====================================================
    def load_top_features(
        self,
        top_n=5
    ):

        df = pd.read_excel(
            self.feature_importance_path
        )

        # -------------------------
        # 自动识别特征列
        # -------------------------
        feature_candidates = [
            "feature"
        ]

        importance_candidates = [
            "importance"
        ]

        feature_col = None
        importance_col = None

        for col in feature_candidates:

            if col in df.columns:
                feature_col = col
                break

        for col in importance_candidates:

            if col in df.columns:
                importance_col = col
                break


        if feature_col is None:

            raise ValueError(
                "feature_importance.csv中"
                "没有找到特征名称列。\n"
                f"当前列名：{list(df.columns)}"
            )


        if importance_col is None:

            raise ValueError(
                "feature_importance.csv中"
                "没有找到重要性列。\n"
                f"当前列名：{list(df.columns)}"
            )


        # -------------------------
        # 清洗
        # -------------------------
        df = df.dropna(
            subset=[
                feature_col,
                importance_col
            ]
        )

        df[importance_col] = pd.to_numeric(
            df[importance_col],
            errors="coerce"
        )

        df = df.dropna(
            subset=[importance_col]
        )


        # -------------------------
        # importance降序
        # -------------------------
        df = df.sort_values(
            by=importance_col,
            ascending=False
        )


        # -------------------------
        # 固定Top5
        # -------------------------
        top_df = df.head(top_n)


        result = []

        for rank, (_, row) in enumerate(
            top_df.iterrows(),
            start=1
        ):

            result.append({

                "rank": rank,

                "feature":
                    str(
                        row[feature_col]
                    ).strip(),

                "importance":
                    float(
                        row[importance_col]
                    )
            })


        return result


    # =====================================================
    # 规则解析
    # =====================================================
    def parse_condition(
        self,
        text
    ):

        text = text.replace(
            " ",
            ""
        )

        operators = [
            "<=",
            ">=",
            "≤",
            "≥",
            "==",
            "=",
            "<",
            ">"
        ]

        for op in operators:

            if op in text:

                a, b = text.split(
                    op,
                    1
                )

                if op == "=":
                    op = "=="

                elif op == "≤":
                    op = "<="

                elif op == "≥":
                    op = ">="

                return a, op, b


        raise ValueError(
            "无法解析规则"
        )


    # =====================================================
    # 匹配当前样本对应规则
    # =====================================================
    def match_rule(
        self,
        features
    ):

        for rule in self.rules:

            flag = True

            for condition in rule.get(
                "conditions",
                []
            ):

                try:

                    feature, op, value = \
                        self.parse_condition(
                            condition
                        )

                    if feature not in features:

                        flag = False
                        break


                    x = float(
                        features[feature]
                    )

                    value = float(
                        value
                    )


                    if op == "<=":

                        flag &= (
                            x <= value
                        )

                    elif op == ">=":

                        flag &= (
                            x >= value
                        )

                    elif op == "<":

                        flag &= (
                            x < value
                        )

                    elif op == ">":

                        flag &= (
                            x > value
                        )

                    elif op == "==":

                        flag &= (
                            x == value
                        )


                    if not flag:
                        break


                except Exception:

                    flag = False
                    break


            if flag:
                return rule


        return None


    # =====================================================
    # 第一阶段：快速价格预测
    # =====================================================
    def predict_price(
        self,
        features: Dict[str, Any]
    ):

        if hasattr(
            self.predictor,
            "predict_with_details"
        ):

            pred_price, base_predictions = \
                self.predictor.predict_with_details(
                    features
                )

        else:

            pred_price = \
                self.predictor.predict(
                    features
                )

            base_predictions = {}


        pred_price = float(
            pred_price
        )


        # 匹配当前样本规则
        rule = self.match_rule(
            features
        )


        return {

            "features":
                features,

            "pred_price":
                pred_price,

            "base_predictions":
                base_predictions,

            "rule":
                rule
        }


    # =====================================================
    # 第二阶段：关键参数机理分析
    # =====================================================
    def explain(
        self,
        predict_result,
        top_k=5
    ):

        # =================================================
        # 模型预测使用特征
        # =================================================
        features = predict_result[
            "features"
        ]


        # =================================================
        # 前端真实输入参数
        # =================================================
        raw_features = \
            predict_result.get(
                "raw_features",
                features
            )


        pred_price = predict_result[
            "pred_price"
        ]


        rule = predict_result.get(
            "rule"
        )


        # =================================================
        # 1. 读取feature_importance Top5
        # =================================================
        important_features = \
            self.load_top_features(
                top_n=top_k
            )


        # 把当前实际值加入Top5
        for item in important_features:

            feature_name = item[
                "feature"
            ]

            item[
                "current_value"
            ] = raw_features.get(
                feature_name
            )


        # =================================================
        # 控制台输出Top5
        # =================================================
        print(
            "\n"
            + "=" * 60
        )

        print(
            "当前机理分析使用的Top5关键参数"
        )

        print(
            "=" * 60
        )


        for item in important_features:

            print(
                f"Top{item['rank']} | "
                f"{item['feature']} | "
                f"importance="
                f"{item['importance']:.6f} | "
                f"当前值="
                f"{item['current_value']}"
            )


        print(
            "=" * 60
            + "\n"
        )


        # =================================================
        # 2. 得到Top5参数名称
        # =================================================
        top_feature_names = [

            item["feature"]

            for item
            in important_features

        ]


        # =================================================
        # 3. 知识链检索
        # =================================================
        #
        # 注意：
        # 当前retriever如果只能接收features，
        # 就构造一个只包含Top5的features，
        # 防止知识检索被其他低重要性参数干扰。
        # =================================================

        top_features_for_retrieval = {}

        for feature_name in top_feature_names:

            if feature_name in features:

                top_features_for_retrieval[
                    feature_name
                ] = features[
                    feature_name
                ]


        # 如果模型特征中没有，
        # 尝试从raw_features获取
        for feature_name in top_feature_names:

            if (
                feature_name
                not in top_features_for_retrieval
                and
                feature_name in raw_features
            ):

                top_features_for_retrieval[
                    feature_name
                ] = raw_features[
                    feature_name
                ]


        related_chains = \
            self.retriever.retrieve(
                top_features_for_retrieval,
                top_k=50
            )


        # =================================================
        # 4. 调用LLM进行机理分析
        # =================================================
        explanation = \
            self.explainer.generate(

                # 模型使用值
                features=features,

                # 前端真实值
                raw_features=raw_features,

                # 当前预测价格
                pred_price=pred_price,

                # 知识链
                related_chains=
                    related_chains,

                # 当前规则
                rule=rule,

                # Top5重要参数
                important_features=
                    important_features
            )


        # =================================================
        # 返回
        # =================================================
        return {

            "explanation":
                explanation,

            "important_features":
                important_features,

            "related_chains":
                related_chains
        }


    # =====================================================
    # 兼容旧版本调用
    # =====================================================
    def run(
        self,
        features,
        top_k=20,
        raw_features=None
    ):

        # =================================================
        # 价格预测
        # =================================================
        result = self.predict_price(
            features
        )


        # =================================================
        # 保存原始输入
        # =================================================
        if raw_features is not None:

            result[
                "raw_features"
            ] = raw_features

        else:

            result[
                "raw_features"
            ] = features.copy()


        # =================================================
        # 机理解释
        # =================================================
        explain_result = \
            self.explain(
                result,
                top_k
            )


        result.update(
            explain_result
        )


        return result
