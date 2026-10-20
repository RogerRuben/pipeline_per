# -*- coding: utf-8 -*-
"""Core implementation of the small-sample price AutoML ensemble.

Python syntax and sklearn API are intentionally compatible with scikit-learn 0.21+.
Custom model classes live in an importable module so pickle works after restart.
"""
import os
import pickle
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, RegressorMixin
from sklearn.ensemble import ExtraTreesRegressor, GradientBoostingRegressor, IsolationForest
from sklearn.linear_model import Lasso, Ridge
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import MinMaxScaler, StandardScaler
from sklearn.svm import SVR
from sklearn.metrics import r2_score

MODEL_NAMES = ("lasso", "ridge", "expert_tree", "extratrees", "svr", "gbdt")
TARGET_DEFAULT = "价格_万元"
EXCLUDE_COLS = {"Cluster", "价格类别", "is_outlier"}
EXPERT_MAP = {
    "压力_bar": 5, "出力_kN": 5, "行程_mm": 4, "速度_mm_s": 4,
    "温度_min_°C": 2, "温度_max_°C": 2, "MTBF_小时": 4, "重量_kg": 3,
    "功率_kW": 4, "泄漏_mL_min": 2, "响应时间_ms": 4, "控制方式": 3,
    "适用机型": 2, "材料类型": 4, "密封等级": 4, "液压油类型": 2,
    "流量_L_min": 4, "固有频率_Hz": 3, "阻尼系数": 2, "冗余配置": 3,
    "认证等级": 3, "维护周期_小时": 2, "故障率_10e6_FH": 3,
    "安装方式": 1, "接口类型": 1,
}


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
    """Original custom expert-weighted split-gain tree, refactored to be reusable."""

    def __init__(self, max_depth=10, min_samples_leaf=1,
                 lambda_expert=0.5, min_gain_split=0.0, expert_map=None):
        self.max_depth = max_depth
        self.min_samples_leaf = min_samples_leaf
        self.lambda_expert = lambda_expert
        self.min_gain_split = min_gain_split
        self.expert_map = expert_map

    def fit(self, X, y):
        self.X_ = pd.DataFrame(X).copy()
        self.y_ = np.asarray(y, dtype=float).ravel()
        if len(self.X_) != len(self.y_):
            raise ValueError("X and y length mismatch")
        self.columns_ = self.X_.columns.tolist()
        self.root_ = self._build_tree(self.X_, self.y_, 0, set())
        return self

    @staticmethod
    def _sse(y):
        return 0.0 if len(y) == 0 else float(np.sum((y - np.mean(y)) ** 2))

    def _expert_norm(self, feature):
        mapping = self.expert_map if self.expert_map is not None else {}
        s = mapping.get(feature, 1.0)
        try:
            s = float(s)
            if not np.isfinite(s):
                s = 1.0
        except (ValueError, TypeError):
            s = 1.0
        return (min(5.0, max(1.0, s)) - 1.0) / 4.0

    def _best_split(self, X, y, used_features):
        parent_sse = self._sse(y)
        best_score = -np.inf
        best = (None, None, None, None)
        for feature in X.columns:
            if feature in used_features:
                continue
            raw_values = X[feature].to_numpy(dtype=float)
            values = np.unique(raw_values)
            if values.size <= 1:
                continue
            thresholds = (values[:-1] + values[1:]) / 2.0
            for thr in thresholds:
                left_idx = raw_values <= thr
                right_idx = ~left_idx
                if left_idx.sum() < self.min_samples_leaf or right_idx.sum() < self.min_samples_leaf:
                    continue
                data_gain = parent_sse - (self._sse(y[left_idx]) + self._sse(y[right_idx]))
                if data_gain < self.min_gain_split:
                    continue
                score = data_gain * (1.0 + self.lambda_expert * self._expert_norm(feature))
                if score > best_score:
                    best_score = score
                    best = (feature, float(thr), left_idx, right_idx)
        return best

    def _build_tree(self, X, y, depth, used_features):
        price = np.exp(y)
        node = TreeNode(value=float(np.mean(y)), min_price=float(np.min(price)),
                        max_price=float(np.max(price)), sample_num=len(y))
        if depth >= self.max_depth or len(y) <= self.min_samples_leaf:
            node.is_leaf = True
            return node
        feature, threshold, left_idx, right_idx = self._best_split(X, y, used_features)
        if feature is None:
            node.is_leaf = True
            return node
        node.feature = feature
        node.threshold = threshold
        next_used = set(used_features)
        next_used.add(feature)
        node.left = self._build_tree(X.loc[left_idx].reset_index(drop=True),
                                     y[left_idx], depth + 1, next_used)
        node.right = self._build_tree(X.loc[right_idx].reset_index(drop=True),
                                      y[right_idx], depth + 1, next_used)
        return node

    def _predict_one(self, x, node):
        if node.is_leaf or node.feature is None:
            return node.value
        if x[node.feature] <= node.threshold:
            return self._predict_one(x, node.left)
        return self._predict_one(x, node.right)

    def predict(self, X):
        X = pd.DataFrame(X).copy()
        if X.shape[1] != len(self.columns_):
            raise ValueError("Expert tree: inconsistent number of input features")
        X.columns = self.columns_
        return np.asarray([self._predict_one(X.iloc[i], self.root_)
                           for i in range(len(X))], dtype=float)


def safe_mape(y_true, y_pred):
    """MAPE in fractional units. All prices must be strictly positive."""
    y_true = np.asarray(y_true, dtype=float).reshape(-1)
    y_pred = np.asarray(y_pred, dtype=float).reshape(-1)
    if len(y_true) != len(y_pred) or np.any(y_true <= 0):
        raise ValueError("MAPE needs equally sized arrays and strictly positive true prices")
    return float(np.mean(np.abs(y_pred - y_true) / y_true))


def regression_metrics(y, pred):
    y = np.asarray(y, dtype=float)
    pred = np.asarray(pred, dtype=float)
    if not np.all(np.isfinite(pred)):
        raise ValueError("non-finite prediction")
    return {
        "r2": float(r2_score(y, pred)),
        "mape": safe_mape(y, pred),
        "mae": float(np.mean(np.abs(y - pred))),
        "rmse": float(np.sqrt(np.mean((y - pred) ** 2))),
    }


def read_table(path):
    if path.lower().endswith((".xlsx", ".xls")):
        return pd.read_excel(path)
    if path.lower().endswith(".csv"):
        return pd.read_csv(path, encoding="utf-8-sig")
    raise ValueError("Input must be an Excel (.xlsx/.xls) or CSV file: " + str(path))


def load_training_data(path, target=TARGET_DEFAULT, drop_first=True,
                       remove_outliers=False):
    if not os.path.exists(path):
        raise FileNotFoundError("Cannot find data: {}. Place the real file there or specify --data.".format(path))
    df = read_table(path).copy()
    if target not in df.columns:
        raise ValueError("Target '{}' missing; available columns: {}".format(target, list(df.columns)))
    # Original notebook intentionally skips its first field (row identifier).
    cols = list(df.columns[1:] if drop_first else df.columns)
    features = [c for c in cols if c != target and c not in EXCLUDE_COLS
                and not str(c).startswith("Unnamed:")]
    if not features:
        raise ValueError("No feature columns remain after dropping metadata")
    X = df[features].copy()
    for name in features:
        X[name] = pd.to_numeric(X[name], errors="raise")
    y = pd.to_numeric(df[target], errors="raise").astype(float)
    valid = X.notnull().all(axis=1) & y.notnull() & np.isfinite(X).all(axis=1) & np.isfinite(y)
    valid &= y > 0.0
    X = X.loc[valid].reset_index(drop=True)
    y = y.loc[valid].reset_index(drop=True)
    source_rows = np.flatnonzero(np.asarray(valid)) + 2  # Excel row numbers (+header)
    if remove_outliers:
        if len(X) < 10:
            raise ValueError("Outlier filtering requires at least 10 usable rows")
        sx = StandardScaler().fit_transform(X)
        forest = IsolationForest(contamination=0.05, random_state=42)
        keep = forest.fit_predict(sx) != -1
        X = X.iloc[np.flatnonzero(keep)].reset_index(drop=True)
        y = y.iloc[np.flatnonzero(keep)].reset_index(drop=True)
        source_rows = source_rows[keep]
    if len(X) < 8:
        raise ValueError("Need at least 8 valid records for split metrics")
    return X, y.to_numpy(dtype=float), np.asarray(source_rows, dtype=int)


def make_price_bins(y, test_size, max_bins=3):
    """Only stratify when group sizes can support the requested train/test split."""
    y = np.asarray(y, dtype=float)
    n = len(y)
    ntest = int(np.ceil(n * test_size))
    ntrain = n - ntest
    for nb in range(min(max_bins, ntest, ntrain), 1, -1):
        try:
            groups = pd.qcut(y, q=nb, labels=False, duplicates="drop")
            groups = np.asarray(groups)
            uniques, cnt = np.unique(groups, return_counts=True)
            if len(uniques) > 1 and np.min(cnt) >= 2 and ntest >= len(uniques) and ntrain >= len(uniques):
                return groups
        except (ValueError, TypeError):
            pass
    return None


def split_data(X, y, source_rows, seed, test_size=0.2, price_bins=3):
    strat = make_price_bins(y, test_size, price_bins)
    idx = np.arange(len(y))
    train_idx, test_idx = train_test_split(
        idx, test_size=test_size, random_state=int(seed), stratify=strat)
    return (X.iloc[train_idx], y[train_idx], X.iloc[test_idx], y[test_idx],
            source_rows[train_idx], source_rows[test_idx])


def expert_weight_vector(columns, expert_map, lower, increment):
    values = []
    for col in columns:
        try:
            value = float(expert_map.get(col, 1.0))
        except (ValueError, TypeError):
            value = 1.0
        if not np.isfinite(value):
            value = 1.0
        value = min(5.0, max(1.0, value))
        values.append(lower + (value - 1.0) / 4.0 * increment)
    return np.asarray(values, dtype=float)


def _weighted_array(X, model_name, expert_map):
    arr = X.to_numpy(dtype=float)
    if model_name == "lasso":
        return arr * expert_weight_vector(X.columns, expert_map, 0.5, 2.5)
    if model_name == "ridge":
        return arr * expert_weight_vector(X.columns, expert_map, 0.3, 4.7)
    if model_name == "svr":
        return arr * expert_weight_vector(X.columns, expert_map, 0.5, 2.0)
    return X if model_name == "expert_tree" else arr


def model_params_from_trial(trial, seed_low, seed_high):
    """One joint Optuna trial encodes split seed, all model params, and 6 weights."""
    cfg = {"split_seed": trial.suggest_int("split_seed", seed_low, seed_high)}
    cfg["lasso_alpha"] = trial.suggest_float("lasso_alpha", 1e-5, 1e-1, log=True)
    cfg["ridge_alpha"] = trial.suggest_float("ridge_alpha", 1e-5, 1e2, log=True)
    cfg["svr_kernel"] = trial.suggest_categorical("svr_kernel", ["linear", "rbf"])
    cfg["svr_C"] = trial.suggest_float("svr_C", 0.1, 100.0, log=True)
    cfg["svr_epsilon"] = trial.suggest_float("svr_epsilon", 0.01, 0.3)
    cfg["svr_gamma"] = trial.suggest_categorical("svr_gamma", ["scale", "auto", 0.01, 0.1])
    cfg["gbdt_n_estimators"] = trial.suggest_categorical("gbdt_n_estimators", [50, 100, 200, 300])
    cfg["gbdt_max_depth"] = trial.suggest_int("gbdt_max_depth", 2, 5)
    cfg["gbdt_min_samples_split"] = trial.suggest_categorical("gbdt_min_samples_split", [2, 4, 6, 8, 10])
    cfg["gbdt_learning_rate"] = trial.suggest_float("gbdt_learning_rate", 0.02, 0.3, log=True)
    cfg["gbdt_subsample"] = trial.suggest_float("gbdt_subsample", 0.65, 1.0)
    cfg["et_n_estimators"] = trial.suggest_categorical("et_n_estimators", [50, 100, 200, 300])
    cfg["et_max_depth"] = trial.suggest_categorical("et_max_depth", [5, 10, 20, 30])
    cfg["et_min_samples_split"] = trial.suggest_categorical("et_min_samples_split", [2, 4, 6])
    cfg["et_max_features"] = trial.suggest_categorical("et_max_features", [0.5, 0.8, 1.0])
    cfg["tree_max_depth"] = trial.suggest_int("tree_max_depth", 2, 9)
    cfg["tree_min_samples_leaf"] = trial.suggest_categorical("tree_min_samples_leaf", [1, 2])
    cfg["tree_lambda_expert"] = trial.suggest_float("tree_lambda_expert", 0.0, 1.5)
    cfg["tree_min_gain_split"] = trial.suggest_float("tree_min_gain_split", 0.0, 0.15)
    for name in MODEL_NAMES:
        cfg["weight_" + name] = trial.suggest_float("weight_" + name, 0.0, 1.0)
    return cfg


def normalized_weights(params, sparse_threshold=0.12):
    raw = np.array([max(0.0, float(params["weight_" + name])) for name in MODEL_NAMES])
    # Permit zero weight (exclude a weak model). Prevent all-zero solution.
    raw[raw < sparse_threshold] = 0.0
    if not np.any(raw):
        raw[np.argmax([float(params["weight_" + name]) for name in MODEL_NAMES])] = 1.0
    return raw / raw.sum()


def instantiate_model(name, cfg, expert_map):
    if name == "lasso":
        return Lasso(alpha=float(cfg["lasso_alpha"]), max_iter=10000)
    if name == "ridge":
        return Ridge(alpha=float(cfg["ridge_alpha"]))
    if name == "svr":
        return SVR(kernel=cfg["svr_kernel"], C=float(cfg["svr_C"]),
                   epsilon=float(cfg["svr_epsilon"]), gamma=cfg["svr_gamma"])
    if name == "gbdt":
        return GradientBoostingRegressor(
            n_estimators=int(cfg["gbdt_n_estimators"]),
            max_depth=int(cfg["gbdt_max_depth"]),
            min_samples_split=int(cfg["gbdt_min_samples_split"]),
            learning_rate=float(cfg["gbdt_learning_rate"]),
            subsample=float(cfg["gbdt_subsample"]), random_state=0)
    if name == "extratrees":
        return ExtraTreesRegressor(
            n_estimators=int(cfg["et_n_estimators"]),
            max_depth=int(cfg["et_max_depth"]),
            min_samples_split=int(cfg["et_min_samples_split"]),
            max_features=float(cfg["et_max_features"]),
            random_state=0, n_jobs=1)
    if name == "expert_tree":
        return ExpertWeightedTreeRegressor(
            max_depth=int(cfg["tree_max_depth"]),
            min_samples_leaf=int(cfg["tree_min_samples_leaf"]),
            lambda_expert=float(cfg["tree_lambda_expert"]),
            min_gain_split=float(cfg["tree_min_gain_split"]), expert_map=dict(expert_map))
    raise KeyError(name)


def fit_ensemble(X_train, y_train, cfg, expert_map, active_weights=None):
    if active_weights is None:
        active_weights = normalized_weights(cfg)
    scaler = MinMaxScaler(feature_range=(0, 1)).fit(X_train)
    Xscaled = pd.DataFrame(scaler.transform(X_train), columns=X_train.columns, index=X_train.index)
    log_y = np.log(np.asarray(y_train, dtype=float))
    models = {}
    for i, name in enumerate(MODEL_NAMES):
        if active_weights[i] == 0:
            continue
        model = instantiate_model(name, cfg, expert_map)
        model.fit(_weighted_array(Xscaled, name, expert_map), log_y)
        models[name] = model
    bundle = {"scaler": scaler, "models": models, "weights": np.asarray(active_weights),
              "feature_names": list(X_train.columns), "expert_map": dict(expert_map),
              "params": dict(cfg), "log_target": True, "model_names": MODEL_NAMES}
    return bundle


def predict_bundle(bundle, new_features):
    if not isinstance(new_features, pd.DataFrame):
        raise TypeError("Prediction input must be pandas.DataFrame with named columns")
    feature_names = bundle["feature_names"]
    missing = [c for c in feature_names if c not in new_features.columns]
    if missing:
        raise ValueError("Missing required features: " + str(missing))
    X = new_features.loc[:, feature_names].copy()
    if X.columns.duplicated().any():
        raise ValueError("Duplicated feature names")
    for c in feature_names:
        X[c] = pd.to_numeric(X[c], errors="raise")
    if X.isnull().any().any() or not np.isfinite(X.to_numpy(dtype=float)).all():
        raise ValueError("Input contains missing or nonfinite features")
    Xscaled = pd.DataFrame(bundle["scaler"].transform(X), columns=feature_names, index=X.index)
    weight = bundle["weights"]
    predictions = []
    for i, name in enumerate(MODEL_NAMES):
        if name not in bundle["models"]:
            predictions.append(np.zeros(len(Xscaled)))
            continue
        model_X = _weighted_array(Xscaled, name, bundle["expert_map"])
        log_pred = np.asarray(bundle["models"][name].predict(model_X), dtype=float)
        pred = np.exp(np.clip(log_pred, -700, 700))
        predictions.append(pred)
    pred_matrix = np.column_stack(predictions)
    ensemble = np.dot(pred_matrix, weight)
    return ensemble, pd.DataFrame(pred_matrix, columns=MODEL_NAMES, index=X.index)


def save_bundle(bundle, path):
    with open(path, "wb") as f:
        pickle.dump(bundle, f, protocol=4)


def load_bundle(path):
    # Warning: pickle is safe only for files you trust.
    with open(path, "rb") as f:
        return pickle.load(f)
