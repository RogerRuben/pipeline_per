import os
import json
import uuid
import time
import pickle
import threading
import shutil
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
from flask import Flask, request, jsonify, render_template, send_file, Response, stream_with_context, redirect
from sklearn.base import BaseEstimator, RegressorMixin

from pipeline0821 import PriceExplainPipeline


# ============================================================
# 1. 为旧pkl模型保留的自定义树类
#    如果你的pkl中保存了 ExpertWeightedTreeRegressor，不能删除
# ============================================================

@dataclass
class TreeNode:
    feature: str = None
    threshold: float = None
    left: object = None
    right: object = None
    value: float = None
    min_price: float = None
    max_price: float = None
    sample_num: int = 0
    is_leaf: bool = False


class ExpertWeightedTreeRegressor(BaseEstimator, RegressorMixin):
    def __init__(self, max_depth=10, min_samples_leaf=1, lambda_expert=0.5,
                 min_gain_split=0.0, expert_map=None):
        self.max_depth = max_depth
        self.min_samples_leaf = min_samples_leaf
        self.lambda_expert = lambda_expert
        self.min_gain_split = min_gain_split
        self.expert_map = expert_map or {}

    def fit(self, X, y):
        self.X_ = pd.DataFrame(X).copy()
        self.y_ = np.array(y).copy()
        self.columns_ = self.X_.columns.tolist()
        self.root_ = self._build_tree(self.X_, self.y_, 0, set())
        return self

    def _sse(self, y):
        return 0.0 if len(y) == 0 else np.sum((y - np.mean(y)) ** 2)

    def _expert_norm(self, feature):
        s = self.expert_map.get(feature, 1.0)
        if s is None or pd.isna(s) or not np.isfinite(s):
            s = 1.0
        s = max(1.0, min(5.0, float(s)))
        return (s - 1.0) / 4.0

    def _best_split(self, X, y, used_features):
        parent_sse = self._sse(y)
        best_score = -np.inf
        best_feature = best_threshold = best_left_idx = best_right_idx = None

        for feature in X.columns:
            if feature in used_features:
                continue
            values = np.unique(X[feature].values)
            if len(values) <= 1:
                continue
            thresholds = (values[:-1] + values[1:]) / 2.0

            for thr in thresholds:
                left_idx = X[feature].values <= thr
                right_idx = ~left_idx
                if left_idx.sum() < self.min_samples_leaf or right_idx.sum() < self.min_samples_leaf:
                    continue

                y_left, y_right = y[left_idx], y[right_idx]
                data_gain = parent_sse - (self._sse(y_left) + self._sse(y_right))
                if data_gain < self.min_gain_split:
                    continue

                score = data_gain * (1.0 + self.lambda_expert * self._expert_norm(feature))
                if score > best_score:
                    best_score = score
                    best_feature = feature
                    best_threshold = thr
                    best_left_idx = left_idx
                    best_right_idx = right_idx

        return best_feature, best_threshold, best_left_idx, best_right_idx

    def _build_tree(self, X, y, depth, used_features):
        price = np.exp(y)
        node = TreeNode(
            value=float(np.mean(y)),
            min_price=float(np.min(price)),
            max_price=float(np.max(price)),
            sample_num=len(price)
        )

        if depth >= self.max_depth or len(y) <= self.min_samples_leaf:
            node.is_leaf = True
            return node

        feature, threshold, left_idx, right_idx = self._best_split(X, y, used_features)
        if feature is None:
            node.is_leaf = True
            return node

        node.feature = feature
        node.threshold = threshold
        new_used = set(used_features)
        new_used.add(feature)
        node.left = self._build_tree(X[left_idx].reset_index(drop=True), y[left_idx], depth + 1, new_used)
        node.right = self._build_tree(X[right_idx].reset_index(drop=True), y[right_idx], depth + 1, new_used)
        return node

    def _predict_one(self, x, node):
        if node.is_leaf or node.feature is None:
            return node.value
        if x[node.feature] <= node.threshold:
            return self._predict_one(x, node.left)
        return self._predict_one(x, node.right)

    def predict(self, X):
        X = pd.DataFrame(X).copy()
        return np.array([self._predict_one(X.iloc[i], self.root_) for i in range(len(X))])


# ============================================================
# 2. Flask与路径配置
# ============================================================

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

RESULT_DIR = os.path.join(BASE_DIR, "results")
os.makedirs(RESULT_DIR, exist_ok=True)

FEATURE_IMPORTANCE_FILE = os.path.join(BASE_DIR, "feature_importance.xlsx")
HISTORY_FILE = os.path.join(BASE_DIR, "样本1.csv")
PRICE_COL = "价格_万元"

CHAIN_PATH = os.path.join(BASE_DIR, "causal_chains_long2.csv")
RULE_PATH = os.path.join(BASE_DIR, "rules.json")
KNOWLEDGE_TRIPLES_FILE = os.path.join(BASE_DIR, "knowledge_triples.csv")

# 自动识别为主；只有识别错误时才人工覆盖
FORCE_NUMERIC = []
FORCE_CATEGORICAL = []

# 数字型字段唯一值很少时优先视为分类变量，如0/1、1/2/3
MAX_CATEGORY_UNIQUE = 2
MAX_CATEGORY_UNIQUE_RATIO = 0.2

# 连续变量敏感性扰动范围
SENSITIVITY_RATES = [-20, -15, -10, -5, 0, 5, 10, 15, 20]

# 历史样本表中的模型字段顺序。样本1.csv 的中文表头存在编码损坏，
# 因此产品选择接口按稳定的列位置映射到页面字段。
PRODUCT_FEATURE_NAMES = [
    "压力_bar", "出力_kN", "行程_mm", "速度_mm_s",
    "温度_min_°C", "温度_max_°C", "MTBF_小时", "重量_kg",
    "功率_kW", "泄漏_mL_min", "响应时间_ms", "控制方式",
    "适用机型", "材料类型", "密封等级", "液压油类型",
    "流量_L_min", "固有频率_Hz", "阻尼系数", "冗余配置",
    "认证等级", "维护周期_小时", "故障率_10e6_FH",
    "安装方式", "接口类型"
]

# 敏感性分析下拉框的自定义优先顺序。
# 可直接调整下面属性的先后；未写入的新属性仍会自动排在最后。
SENSITIVITY_FEATURE_ORDER = [
    "压力_bar", "出力_kN", "行程_mm", "速度_mm_s",
    "温度_min_°C", "温度_max_°C", "MTBF_小时", "重量_kg",
    "功率_kW", "泄漏_mL_min", "响应时间_ms", "控制方式",
    "适用机型", "材料类型", "密封等级", "液压油类型",
    "流量_L_min", "固有频率_Hz", "阻尼系数", "冗余配置",
    "认证等级", "维护周期_小时", "故障率_10e6_FH",
    "安装方式", "接口类型"
]


# ============================================================
# 3. 时间与批量价格校正参数
# ============================================================

PPI_BASE = 100
LAMBDA_MEAN = -0.08
STANDARD_QTY = 100

PPI_DICT = {
    2010: 377.5,
    2011: 400.2,
    2012: 393.4,
    2013: 385.9,
    2014: 378.6,
    2015: 358.9,
    2016: 353.9,
    2017: 376.2,
    2018: 389.4,
    2019: 388.2,
    2020: 381.2,
    2021: 412.1,
    2022: 429,
    2023: 416.1,
    2024: 406.9,
    2025: 396.3
}


# ============================================================
# 4. 当前数据集分类映射
#    后面更换数据集时，如新模型仍需“文字类别→数字编码”，只改这里即可。
#    若新数据集本身已经使用数字编码，可不增加映射。
# ============================================================

CATEGORY_MAP = {
    "控制方式": {
        "手动": 1, "电控": 2, "3": 3, "6": 6,
        "9": 9, "10": 10, "液控": 12
    },
    "适用机型": {
        "通用型": 1, "重载型": 2, "3": 3,
        "6": 6, "8": 8, "精密型": 9
    },
    "材料类型": {
        "钢": 1, "铝": 2, "铸铁": 3, "a": 4, "5": 5
    },
    "密封等级": {
        "普通": 1, "增强": 2, "高密封": 3, "a": 4, "5": 5
    },
    "液压油类型": {
        "0": 0, "矿物油": 1, "合成油": 2, "环保油": 3
    },
    "安装方式": {
        "法兰安装": 1, "螺纹安装": 2, "底座安装": 3, "4": 4, "5": 5
    },
    "接口类型": {
        "标准接口": 1, "快速接口": 2, "定制接口": 3
    },
    "认证等级": {
        "无认证": 1, "一般认证": 2, "高等级认证": 3
    }
}


# 不在代码中写真实密钥。
# Windows命令行可先执行：
# set DASHSCOPE_API_KEY=你的API_KEY
API_KEY = os.getenv("DASHSCOPE_API_KEY", "")  # credential removed for reference distribution
BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
MODEL_NAME = "qwen3.8-27b"


# ============================================================
# 5. 模型目录发现、加载与切换
# ============================================================

PIPELINE_LOCK = threading.RLock()
ACTIVE_MODEL_SET = None
ACTIVE_MODEL_INFO = None

# ensemble_weights.npy 按训练阶段的模型顺序保存。当前项目沿用这一顺序，
# 即使某个模型目录只包含其中一部分模型，也不能按文件名字母顺序重排。
MODEL_LOAD_ORDER = ["lasso", "ridge", "rf", "et", "svr", "gbdt"]


def safe_model_dir_name(raw_name):
    """把用户输入的模型库名称整理成安全的 model* 目录名。"""
    name = str(raw_name or "").strip()
    keep = []
    for char in name:
        if char.isalnum() or char in ("_", "-"):
            keep.append(char)
    name = "".join(keep).strip("_-")
    if not name:
        name = "upload"
    if not name.lower().startswith("model"):
        name = f"model_{name}"
    return name


def next_available_model_dir(base_name):
    candidate = os.path.join(BASE_DIR, base_name)
    if not os.path.exists(candidate):
        return candidate, base_name

    index = 1
    while True:
        name = f"{base_name}_{index}"
        candidate = os.path.join(BASE_DIR, name)
        if not os.path.exists(candidate):
            return candidate, name
        index += 1


def inspect_model_directory(model_name, model_path):
    discovered_paths = {}
    for filename in sorted(os.listdir(model_path)):
        suffix = "_complete.pkl"
        if filename.lower().endswith(suffix):
            current_model_name = filename[:-len(suffix)]
            discovered_paths[current_model_name] = os.path.join(model_path, filename)

    ordered_names = sorted(
        discovered_paths,
        key=lambda name: (
            MODEL_LOAD_ORDER.index(name)
            if name in MODEL_LOAD_ORDER
            else len(MODEL_LOAD_ORDER),
            name.lower()
        )
    )
    model_paths = {name: discovered_paths[name] for name in ordered_names}

    weights_path = os.path.join(model_path, "ensemble_weights.npy")
    feature_names_path = os.path.join(model_path, "feature_names.pkl")
    scaler_candidates = [
        os.path.join(model_path, "minmax.pkl"),
        os.path.join(model_path, "scaler.pkl")
    ]
    scaler_path = next((path for path in scaler_candidates if os.path.isfile(path)), None)

    missing = []
    if not model_paths:
        missing.append("*_complete.pkl")
    if not os.path.isfile(weights_path):
        missing.append("ensemble_weights.npy")
    if not os.path.isfile(feature_names_path):
        missing.append("feature_names.pkl")
    if scaler_path is None:
        missing.append("minmax.pkl/scaler.pkl")

    metrics = {}
    for metrics_name in ("metrics.json", "evaluation_metrics.json"):
        metrics_path = os.path.join(model_path, metrics_name)
        if os.path.isfile(metrics_path):
            try:
                with open(metrics_path, "r", encoding="utf-8") as file:
                    metrics = json.load(file)
            except Exception as exc:
                metrics = {"error": str(exc)}
            break

    return {
        "name": model_name,
        "path": model_path,
        "model_paths": model_paths,
        "model_names": list(model_paths.keys()),
        "model_count": len(model_paths),
        "weights_path": weights_path,
        "feature_names_path": feature_names_path,
        "scaler_path": scaler_path,
        "missing": missing,
        "valid": not missing,
        "metrics": metrics
    }


def discover_model_sets():
    """发现项目根目录下名称以 model 开头的模型文件夹。"""
    result = {}
    for entry in sorted(os.scandir(BASE_DIR), key=lambda item: item.name.lower()):
        if not entry.is_dir() or not entry.name.lower().startswith("model"):
            continue

        result[entry.name] = inspect_model_directory(entry.name, entry.path)
    return result


def build_pipeline(model_set_name):
    model_sets = discover_model_sets()
    if model_set_name not in model_sets:
        raise ValueError(f"模型目录不存在：{model_set_name}")

    info = model_sets[model_set_name]
    if not info["valid"]:
        raise ValueError(
            f"模型目录 {model_set_name} 缺少：{', '.join(info['missing'])}"
        )

    weights = np.load(info["weights_path"]).reshape(-1)
    if len(weights) != info["model_count"]:
        raise ValueError(
            f"{model_set_name} 中模型数量为 {info['model_count']}，"
            f"但融合权重数量为 {len(weights)}"
        )
    if not np.all(np.isfinite(weights)) or float(np.sum(weights)) == 0:
        raise ValueError(f"{model_set_name} 的融合权重无效")

    with open(info["feature_names_path"], "rb") as file:
        feature_order = pickle.load(file)
    with open(info["scaler_path"], "rb") as file:
        scaler = pickle.load(file)

    local_feature_importance = next((
        os.path.join(info["path"], filename)
        for filename in ("feature_importance.xlsx", "feature_importance.csv")
        if os.path.isfile(os.path.join(info["path"], filename))
    ), FEATURE_IMPORTANCE_FILE)
    local_rule_path = os.path.join(info["path"], "rules.json")
    if not os.path.isfile(local_rule_path):
        local_rule_path = RULE_PATH
    local_chain_path = os.path.join(info["path"], "causal_chains_long2.csv")
    if not os.path.isfile(local_chain_path):
        local_chain_path = CHAIN_PATH

    new_pipeline = PriceExplainPipeline(
        model_paths=info["model_paths"],
        weights_path=info["weights_path"],
        feature_order=feature_order,
        scaler=scaler,
        chain_csv_path=local_chain_path,
        api_key=API_KEY,
        base_url=BASE_URL,
        model_name=MODEL_NAME,
        rule_path=local_rule_path,
        feature_importance_path=local_feature_importance
    )
    return new_pipeline, info


def activate_model_set(model_set_name):
    global pipeline, ACTIVE_MODEL_SET, ACTIVE_MODEL_INFO
    new_pipeline, info = build_pipeline(model_set_name)
    with PIPELINE_LOCK:
        pipeline = new_pipeline
        ACTIVE_MODEL_SET = model_set_name
        ACTIVE_MODEL_INFO = info
    return info


def get_active_pipeline():
    with PIPELINE_LOCK:
        return pipeline


def save_uploaded_model_files(uploaded_files, model_name):
    if not uploaded_files:
        raise ValueError("请选择需要上传的模型文件")

    allowed_extensions = {".pkl", ".npy", ".json", ".xlsx", ".xls", ".csv"}
    temp_name = f".model_upload_{uuid.uuid4().hex}"
    temp_dir = os.path.join(BASE_DIR, temp_name)
    os.makedirs(temp_dir, exist_ok=False)

    try:
        saved_files = []
        for uploaded_file in uploaded_files:
            original_name = os.path.basename(uploaded_file.filename or "")
            original_name = original_name.replace("\\", "")
            original_name = original_name.replace("/", "")
            if not original_name:
                continue

            extension = os.path.splitext(original_name)[1].lower()
            if extension not in allowed_extensions:
                raise ValueError(f"不支持的文件类型：{original_name}")

            target_path = os.path.join(temp_dir, original_name)
            if os.path.exists(target_path):
                raise ValueError(f"存在重复文件名：{original_name}")
            uploaded_file.save(target_path)
            saved_files.append(original_name)

        if not saved_files:
            raise ValueError("没有成功读取到上传文件")

        safe_name = safe_model_dir_name(model_name or f"model_upload_{uuid.uuid4().hex[:8]}")
        info = inspect_model_directory(safe_name, temp_dir)
        if not info["valid"]:
            raise ValueError(f"模型文件不完整，缺少：{', '.join(info['missing'])}")

        weights = np.load(info["weights_path"]).reshape(-1)
        if len(weights) != info["model_count"]:
            raise ValueError(
                f"模型数量为 {info['model_count']}，但融合权重数量为 {len(weights)}"
            )
        if not np.all(np.isfinite(weights)) or float(np.sum(weights)) == 0:
            raise ValueError("融合权重无效")

        target_dir, final_name = next_available_model_dir(safe_name)
        shutil.move(temp_dir, target_dir)
        final_info = inspect_model_directory(final_name, target_dir)
        return final_info, saved_files
    except Exception:
        if os.path.isdir(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise


available_model_sets = discover_model_sets()
initial_model_set = "model" if available_model_sets.get("model", {}).get("valid") else next(
    (name for name, info in available_model_sets.items() if info["valid"]),
    None
)
if initial_model_set is None:
    raise RuntimeError("未找到包含完整运行文件的 model* 模型目录")
activate_model_set(initial_model_set)

executor = ThreadPoolExecutor(max_workers=4)
EXPLAIN_TASKS = {}

RESULT_CACHE = {}


def sse_event(event, data):
    """生成标准 SSE 消息。"""
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


# ============================================================
# 6. 通用工具
# ============================================================

def encode_feature(name, value):
    if name not in CATEGORY_MAP:
        return value

    mapping = CATEGORY_MAP[name]
    key = str(value).strip()

    # 已经是前端文字类别
    if key in mapping:
        return mapping[key]

    # 已经是数字编码时允许直接通过
    try:
        number = float(value)
        return int(number) if number.is_integer() else number
    except Exception:
        raise ValueError(f"{name} 的类别值“{value}”不在 CATEGORY_MAP 中")


def convert_input(data):
    """网页原始输入 -> 模型输入。时间批量校正字段不进入模型。"""
    exclude = {
        "price", "quantity", "year",
        "adjust_price", "adjust_quantity", "adjust_year"
    }

    features = {}

    for k, v in data.items():
        if k in exclude:
            continue

        if k in CATEGORY_MAP:
            features[k] = encode_feature(k, v)
        else:
            try:
                features[k] = float(v)
            except Exception:
                # 新数据集若存在新的文字分类列，需要在 CATEGORY_MAP 中增加映射。
                raise ValueError(
                    f"特征“{k}”的值“{v}”无法转换为数字。"
                    f"如果它是分类变量，请在 CATEGORY_MAP 中增加该字段的编码映射。"
                )

    return features


def predict_one_price(raw_features):
    """页面2/3统一复用页面1完全相同的输入转换与预测流程。"""
    model_features = convert_input(raw_features)
    result = get_active_pipeline().predict_price(model_features)

    if isinstance(result, dict):
        for key in ["pred_price", "price", "predicted_price", "ensemble_price"]:
            if key in result:
                return float(result[key])

    return float(result)


def load_history_data():
    if HISTORY_FILE.lower().endswith(".csv"):
        try:
            return pd.read_csv(HISTORY_FILE, encoding="utf-8-sig")
        except UnicodeDecodeError:
            return pd.read_csv(HISTORY_FILE)

    return pd.read_excel(HISTORY_FILE)


def load_feature_importance():
    if FEATURE_IMPORTANCE_FILE.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(FEATURE_IMPORTANCE_FILE)
    else:
        df = pd.read_csv(FEATURE_IMPORTANCE_FILE, encoding="utf-8-sig")

    if len(df.columns) < 2:
        raise ValueError("feature_importance 文件至少需要“特征名称”和“重要度”两列")

    feature_col = None
    importance_col = None

    for col in df.columns:
        lower = str(col).lower()
        if feature_col is None and (
            "feature" in lower or "name" in lower or "特征" in str(col)
        ):
            feature_col = col
        if importance_col is None and (
            "importance" in lower or "score" in lower or "重要" in str(col)
        ):
            importance_col = col

    if feature_col is None:
        feature_col = df.columns[0]
    if importance_col is None:
        importance_col = df.columns[1]

    result = df[[feature_col, importance_col]].copy()
    result.columns = ["feature", "importance"]
    result["feature"] = result["feature"].astype(str).str.strip()
    result["importance"] = pd.to_numeric(result["importance"], errors="coerce")
    result = result.dropna(subset=["importance"])
    result = result.sort_values("importance", ascending=False).reset_index(drop=True)

    total = result["importance"].sum()
    if total == 0:
        total = 1.0

    result["ratio"] = result["importance"] / total * 100
    result["rank"] = np.arange(1, len(result) + 1)
    return result


def detect_feature_types(df, feature_names=None):
    """自动识别连续数值与分类变量，FORCE_* 的人工设置优先。"""
    if feature_names is None:
        feature_names = [c for c in df.columns if c != PRICE_COL]

    numeric_features = []
    categorical_features = []

    for col in feature_names:
        if col not in df.columns:
            continue

        if col in FORCE_NUMERIC:
            numeric_features.append(col)
            continue

        if col in FORCE_CATEGORICAL:
            categorical_features.append(col)
            continue

        s = df[col].dropna()
        if len(s) == 0:
            categorical_features.append(col)
            continue

        numeric_s = pd.to_numeric(s, errors="coerce")
        numeric_ratio = numeric_s.notna().mean()

        if numeric_ratio < 0.95:
            categorical_features.append(col)
            continue

        values = numeric_s.dropna()
        n_unique = values.nunique()
        unique_ratio = n_unique / max(len(values), 1)

        if (
            n_unique <= MAX_CATEGORY_UNIQUE
            and unique_ratio <= MAX_CATEGORY_UNIQUE_RATIO
        ):
            categorical_features.append(col)
        else:
            numeric_features.append(col)

    numeric_features = list(dict.fromkeys(numeric_features))
    categorical_features = [
        x for x in dict.fromkeys(categorical_features)
        if x not in numeric_features
    ]
    return numeric_features, categorical_features


def get_feature_types():
    history = load_history_data()
    importance_df = load_feature_importance()
    feature_names = [
        str(f) for f in importance_df["feature"]
        if str(f) in history.columns
    ]
    return detect_feature_types(history, feature_names)


def get_sensitivity_features():
    importance_df = load_feature_importance()
    history = load_history_data()
    numeric_features, categorical_features = get_feature_types()
    result = []

    for feature in importance_df["feature"]:
        feature = str(feature)
        if feature not in history.columns:
            continue

        if feature in numeric_features:
            feature_type = "numeric"
        elif feature in categorical_features:
            feature_type = "categorical"
        else:
            continue

        result.append({"feature": feature, "type": feature_type})

    order_map = {
        feature: index
        for index, feature in enumerate(SENSITIVITY_FEATURE_ORDER)
    }
    result.sort(
        key=lambda item: order_map.get(item["feature"], len(order_map))
    )

    return result


def filter_analyzable_sensitivity_features(current_features, candidates):
    """快速排除缺值、类型无效或没有可比较类别的字段。"""
    if not current_features:
        return candidates

    history = load_history_data()
    analyzable = []

    for item in candidates:
        feature = item["feature"]
        if feature not in current_features:
            continue

        if item["type"] == "numeric":
            try:
                base_value = float(current_features[feature])
                if np.isfinite(base_value):
                    analyzable.append(item)
            except Exception:
                continue
            continue

        # 分类字段至少需要两个有效类别，否则无法形成对比图。
        if feature not in history.columns:
            continue
        values = (
            history[feature].dropna().astype(str).str.strip().unique().tolist()
        )
        valid_values = []
        for value in values:
            try:
                restored = restore_categorical_value(feature, value, history)
                encode_feature(feature, restored)
                valid_values.append(restored)
            except Exception:
                continue
        if len({str(value) for value in valid_values}) >= 2:
            analyzable.append(item)

    return analyzable


def get_top10_numeric_features():
    importance_df = load_feature_importance()
    numeric_features, _ = get_feature_types()
    result = []

    for feature in importance_df["feature"]:
        feature = str(feature)
        if feature in numeric_features:
            result.append(feature)
        if len(result) >= 10:
            break

    return result


def restore_categorical_value(feature_name, value, history):
    """历史表中的数字型分类变量恢复成int/float，文字类别保持字符串。"""
    if feature_name not in history.columns:
        return value

    original_series = history[feature_name].dropna()

    if pd.api.types.is_numeric_dtype(original_series):
        try:
            num = float(value)
            return int(num) if num.is_integer() else num
        except Exception:
            return value

    return value


def display_product_value(feature_name, value):
    """把历史样本中的分类编码恢复成页面下拉框使用的文字。"""
    if feature_name not in CATEGORY_MAP:
        return value
    try:
        number = float(value)
        number = int(number) if number.is_integer() else number
    except Exception:
        number = value
    for label, code in CATEGORY_MAP[feature_name].items():
        if code == number:
            return label
    return str(value)


# ============================================================
# 7. 页面路由
# ============================================================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/factor_analysis")
def factor_analysis():
    return render_template("factor_analysis.html")


@app.route("/model_evaluation")
def model_evaluation():
    return render_template("model_evaluation.html")


@app.route("/api/model_sets", methods=["GET"])
def model_sets():
    discovered = discover_model_sets()
    items = []
    for name, info in discovered.items():
        items.append({
            "name": name,
            "active": name == ACTIVE_MODEL_SET,
            "valid": info["valid"],
            "missing": info["missing"],
            "model_names": info["model_names"],
            "model_count": info["model_count"],
            "metrics": info["metrics"]
        })
    return jsonify({"active_model_set": ACTIVE_MODEL_SET, "model_sets": items})


@app.route("/api/active_model", methods=["POST"])
def set_active_model():
    data = request.json or {}
    model_set_name = str(data.get("model_set", "")).strip()
    if not model_set_name:
        return jsonify({"error": "请选择模型目录"}), 400

    try:
        info = activate_model_set(model_set_name)
        return jsonify({
            "success": True,
            "active_model_set": ACTIVE_MODEL_SET,
            "model_names": info["model_names"],
            "model_count": info["model_count"]
        })
    except Exception as exc:
        return jsonify({"error": f"模型切换失败：{str(exc)}"}), 400


@app.route("/api/upload_model", methods=["POST"])
def upload_model():
    uploaded_files = request.files.getlist("files")
    model_name = request.form.get("model_name", "")

    try:
        info, saved_files = save_uploaded_model_files(uploaded_files, model_name)
        return jsonify({
            "success": True,
            "model_set": info["name"],
            "model_names": info["model_names"],
            "model_count": info["model_count"],
            "saved_file_count": len(saved_files)
        })
    except Exception as exc:
        return jsonify({"error": f"模型加入失败：{str(exc)}"}), 400


@app.route("/scenario_analysis")
def scenario_analysis():
    return redirect("/#sensitivity-analysis")


@app.route("/api/products", methods=["GET"])
def products():
    """返回可在第一页选择并自动回填的历史产品。"""
    try:
        history = load_history_data()
        required_columns = 1 + len(PRODUCT_FEATURE_NAMES) + 1
        if history.shape[1] < required_columns:
            raise ValueError("历史样本字段数量不足，无法映射产品属性")

        items = []
        for index, row in history.iterrows():
            product_type = row.iloc[0]
            features = {}
            for offset, feature_name in enumerate(PRODUCT_FEATURE_NAMES, start=1):
                value = row.iloc[offset]
                if pd.isna(value):
                    value = ""
                features[feature_name] = display_product_value(feature_name, value)

            price = pd.to_numeric(row.iloc[1 + len(PRODUCT_FEATURE_NAMES)], errors="coerce")
            if pd.isna(price):
                continue
            items.append({
                "id": int(index),
                "name": f"产品 {index + 1:03d}（类型 {product_type}）",
                "product_type": str(product_type),
                "price": round(float(price), 4),
                "features": features
            })
        return jsonify({"products": items})
    except Exception as e:
        return jsonify({"error": f"产品列表读取失败：{str(e)}"}), 500


# ============================================================
# 8. 页面1：时间批量价格校正
# ============================================================

@app.route("/price_adjust", methods=["POST"])
def price_adjust():
    data = request.json or {}

    try:
        price = float(data["price"])
        quantity = float(data["quantity"])
        year = int(data["year"])

        ppi = PPI_DICT.get(year, PPI_BASE)
        price_time_adj = price * PPI_BASE / ppi
        final_adjust_price = (
            price_time_adj
            * (quantity / STANDARD_QTY) ** LAMBDA_MEAN
        )

        return jsonify({
            "success": True,
            "original_price": round(price, 4),
            "time_adjust_price": round(price_time_adj, 4),
            "final_adjust_price": round(final_adjust_price, 4)
        })

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


# ============================================================
# 9. 页面1：价格预测与机理解释
# ============================================================

@app.route("/predict", methods=["POST"])
def predict():
    data = request.json or {}

    try:
        features = convert_input(data)
        task_id = str(uuid.uuid4())
        active_pipeline = get_active_pipeline()

        result = active_pipeline.predict_price(features)

        # 保存时需要features；即使pipeline没有返回，也补进去
        if isinstance(result, dict):
            result.setdefault("features", features)

        RESULT_CACHE[task_id] = result

        # 大模型解释后台执行，预测价格先返回
        future = executor.submit(active_pipeline.explain, result)
        EXPLAIN_TASKS[task_id] = future

        return jsonify({
            "task_id": task_id,
            "pred_price": round(float(result["pred_price"]), 2)
        })

    except Exception as e:
        return jsonify({"error": f"价格预测失败：{str(e)}"}), 500


@app.route("/explain_status/<task_id>")
def explain_status(task_id):
    future = EXPLAIN_TASKS.get(task_id)

    if future is None:
        return jsonify({"status": "not_found"})

    if not future.done():
        return jsonify({"status": "running"})

    try:
        explain_result = future.result()

        if isinstance(explain_result, dict):
            RESULT_CACHE[task_id].update(explain_result)

        EXPLAIN_TASKS.pop(task_id, None)

        return jsonify({
            "status": "finished",
            "data": explain_result
        })

    except Exception as e:
        EXPLAIN_TASKS.pop(task_id, None)
        return jsonify({
            "status": "error",
            "error": str(e)
        })


@app.route("/explain_stream/<task_id>")
def explain_stream(task_id):
    """
    SSE流式解释接口。

    当前版本兼容现有 pipeline.explain():
    - 大模型解释任务运行期间持续发送 running 心跳；
    - 解释完成后按机理条目逐段推送 mechanism；
    - 最后发送 finished。

    如果后续 pipeline.py 提供真正的 token 流式方法，
    可直接在这里替换为逐token yield。
    """
    @stream_with_context
    def generate():
        future = EXPLAIN_TASKS.get(task_id)

        if future is None:
            yield sse_event("error", {
                "message": "未找到机理分析任务"
            })
            return

        # 等待后台解释任务完成，同时给前端发送心跳
        while not future.done():
            yield sse_event("running", {
                "message": "正在生成关键参数影响机理分析..."
            })
            time.sleep(1.0)

        try:
            explain_result = future.result()

            if isinstance(explain_result, dict):
                RESULT_CACHE[task_id].update(explain_result)

            explanation = (
                explain_result.get("explanation", {})
                if isinstance(explain_result, dict)
                else {}
            )

            mechanism = explanation.get(
                "mechanism_analysis",
                []
            )

            if isinstance(mechanism, str):
                mechanism = [mechanism]

            # 分条发送，前端可以一条一条展示
            for index, item in enumerate(mechanism, 1):
                yield sse_event("mechanism", {
                    "index": index,
                    "text": str(item)
                })
                time.sleep(0.05)

            yield sse_event("finished", {
                "message": "机理分析完成",
                "data": explain_result
            })

            EXPLAIN_TASKS.pop(task_id, None)

        except Exception as e:
            EXPLAIN_TASKS.pop(task_id, None)
            yield sse_event("error", {
                "message": str(e)
            })

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive"
        }
    )


# ============================================================
# 10. 页面1：保存结果
# ============================================================

@app.route("/save_csv/<task_id>")
def save_csv(task_id):
    data = RESULT_CACHE.get(task_id)

    if data is None:
        return "无数据", 404

    row = {}
    row.update(data.get("features", {}))
    row["pred_price"] = data.get("pred_price")

    path = os.path.join(RESULT_DIR, f"price_result_{task_id}.csv")
    pd.DataFrame([row]).to_csv(path, index=False, encoding="utf-8-sig")
    return send_file(path, as_attachment=True)


@app.route("/save_txt/<task_id>")
def save_txt(task_id):
    data = RESULT_CACHE.get(task_id)

    if data is None:
        return "无数据", 404

    explanation = data.get("explanation", {})
    mechanism = explanation.get("mechanism_analysis", [])
    if isinstance(mechanism, str):
        mechanism = [mechanism]

    sections = []
    if mechanism:
        sections.append(("关键参数影响机理分析", mechanism))

    # 兼容旧版解释结果结构。
    old_sections = [
        ("总体价格判断", "overall_judgement"),
        ("关键参数影响分析", "parameter_analysis"),
        ("因果链解释", "causal_chain"),
        ("综合结论", "conclusion")
    ]
    for title, key in old_sections:
        value = explanation.get(key)
        if value:
            sections.append((title, value))

    if not sections:
        return "机理分析结果尚未生成，请等待分析完成后再保存", 409

    path = os.path.join(RESULT_DIR, f"mechanism_result_{task_id}.txt")

    with open(path, "w", encoding="utf-8-sig") as f:
        f.write("机理分析结果\n")
        f.write("=" * 50 + "\n\n")
        for section_index, (title, content) in enumerate(sections, 1):
            f.write(f"{section_index}、{title}\n")
            if isinstance(content, list):
                for item_index, item in enumerate(content, 1):
                    f.write(f"{item_index}. {item}\n")
            else:
                f.write(str(content) + "\n")
            f.write("\n")

    return send_file(path, as_attachment=True)


# ============================================================
# 11. 页面2：Top10特征重要度
# ============================================================

@app.route("/api/feature_importance", methods=["GET"])
def api_feature_importance():
    try:
        df = load_feature_importance().head(10)
        data = [
            {
                "rank": int(row["rank"]),
                "feature": str(row["feature"]),
                "importance": round(float(row["importance"]), 6),
                "ratio": round(float(row["ratio"]), 2)
            }
            for _, row in df.iterrows()
        ]
        return jsonify({"top10": data})

    except Exception as e:
        return jsonify({"error": f"读取特征重要度失败：{str(e)}"}), 500


@app.route("/api/sensitivity_features", methods=["GET", "POST"])
def sensitivity_features():
    try:
        candidates = get_sensitivity_features()
        current_features = (request.json or {}).get("features", {}) \
            if request.method == "POST" else {}
        features = filter_analyzable_sensitivity_features(
            current_features, candidates
        )
        return jsonify({"features": features})
    except Exception as e:
        return jsonify({"error": f"读取敏感性分析特征失败：{str(e)}"}), 500



# ============================================================
# 12. 页面3：单参数敏感性分析
# ============================================================

@app.route("/api/sensitivity_single", methods=["POST"])
def sensitivity_single():
    data = request.json or {}
    features = data.get("features", {})
    feature_name = data.get("feature")

    if not features:
        return jsonify({"error": "没有当前产品参数"}), 400

    if feature_name not in features:
        return jsonify({"error": f"当前样本中不存在特征：{feature_name}"}), 400

    history = load_history_data()
    numeric_features, categorical_features = get_feature_types()

    try:
        base_price = predict_one_price(features.copy())
    except Exception as e:
        return jsonify({"error": f"当前产品价格预测失败：{str(e)}"}), 500

    # ---------- 连续数值变量 ----------
    if feature_name in numeric_features:
        if feature_name not in history.columns:
            return jsonify({"error": f"历史数据中不存在字段：{feature_name}"}), 400

        try:
            base_value = float(features[feature_name])
        except Exception:
            return jsonify({"error": f"{feature_name} 当前值不能转换为数值"}), 400

        history_values = pd.to_numeric(
            history[feature_name], errors="coerce"
        ).replace([np.inf, -np.inf], np.nan).dropna()
        if history_values.empty:
            return jsonify({"error": f"{feature_name} 没有有效的历史数值"}), 400

        min_value = float(history_values.min())
        max_value = float(history_values.max())
        if min_value == max_value:
            sample_values = np.array([min_value])
        else:
            # 两端保留样本真实最小值、最大值；中间等距点取普通整数并去重。
            raw_values = np.linspace(min_value, max_value, 21)
            integer_values = []
            for value in raw_values[1:-1]:
                integer_value = int(round(float(value)))
                if min_value < integer_value < max_value:
                    integer_values.append(integer_value)
            sample_values = np.array(
                list(dict.fromkeys([min_value, *integer_values, max_value])),
                dtype=float
            )

        results = []

        for new_value in sample_values:
            new_features = features.copy()
            new_value = float(new_value)
            new_features[feature_name] = new_value

            try:
                new_price = predict_one_price(new_features)
            except Exception as e:
                return jsonify({
                    "error": f"{feature_name} 敏感性预测失败：{str(e)}"
                }), 500

            results.append({
                "feature_value": round(new_value, 6),
                "price": round(new_price, 4)
            })

        return jsonify({
            "feature": feature_name,
            "type": "numeric",
            "base_value": base_value,
            "base_price": round(base_price, 4),
            "min_value": round(min_value, 6),
            "max_value": round(max_value, 6),
            "data": results
        })

    # ---------- 分类变量 ----------
    if feature_name in categorical_features:
        if feature_name not in history.columns:
            return jsonify({"error": f"历史数据中不存在字段：{feature_name}"}), 400

        values = (
            history[feature_name]
            .dropna()
            .astype(str)
            .str.strip()
            .unique()
            .tolist()
        )

        current_value = str(features[feature_name]).strip()
        values = [x for x in values if x != current_value]
        values.insert(0, current_value)

        results = []

        for value in values:
            new_features = features.copy()
            new_value = restore_categorical_value(feature_name, value, history)
            new_features[feature_name] = new_value

            try:
                new_price = predict_one_price(new_features)
            except Exception:
                # 某个历史类别若无法通过当前模型编码，则跳过
                continue

            price_change_rate = (
                (new_price - base_price) / base_price * 100
                if base_price != 0 else 0
            )

            results.append({
                "label": str(value),
                "feature_value": new_value,
                "price": round(new_price, 4),
                "price_change_rate": round(price_change_rate, 4),
                "is_current": str(value) == current_value
            })

        if not results:
            return jsonify({
                "error": f"{feature_name} 没有可成功预测的类别"
            }), 400

        return jsonify({
            "feature": feature_name,
            "type": "categorical",
            "base_value": current_value,
            "base_price": round(base_price, 4),
            "data": results
        })

    return jsonify({"error": f"无法识别 {feature_name} 的变量类型"}), 400


# ============================================================
# 13. Top10连续变量敏感性对比
# ============================================================

@app.route("/api/sensitivity_top10", methods=["POST"])
def sensitivity_top10():
    data = request.json or {}
    features = data.get("features", {})

    if not features:
        return jsonify({"error": "没有当前产品参数"}), 400

    top10 = get_top10_numeric_features()

    if len(top10) == 0:
        return jsonify({
            "error": "当前数据集中没有可用于Top10对比的连续数值特征"
        }), 400

    try:
        base_price = predict_one_price(features.copy())
    except Exception as e:
        return jsonify({"error": f"当前产品预测失败：{str(e)}"}), 500

    series = []

    for feature_name in top10:
        if feature_name not in features:
            continue

        try:
            base_value = float(features[feature_name])
        except Exception:
            continue

        values = []

        for rate in SENSITIVITY_RATES:
            new_features = features.copy()
            new_features[feature_name] = base_value * (1 + rate / 100.0)

            try:
                new_price = predict_one_price(new_features)
            except Exception:
                values.append(None)
                continue

            price_change_rate = (
                (new_price - base_price) / base_price * 100
                if base_price != 0 else 0
            )
            values.append(round(price_change_rate, 4))

        series.append({
            "feature": feature_name,
            "values": values
        })

    return jsonify({
        "change_rates": SENSITIVITY_RATES,
        "base_price": round(base_price, 4),
        "series": series
    })



# ============================================================
# 14. 页面3：Top10重要属性情景模拟字段
# ============================================================

@app.route("/api/scenario_features", methods=["GET"])
def scenario_features():
    try:
        history = load_history_data()
        numeric_features, categorical_features = get_feature_types()
        importance_df = load_feature_importance().head(10)
        result = []

        for feature in importance_df["feature"]:
            feature = str(feature)

            if feature not in history.columns:
                continue

            if feature in numeric_features:
                result.append({
                    "feature": feature,
                    "type": "numeric",
                    "options": []
                })

            elif feature in categorical_features:
                options = (
                    history[feature]
                    .dropna()
                    .astype(str)
                    .str.strip()
                    .unique()
                    .tolist()
                )

                # 当前液压数据若定义了CATEGORY_MAP，优先把文字类别也加入选项
                if feature in CATEGORY_MAP:
                    mapped_options = list(CATEGORY_MAP[feature].keys())
                    if mapped_options:
                        options = list(dict.fromkeys(mapped_options + options))

                result.append({
                    "feature": feature,
                    "type": "categorical",
                    "options": options
                })

        return jsonify({"features": result})

    except Exception as e:
        return jsonify({
            "error": f"读取情景模拟字段失败：{str(e)}"
        }), 500


# ============================================================
# 15. 页面3：多参数情景模拟
# ============================================================

@app.route("/api/scenario_predict", methods=["POST"])
def scenario_predict():
    data = request.json or {}
    original_features = data.get("original_features", {})
    scenario_features = data.get("scenario_features", {})

    if not original_features:
        return jsonify({"error": "没有原始产品参数"}), 400

    history = load_history_data()
    numeric_features, categorical_features = get_feature_types()
    new_features = original_features.copy()
    changed_features = []

    for key, value in scenario_features.items():
        if str(value).strip() == "":
            continue

        old_value = original_features.get(key)

        if key in numeric_features:
            try:
                value = float(value)
            except Exception:
                return jsonify({"error": f"{key} 必须输入数值"}), 400

        elif key in categorical_features:
            value = restore_categorical_value(key, value, history)

        new_features[key] = value

        if str(old_value) != str(value):
            changed_features.append({
                "feature": key,
                "old_value": old_value,
                "new_value": value
            })

    if not changed_features:
        return jsonify({"error": "请至少修改一个参数"}), 400

    try:
        old_price = predict_one_price(original_features.copy())
        new_price = predict_one_price(new_features.copy())

    except Exception as e:
        return jsonify({"error": f"情景预测失败：{str(e)}"}), 500

    price_difference = new_price - old_price
    change_rate = (
        price_difference / old_price * 100
        if old_price != 0 else 0
    )

    return jsonify({
        "old_price": round(old_price, 4),
        "new_price": round(new_price, 4),
        "price_difference": round(price_difference, 4),
        "change_rate": round(change_rate, 2),
        "changed_count": len(changed_features),
        "changed_features": changed_features
    })


# ============================================================
# 16. 启动
# ============================================================

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=7860, debug=True)
