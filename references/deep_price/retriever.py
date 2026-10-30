import csv
import json
from typing import List, Dict, Any


class KnowledgeRetriever:
    """
    简单知识检索器：
    - 读取长因果链CSV
    - 根据输入特征关键词匹配相关链条
    """

    def __init__(self, chain_csv_path: str):
        self.chain_csv_path = chain_csv_path
        self.chains = self.load_chains()

    def load_chains(self) -> List[Dict[str, Any]]:
        chains = []
        with open(self.chain_csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # 解析 JSON 字段
                try:
                    row["nodes"] = json.loads(row["nodes"])
                except Exception:
                    pass

                try:
                    row["triples"] = json.loads(row["triples"])
                except Exception:
                    pass

                chains.append(row)

        print(f"✅ 已加载知识链 {len(chains)} 条")
        return chains

    def retrieve(self, features: Dict[str, Any], top_k: int = 5) -> List[Dict[str, Any]]:
        """
        简单关键词检索：
        - 特征名
        - 特征值
        - chain_structure / narrative 中的关键词
        """
        scored = []

        feature_keys = [str(k).lower() for k in features.keys()]
        feature_values = [str(v).lower() for v in features.values()]

        for chain in self.chains:
            text = f"{chain.get('chain_structure', '')} {chain.get('narrative', '')}".lower()

            score = 0

            # 特征名匹配
            for kw in feature_keys:
                if kw in text:
                    score += 3

            # 特征值匹配
            for val in feature_values:
                if val in text:
                    score += 1

            # 一些常见因果关键词加权
            for kw in ["冗余", "可靠性", "温度", "泄漏", "污染", "压力", "节流", "黏度", "效率", "成本", "价格"]:
                if kw in text:
                    score += 0.5

            if score > 0:
                scored.append((score, chain))

        scored.sort(key=lambda x: x[0], reverse=True)
        result = [c for _, c in scored[:top_k]]

        print(f"✅ 检索到相关知识链 {len(result)} 条")
        return result
