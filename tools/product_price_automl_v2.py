# -*- coding: utf-8 -*-
"""Executable grouped AutoML, seed averaging and expert-constrained fusion.

All inner decisions are repeated inside outer training folds. The holdout is
evaluated once after freezing the procedure. No installed model is modified.
"""
from __future__ import print_function

import copy
import hashlib
import json
import multiprocessing as mp
import queue
import sys
import time
import warnings
from pathlib import Path

from app.console_compat import enable_plain_console
enable_plain_console()

import numpy as np
import optuna
import pandas as pd
import scipy
import sklearn
from sklearn.compose import ColumnTransformer, TransformedTargetRegressor
from sklearn.ensemble import ExtraTreesRegressor, RandomForestRegressor, GradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.kernel_ridge import KernelRidge
from sklearn.linear_model import Ridge, Lasso, ElasticNet, BayesianRidge, HuberRegressor
from sklearn.model_selection import GroupShuffleSplit, LeaveOneGroupOut
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler, RobustScaler, PowerTransformer
from sklearn.svm import SVR

from services.price_service.ensemble_estimators import (AttributeInteractions, SmallSampleSelector, SmallSampleKNN,
                                                       EngineeringPriceRegressor, EngineeringResidualRegressor, StableFoldImputer,
                                                       RawFeatureSubset, FoldwiseProjection, MAPEConstantRegressor, EngineeringCalibratedRegressor, EffectiveDFRidge)
from services.price_service.export_native_price_bundle import export_from_notebook
from services.price_service.native_bundle import predict, load_bundle, save_bundle
from tools.product_price_automl import price_dataset, grouped_folds, native_field_metadata, metrics, write_json
from tools.price_expert_fusion_probe import fit_expert_weights, FusionOptimizationError


LABELS = {"ridge": "岭回归", "lasso": "稀疏线性回归", "elasticnet": "弹性网络", "bayesian_ridge": "贝叶斯岭回归",
          "huber": "稳健线性回归", "svr": "支持向量回归", "kernel_ridge": "核岭回归", "knn": "近邻回归",
          "random_forest": "随机森林", "extra_trees": "极端随机树", "gbdt": "梯度提升树",
          "engineering_cost": "专家工程成本", "engineering_residual": "工程成本与残差修正", "baseline": "常量价格参考"}
FAMILIES = dict((name, "linear") for name in ("ridge", "lasso", "elasticnet", "bayesian_ridge", "huber"))
FAMILIES.update(dict((name, "kernel") for name in ("svr", "kernel_ridge", "knn")))
FAMILIES.update(dict((name, "tree") for name in ("random_forest", "extra_trees", "gbdt")))
FAMILIES.update({"engineering_cost": "engineering", "engineering_residual": "engineering"})
FAMILIES["baseline"] = "baseline"
DEFAULT_CONFIG = {
    "schema_version": "price-automl-v2-1.0",
    "experts_from_workbook": False,
    "models": [name for name in LABELS if not name.startswith("engineering") and name != "baseline"],
    "search": {"trials": 26, "ensemble_trials": 6, "promote": 8, "seed": 2026, "parameter_ranges": {}},
    "resources": {"total_seconds": 300, "fit_timeout_seconds": 30, "cache_directory": None},
    "validation": {"minimum_groups": 8, "holdout_fraction": .2, "holdout_seed": 20261002,
                   "folds": 3, "repeats": 2, "split_seed": 311, "outer_folds": 3, "outer_repeats": 1},
    "pipeline": {"imputation": ["median", "mean"], "scaler": ["standard", "robust", "none"],
                 "numeric_transform": ["identity"], "target_transform": ["identity", "log"],
                 "select_k": ["all", 3, 6], "missing_indicator": [False, True], "interaction_pairs": []},
    "seed_bagging": {"base": 101, "milestones": [4, 8, 16], "stable_rounds": 2,
                     "prediction_change_percent": .1, "mape_gain_percentage_points": .05},
    "ensemble": {"mode": "data_only", "alpha": .7, "prior_strength": .01,
                 "max_mape_delta_pp": .1, "max_r2_drop": .005, "max_members": 6, "top_k": 3,
                 "minimum_weight": 1e-8, "family_priors": []},
    "engineering": {"intercept_wan": 0.0, "coefficients": {}},
    "quality": {"mape_percent_max": 10.0, "r2_min": .85, "minimum_independent_groups_for_evidence": 30},
    "small_sample": {"policy": "auto", "max_groups": 29, "ridge_alphas": [1., 10., 100.],
                     "latent_components": 2, "max_members": 4, "tree_seeds": 4,
                     "expert_features": [], "expert_max_features": 8, "calibration_strength": 8., "max_effective_df": 3.,
                     "minimum_group_win_fraction": .6, "minimum_mape_gain_pp": .1, "log_target": True,
                     "weight_jackknife": True, "jackknife_blends": [0., .25, .5, .75, 1.]}}


def load_config(path=None, overrides=None):
    supplied = json.loads(Path(path).read_text(encoding="utf-8-sig")) if path else (overrides or {})
    config = copy.deepcopy(DEFAULT_CONFIG)
    def merge(target, source, location=""):
        if not isinstance(source, dict):
            raise ValueError("配置须为对象：" + location)
        for key, value in source.items():
            if key not in target:
                raise ValueError("未知V2设置：" + location + key)
            if isinstance(target[key], dict) and key not in ("parameter_ranges", "coefficients"):
                merge(target[key], value, location + key + ".")
            else:
                target[key] = value
    merge(config, supplied)
    if path and overrides:
        merge(config, overrides)
    if config["schema_version"] != DEFAULT_CONFIG["schema_version"]:
        raise ValueError("请使用可执行V2配置，设计草稿不能直接用于训练。")
    if not config["models"] or len(set(config["models"])) != len(config["models"]) or set(config["models"]) - set(LABELS):
        raise ValueError("V2模型名单无效。")
    parameter_names = {"ridge": {"alpha"}, "lasso": {"alpha"}, "elasticnet": {"alpha", "l1_ratio"},
                       "bayesian_ridge": {"alpha_1", "alpha_2", "lambda_1", "lambda_2"}, "huber": {"epsilon", "alpha"},
                       "svr": {"C", "gamma", "epsilon"}, "kernel_ridge": {"alpha", "gamma"}, "knn": {"n_neighbors"},
                       "engineering_cost": set(), "engineering_residual": {"alpha"}, "baseline": set()}
    for model in ("random_forest", "extra_trees", "gbdt"):
        parameter_names[model] = {"n_estimators", "max_depth", "min_samples_leaf", "min_samples_split", "max_features"}
    parameter_names["gbdt"].update({"learning_rate", "subsample"})
    for model, ranges in config["search"]["parameter_ranges"].items():
        if model not in parameter_names or set(ranges) - parameter_names[model]:
            raise ValueError("未知的模型参数范围：" + model)
        for name, bounds in ranges.items():
            if not isinstance(bounds, list) or len(bounds) not in (2, 3) or not np.all(np.isfinite(bounds[:2])) or bounds[0] > bounds[1]:
                raise ValueError("参数范围须为[下限,上限,可选对数标志]：" + name)
    v, s, e, p, r = (config[key] for key in ("validation", "search", "ensemble", "pipeline", "resources"))
    if v["minimum_groups"] < 8 or not 0 < v["holdout_fraction"] < .5 or min(v["folds"], v["outer_folds"]) < 2:
        raise ValueError("验证至少8组、至少2折，保留比例须大于0且小于0.5。")
    if min(v["repeats"], v["outer_repeats"], s["promote"], e["max_members"], e["top_k"]) < 1:
        raise ValueError("重复数和候选数量须为正。")
    if s["trials"] < len(config["models"]) or s["ensemble_trials"] < 0 or min(r["total_seconds"], r["fit_timeout_seconds"]) <= 0:
        raise ValueError("搜索次数须覆盖全部模型，预算须为正。")
    if e["mode"] not in ("data_only", "soft_prior", "performance_budget", "equal_topk", "greedy_selection"):
        raise ValueError("融合模式无效。")
    for key, supported in (("imputation", {"mean", "median"}), ("scaler", {"none", "standard", "robust"}),
                           ("numeric_transform", {"identity", "yeo_johnson"}), ("target_transform", {"identity", "log"})):
        if not p[key] or set(p[key]) - supported:
            raise ValueError("预处理设置无效：" + key)
    if not p["select_k"] or any(k != "all" and (not isinstance(k, int) or k < 1) for k in p["select_k"]):
        raise ValueError("特征选择数量须为all或正整数。")
    if not p["missing_indicator"] or any(not isinstance(value, bool) for value in p["missing_indicator"]):
        raise ValueError("缺失指示设置须为布尔数组。")
    seeds = config["seed_bagging"]
    if not seeds["milestones"] or seeds["milestones"] != sorted(set(seeds["milestones"])) or min(seeds["milestones"]) < 2 or max(seeds["milestones"]) > 32:
        raise ValueError("训练种子档位须递增，范围2至32。")
    if seeds["stable_rounds"] < 1 or min(seeds["prediction_change_percent"], seeds["mape_gain_percentage_points"]) < 0:
        raise ValueError("种子稳定性设置无效。")
    priors = e["family_priors"]
    if len({item["family"] for item in priors}) != len(priors) or any(item["family"] not in set(FAMILIES.values()) for item in priors):
        raise ValueError("专家模型族代号无效或重复。")
    if sum(float(item.get("weight", 0)) for item in priors) > 1 + 1e-8:
        raise ValueError("专家模型族先验份额总和不能超过1。")
    if e["mode"] in ("performance_budget", "soft_prior") and not priors:
        raise ValueError("专家融合模式需要预先给出的模型族权重。")
    if not 0 <= e["alpha"] <= 1 or min(e["prior_strength"], e["max_mape_delta_pp"], e["max_r2_drop"]) < 0:
        raise ValueError("融合目标/预算无效。")
    if not 0 <= e["minimum_weight"] < 1 / e["max_members"]:
        raise ValueError("成员删除阈值无效。")
    if any(name.startswith("engineering") for name in config["models"]) and not config["engineering"]["coefficients"]:
        raise ValueError("工程模型需要预先配置带单位的成本系数。")
    small = config["small_sample"]
    if small["policy"] not in ("auto", "off", "force") or not 8 <= small["max_groups"] <= 100:
        raise ValueError("微样本策略须为auto/off/force，组数上限为8至100。")
    if not small["ridge_alphas"] or len(small["ridge_alphas"]) > 5 or any(not np.isfinite(a) or a <= 0 for a in small["ridge_alphas"]):
        raise ValueError("微样本岭回归须预设1至5个正的收缩强度。")
    for key in ("max_groups", "latent_components", "max_members", "tree_seeds", "expert_max_features"):
        if not isinstance(small[key], int) or isinstance(small[key], bool):
            raise ValueError("微样本数量设置须为整数：" + key)
    if not 1 <= small["latent_components"] <= 5 or not 2 <= small["max_members"] <= 6 or not 2 <= small["tree_seeds"] <= 32:
        raise ValueError("微样本潜变量/成员/种子数量无效。")
    if small["expert_max_features"] < 1 or not np.isfinite(small["calibration_strength"]) or small["calibration_strength"] <= 0:
        raise ValueError("专家子集数量与先验校准收缩强度须为正。")
    if not 1 < small["max_effective_df"] <= 10 or not 0 <= small["minimum_group_win_fraction"] <= 1 or not np.isfinite(small["minimum_mape_gain_pp"]) or small["minimum_mape_gain_pp"] < 0:
        raise ValueError("微样本自由度、组胜率或MAPE增益设置无效。")
    if not isinstance(small["weight_jackknife"], bool) or not isinstance(small["log_target"], bool) or not isinstance(small["expert_features"], list) or any(not isinstance(key, str) for key in small["expert_features"]):
        raise ValueError("微样本权重诊断开关须为布尔；专家属性须为英文代号数组。")
    if any(not np.isfinite(value) or not 0 <= value <= 1 for value in small["jackknife_blends"]) or small["jackknife_blends"] != sorted(set(small["jackknife_blends"])) or not small["jackknife_blends"] or small["jackknife_blends"][0] != 0:
        raise ValueError("权重稳定化档位须从0起、递增且不超过1。")
    return config


def workbook_experts(input_path, config_path):
    """Only use workbook expert sheets when the explicit config enables them."""
    raw_config = json.loads(Path(config_path).read_text(encoding="utf-8-sig")) if config_path else {}
    if not raw_config.get("experts_from_workbook") or Path(input_path).suffix.lower() != ".xlsx":
        return {}
    from app.xlsx_utils import read_workbook_bytes
    from app.product_table import clean_field_id
    sheets = read_workbook_bytes(Path(input_path).read_bytes())
    if "专家模型权重" not in sheets or "价格工程先验" not in sheets:
        raise ValueError("配置要求工作簿专家先验，但缺少专家模型权重或价格工程先验表。")
    priors = []
    for row in sheets["专家模型权重"][1:]:
        if not row or not str(row[0] or "").strip():
            continue
        priors.append({"family": clean_field_id(row[0]), "label": str(row[1] or row[0]), "weight": float(row[2]),
                       "confidence": float(row[3]), "lower_bound": float(row[4] or 0), "upper_bound": float(row[5])})
    engineering = {"coefficients": {}, "intercept_wan": 0.0}
    for row in sheets["价格工程先验"][1:]:
        if not row or not str(row[0] or "").strip():
            continue
        key = clean_field_id(row[0])
        if key == "__intercept__":
            engineering["intercept_wan"] = float(row[2])
        else:
            if key in engineering["coefficients"]:
                raise ValueError("价格工程先验中的英文代号清理后重复：" + key)
            engineering["coefficients"][key] = float(row[2])
    return {"ensemble": {"family_priors": priors}, "engineering": engineering}


def _suggest(trial, config):
    name = trial.suggest_categorical("model", config["models"])
    result = {"model": name}
    if name == "baseline":
        return result
    def number(key, low, high, log=False, integer=False):
        override = config["search"]["parameter_ranges"].get(name, {}).get(key)
        if override is not None:
            low, high = override[:2]
            log = bool(override[2]) if len(override) > 2 else log
        value = trial.suggest_int(name + "_" + key, int(low), int(high), log=log) if integer else trial.suggest_float(name + "_" + key, float(low), float(high), log=log)
        result[key] = value
    if name in ("ridge", "lasso", "elasticnet", "kernel_ridge", "engineering_residual"):
        number("alpha", 1e-4, 100, True)
    if name == "elasticnet":
        number("l1_ratio", .1, .9)
    if name == "bayesian_ridge":
        for key in ("alpha_1", "alpha_2", "lambda_1", "lambda_2"):
            number(key, 1e-8, 1e-3, True)
    if name == "huber":
        number("epsilon", 1.1, 2)
        number("alpha", 1e-4, 10, True)
    if name in ("svr", "kernel_ridge"):
        number("gamma", 1e-5, .3, True)
    if name == "svr":
        number("C", .1, 300, True)
        number("epsilon", .001, .5, True)
    if name == "knn":
        number("n_neighbors", 1, 5, integer=True)
    if name in ("random_forest", "extra_trees", "gbdt"):
        number("n_estimators", 30, 120, integer=True)
        number("max_depth", 1, 6, integer=True)
        number("min_samples_leaf", 1, 3, integer=True)
        number("min_samples_split", 2, 4, integer=True)
        number("max_features", .3, 1)
    if name == "gbdt":
        number("learning_rate", .02, .2, True)
        number("subsample", .7, 1)
    if name != "engineering_cost":
        result["pipeline"] = dict((key, trial.suggest_categorical(key, config["pipeline"][key])) for key in
                                  ("imputation", "scaler", "numeric_transform", "select_k", "missing_indicator"))
        result["target_transform"] = "identity" if name == "engineering_residual" else trial.suggest_categorical("target_transform", config["pipeline"]["target_transform"])
        result["interactions"] = bool(config["pipeline"]["interaction_pairs"]) and trial.suggest_categorical("interactions", [False, True])
    return result


def estimator(candidate, seed, specs, config):
    name = candidate["model"]
    keys = [p["parameter_id"] for p in specs]
    core = EngineeringPriceRegressor(keys, config["engineering"]["coefficients"], config["engineering"]["intercept_wan"])
    if name == "baseline":
        return MAPEConstantRegressor()
    if name == "engineering_cost":
        return core
    if candidate.get("micro_calibration"):
        return EngineeringCalibratedRegressor(core, strength=candidate["alpha"])
    settings = candidate["pipeline"]
    micro = bool(settings.get("micro", False))
    subset = candidate.get("feature_subset")
    active_indices = [keys.index(key) for key in subset] if subset else list(range(len(keys)))
    active_specs = [specs[i] for i in active_indices]
    pairs = [(keys.index(a), keys.index(b)) for a, b in config["pipeline"]["interaction_pairs"]] if candidate["interactions"] else []
    numeric = [i for i, p in enumerate(active_specs) if p["value_type"] not in ("enum", "boolean")] + list(range(len(keys), len(keys) + len(pairs)))
    imputer = StableFoldImputer if micro else SimpleImputer
    numeric_steps = [("imputer", imputer(strategy=settings["imputation"], add_indicator=settings["missing_indicator"]))]
    if settings["numeric_transform"] == "yeo_johnson":
        numeric_steps.append(("power", PowerTransformer(method="yeo-johnson", standardize=False)))
    if settings["scaler"] != "none":
        numeric_steps.append(("scale", StandardScaler() if settings["scaler"] == "standard" else RobustScaler()))
    transforms = []
    if numeric:
        transforms.append(("numeric", Pipeline(numeric_steps), numeric))
    for kind in ("enum", "boolean"):
        indices = [i for i, p in enumerate(active_specs) if p["value_type"] == kind]
        if indices:
            steps = [("imputer", imputer(strategy="most_frequent"))]
            steps.append(("encode", OneHotEncoder(handle_unknown="ignore", sparse=False)) if kind == "enum" else ("scale", StandardScaler()))
            transforms.append((kind, Pipeline(steps), indices))
    params = dict((key, value) for key, value in candidate.items() if key not in ("model", "pipeline", "target_transform", "interactions", "feature_subset"))
    factories = {"ridge": Ridge, "lasso": Lasso, "elasticnet": ElasticNet, "bayesian_ridge": BayesianRidge,
                 "huber": HuberRegressor, "svr": SVR, "kernel_ridge": KernelRidge, "knn": SmallSampleKNN,
                 "random_forest": RandomForestRegressor, "extra_trees": ExtraTreesRegressor, "gbdt": GradientBoostingRegressor,
                 "engineering_residual": Ridge}
    if micro and name == "ridge":
        factories["ridge"] = EffectiveDFRidge
        params["max_effective_df"] = config["small_sample"]["max_effective_df"]
    if name in ("lasso", "elasticnet", "huber"):
        params["max_iter"] = 2000
    if name in ("random_forest", "extra_trees", "gbdt"):
        params["random_state"] = int(seed)
        if name != "gbdt":
            params["n_jobs"] = 1
        else:
            params["loss"] = "huber"
    if name == "kernel_ridge":
        params["kernel"] = "rbf"
    feature_step = RawFeatureSubset(active_indices) if subset else AttributeInteractions(pairs)
    reduction = FoldwiseProjection(settings.get("latent_components")) if micro else SmallSampleSelector(settings["select_k"])
    model = Pipeline([("features", feature_step), ("prepare", ColumnTransformer(transforms)),
                      ("select", reduction), ("model", factories[name](**params))])
    if name == "engineering_residual":
        return EngineeringResidualRegressor(core, model)
    return TransformedTargetRegressor(regressor=model, func=np.log, inverse_func=np.exp) if candidate["target_transform"] == "log" else model


class BudgetExpired(RuntimeError):
    pass


def _worker_loop(incoming, outgoing, factory=None):
    from threadpoolctl import threadpool_limits
    while True:
        payload = incoming.get()
        if payload is None:
            return
        candidate, seed, specs, config, frame, y, validation, return_model = payload
        try:
            with warnings.catch_warnings(), threadpool_limits(limits=1):
                warnings.simplefilter("ignore")
                fitted = (factory or estimator)(candidate, seed, specs, config).fit(frame, y)
                predictions = np.asarray(fitted.predict(validation), dtype=float)
            if not np.all(np.isfinite(predictions)) or np.any(predictions <= 0):
                finite = np.isfinite(predictions)
                minimum = float(np.min(predictions[finite])) if np.any(finite) else None
                raise ValueError("候选产生非有限或非正价格（最小有限预测=%s，非正=%d，非有限=%d）" %
                                 (minimum, np.count_nonzero(finite & (predictions <= 0)), np.count_nonzero(~finite)))
            outgoing.put((True, predictions, fitted if return_model else None))
        except Exception as exc:
            outgoing.put((False, type(exc).__name__ + ": " + str(exc), None))


class FitWorker(object):
    def __init__(self, config, deadline, factory=None):
        self.config, self.deadline = config, deadline
        self.factory = factory
        self.process = None

    def close(self):
        if self.process is not None:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(timeout=5)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=5)
            self.incoming.cancel_join_thread()
            self.outgoing.cancel_join_thread()
            self.incoming.close()
            self.outgoing.close()
            self.process = None

    def fit(self, candidate, seed, specs, frame, y, validation, return_model=False):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise BudgetExpired("V2总预算耗尽，必要审核/导出未完成。请增加总预算后重试；OOF缓存可复用。")
        if self.process is None:
            context = mp.get_context("spawn")
            self.incoming, self.outgoing = context.Queue(), context.Queue()
            self.process = context.Process(target=_worker_loop, args=(self.incoming, self.outgoing, self.factory), daemon=True)
            self.process.start()
        self.incoming.put((candidate, seed, specs, self.config, frame, y, validation, return_model))
        timeout = min(remaining, self.config["resources"]["fit_timeout_seconds"])
        try:
            success, predictions, fitted = self.outgoing.get(timeout=timeout)
        except queue.Empty:
            self.close()
            if time.monotonic() >= self.deadline:
                raise BudgetExpired("V2总预算耗尽。")
            raise RuntimeError("单次拟合超时，已停止该训练工作进程。")
        if not success:
            raise ValueError(predictions)
        return predictions, fitted


def _loss(y, prediction, alpha):
    score = metrics(y, prediction)
    return alpha * score["mape_percent"] / 100 + (1 - alpha) * (1 - score["r2"])


def diagnostic_metrics(y, prediction):
    ape = np.abs((np.asarray(y) - prediction) / y) * 100
    return dict(metrics(y, prediction), mdape_percent=float(np.median(ape)), p90_ape_percent=float(np.quantile(ape, .9)),
                p95_ape_percent=float(np.quantile(ape, .95)), wape_percent=float(np.sum(np.abs(y - prediction)) / np.sum(y) * 100))


def _greedy(y, bank, alpha, maximum=6):
    counts = np.zeros(bank.shape[1])
    best, best_loss = None, float("inf")
    for _ in range(40):
        choices = []
        for index in range(bank.shape[1]):
            if not counts[index] and np.count_nonzero(counts) >= maximum:
                continue
            proposed = counts.copy()
            proposed[index] += 1
            proposed /= np.sum(proposed)
            choices.append((_loss(y, bank.dot(proposed), alpha), index, proposed))
        score, index, proposed = min(choices, key=lambda item: (item[0], item[1]))
        counts[index] += 1
        if score < best_loss - 1e-12:
            best, best_loss = proposed, score
    return best


def _family_records(candidates, config):
    return [dict(item, name=item.get("label", item["family"]),
                 members=[i for i, candidate in enumerate(candidates) if FAMILIES[candidate["config"]["model"]] == item["family"]])
            for item in config["ensemble"]["family_priors"]]


def _fusion(y, bank, candidates, config, zero_indices=()):
    settings = config["ensemble"]
    mode = settings["mode"]
    groups = _family_records(candidates, config)
    if mode in ("equal_topk", "greedy_selection"):
        if any(item.get("lower_bound", 0) or item.get("upper_bound", 1) < 1 for item in groups):
            raise ValueError("简单融合不支持专家硬界，请使用受约束权重模式。")
        weights = _greedy(y, bank, settings["alpha"], settings["max_members"])
        if mode == "equal_topk":
            selected = sorted(range(bank.shape[1]), key=lambda i: _loss(y, bank[:, i], settings["alpha"]))[:min(settings["top_k"], settings["max_members"])]
            weights = np.zeros(bank.shape[1]); weights[selected] = 1.0 / len(selected)
        return {"mode": mode, "weights": weights.tolist(), "expert_groups": []}
    upper = np.ones(bank.shape[1]); upper[list(zero_indices)] = 0.0
    return fit_expert_weights(y, bank, mode=mode, prior_strength=settings["prior_strength"], alpha=settings["alpha"], upper_bounds=upper,
                              max_mape_delta_pp=settings["max_mape_delta_pp"], max_r2_drop=settings["max_r2_drop"], expert_groups=groups)


def _oof(frame, y, groups, specs, candidate, seeds, config, worker, cache, split_seeds):
    code_digest = hashlib.sha256(Path(__file__).read_bytes() + Path(sys.modules[EngineeringPriceRegressor.__module__].__file__).read_bytes()).hexdigest()
    identity = {"candidate": candidate, "training_seeds": seeds, "split_seeds": split_seeds,
                "validation": config["validation"], "engineering": config["engineering"], "pipeline": config["pipeline"],
                "versions": [sklearn.__version__, np.__version__, scipy.__version__], "code": code_digest,
                "schema": [{"key": p["parameter_id"], "type": p["value_type"]} for p in specs]}
    key = hashlib.sha256(frame.values.tobytes() + y.tobytes() + groups.tobytes() +
                         json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()
    path = cache / (key + ".npz")
    if path.exists():
        with np.load(str(path), allow_pickle=False) as saved:
            bank = saved["predictions"]
        if bank.shape == (len(seeds), len(split_seeds), len(y)) and np.all(np.isfinite(bank)) and np.all(bank > 0):
            return bank, True
    bank = np.zeros((len(seeds), len(split_seeds), len(y)))
    folds = min(config["validation"]["folds"], len(np.unique(groups)) // 2)
    if folds < 2:
        raise ValueError("内层至少需要4组独立成品。")
    for repetition, split_seed in enumerate(split_seeds):
        for training, validation in grouped_folds(groups, folds, split_seed):
            if set(groups[training]) & set(groups[validation]):
                raise AssertionError("业务组跨折")
            for index, seed in enumerate(seeds):
                bank[index, repetition, validation] = worker.fit(candidate, seed, specs, frame.iloc[training], y[training], frame.iloc[validation])[0]
    cache.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        np.savez_compressed(handle, predictions=bank)
    return bank, False


def _inner(frame, y, groups, specs, config, worker, cache, directory, progress):
    directory.mkdir(parents=True, exist_ok=True)
    split_seeds = [config["validation"]["split_seed"] + 1009 * i for i in range(config["validation"]["repeats"])]
    stream = [int(seed) for seed in np.random.SeedSequence(config["seed_bagging"]["base"]).generate_state(max(config["seed_bagging"]["milestones"]))]
    candidates, failures, seen, cache_hits = [], [], set(), [0]
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    def evaluate_candidate(trial, frozen=None):
        candidate = _suggest(trial, config)
        trial.set_user_attr("effective_config", candidate)
        signature = json.dumps(candidate, sort_keys=True)
        if signature in seen:
            raise optuna.TrialPruned("相同有效配置已缓存")
        seen.add(signature)
        try:
            predictions, hit = _oof(frame, y, groups, specs, candidate, [stream[0]], config, worker, cache, split_seeds)
            cache_hits[0] += int(hit)
            prediction = predictions.mean(axis=(0, 1))
            candidate_row = {"config": candidate, "seed_history": [], "seeds": [stream[0]], "oof_repeats": predictions.mean(axis=0),
                             "oof": prediction, "screen_metrics": metrics(y, prediction)}
            candidates.append(candidate_row)
            score = metrics(y, prediction)
            if frozen is not None:
                augmented = np.column_stack([frozen, prediction])
                mixed = augmented.dot(_greedy(y, augmented, config["ensemble"]["alpha"], config["ensemble"]["max_members"]))
                score = metrics(y, mixed)
                trial.set_user_attr("ensemble_context", "frozen_screen_library")
            trial.set_user_attr("cv", score)
            progress("V2候选已完成%d个；当前模型：%s" % (len(candidates), LABELS[candidate["model"]]))
            return score["mape_percent"], score["r2"]
        except BudgetExpired:
            raise
        except Exception as exc:
            failures.append({"config": candidate, "reason": str(exc)})
            raise optuna.TrialPruned(str(exc))
    study = optuna.create_study(directions=["minimize", "maximize"], sampler=optuna.samplers.TPESampler(seed=config["search"]["seed"]))
    for name in config["models"]:
        study.enqueue_trial({"model": name})
    study.optimize(evaluate_candidate, n_trials=config["search"]["trials"])
    if not candidates:
        raise RuntimeError("全部价格候选失败。")
    frozen = np.column_stack([row["oof"] for row in candidates])
    if config["search"]["ensemble_trials"]:
        joint = optuna.create_study(directions=["minimize", "maximize"], sampler=optuna.samplers.TPESampler(seed=config["search"]["seed"] + 701))
        joint.optimize(lambda trial: evaluate_candidate(trial, frozen), n_trials=config["search"]["ensemble_trials"])
    bank = np.column_stack([row["oof"] for row in candidates])
    greedy = _greedy(y, bank, config["ensemble"]["alpha"], config["ensemble"]["max_members"])
    ranked = sorted(range(len(candidates)), key=lambda i: (-greedy[i], _loss(y, bank[:, i], config["ensemble"]["alpha"])))
    promoted = []
    # Every available expert family gets a chance; priors apply to its total share.
    for family in sorted({FAMILIES[row["config"]["model"]] for row in candidates}):
        promoted.append(min((i for i in ranked if FAMILIES[candidates[i]["config"]["model"]] == family), key=ranked.index))
    promoted.extend(i for i in ranked if i not in promoted)
    promoted = promoted[:max(config["search"]["promote"], len(config["ensemble"]["family_priors"]))]
    final = []
    for index in promoted:
        row = candidates[index]
        if FAMILIES[row["config"]["model"]] != "tree":
            final.append(row)
            continue
        previous, stable, accumulated = None, 0, []
        try:
            for milestone in config["seed_bagging"]["milestones"]:
                start = len(accumulated)
                predictions, hit = _oof(frame, y, groups, specs, row["config"], stream[start:milestone], config, worker, cache, split_seeds)
                cache_hits[0] += int(hit)
                accumulated.extend(predictions)
                averaged = np.asarray(accumulated).mean(axis=0)
                current = averaged.mean(axis=0)
                change = None if previous is None else float(np.mean(np.abs((current - previous) / y)) * 100)
                gain = None if previous is None else metrics(y, previous)["mape_percent"] - metrics(y, current)["mape_percent"]
                row["seed_history"].append({"count": milestone, "prediction_change_percent": change, "mape_gain_percentage_points": gain,
                                            "metrics": metrics(y, current)})
                stable = stable + 1 if change is not None and change <= config["seed_bagging"]["prediction_change_percent"] and abs(gain) <= config["seed_bagging"]["mape_gain_percentage_points"] else 0
                row.update(seeds=stream[:milestone], oof=current, oof_repeats=averaged)
                previous = current
                if stable >= config["seed_bagging"]["stable_rounds"]:
                    break
            row["seed_stop_reason"] = "stable" if stable >= config["seed_bagging"]["stable_rounds"] else "configured_cap"
            final.append(row)
        except BudgetExpired:
            raise
        except Exception as exc:
            failures.append({"config": row["config"], "reason": "晋级失败：" + str(exc)})
    if not final:
        raise RuntimeError("没有有效晋级候选。")
    final_bank = np.column_stack([row["oof"] for row in final])
    # Member count is explicit selection, not an ineffective L1 penalty.
    if len(final) > config["ensemble"]["max_members"]:
        ranking = _greedy(y, final_bank, config["ensemble"]["alpha"], config["ensemble"]["max_members"])
        keep = sorted(range(len(final)), key=lambda i: (-ranking[i], _loss(y, final_bank[:, i], config["ensemble"]["alpha"])))[:config["ensemble"]["max_members"]]
        # Positive mandatory family shares must retain an eligible candidate.
        required = {item["family"] for item in config["ensemble"]["family_priors"] if item.get("lower_bound", 0) > 0}
        for family in required:
            if not any(FAMILIES[final[i]["config"]["model"]] == family for i in keep):
                replacement = next(i for i in range(len(final)) if FAMILIES[final[i]["config"]["model"]] == family)
                removable = next((i for i in reversed(keep) if FAMILIES[final[i]["config"]["model"]] not in required), None)
                if removable is None:
                    raise ValueError("成员数上限与专家模型族硬界冲突。")
                keep.remove(removable); keep.append(replacement)
        final = [final[i] for i in keep]
        final_bank = final_bank[:, keep]
    fused = _fusion(y, final_bank, final, config)
    weights = np.asarray(fused["weights"])
    # Do not silently prune/renormalize a constrained solution: re-optimize or keep it.
    tiny = [i for i, weight in enumerate(weights) if 0 < weight < config["ensemble"]["minimum_weight"]]
    if tiny:
        try:
            fused = _fusion(y, final_bank, final, config, tiny)
            weights = np.asarray(fused["weights"])
        except (ValueError, FusionOptimizationError):
            pass
    prediction = final_bank.dot(weights)
    repeat_predictions = np.column_stack([np.column_stack([row["oof_repeats"][i] for row in final]).dot(weights) for i in range(len(split_seeds))])
    scores = [metrics(y, repeat_predictions[:, i]) for i in range(len(split_seeds))]
    cv = {"mean_mape": float(np.mean([s["mape_percent"] for s in scores])), "std_mape": float(np.std([s["mape_percent"] for s in scores])),
          "mean_r2": float(np.mean([s["r2"] for s in scores])), "std_r2": float(np.std([s["r2"] for s in scores]))}
    comparisons = {"selected": metrics(y, prediction), "best_single": min((metrics(y, final_bank[:, i]) for i in range(len(final))), key=lambda s: s["mape_percent"])}
    records = [dict((key, value) for key, value in row.items() if key not in ("oof", "oof_repeats")) for row in candidates]
    write_json(directory / "search_trials.json", {"valid_candidates": records, "failed_candidates": failures, "cache_hits": cache_hits[0]})
    write_json(directory / "weight_report.json", fused)
    np.savez_compressed(str(directory / "development_oof_bank.npz"), y=y, predictions=final_bank, groups=groups, weights=weights)
    return {"candidates": final, "weights": weights, "fusion": fused, "cv": cv, "oof": prediction,
            "comparisons": comparisons, "searched_models": sorted({row["config"]["model"] for row in candidates}),
            "completed_trials": len(candidates), "cache_hits": cache_hits[0], "failures": failures}


def _fit_bank(selection, train_frame, train_y, validation_frame, specs, worker):
    columns, fitted = [], []
    for row, weight in zip(selection["candidates"], selection["weights"]):
        if weight <= 0:
            columns.append(np.zeros(len(validation_frame))); fitted.append([])
            continue
        predictions, models = [], []
        for seed in row["seeds"]:
            prediction, model = worker.fit(row["config"], seed, specs, train_frame, train_y, validation_frame, True)
            predictions.append(prediction); models.append(model)
        columns.append(np.mean(predictions, axis=0)); fitted.append(models)
    return np.column_stack(columns), fitted


def train_price_v2(data, output_dir, config_path=None, progress=None, config_overrides=None):
    config = load_config(config_path, config_overrides)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "effective_config.json", config)
    progress = progress or (lambda message: print(message, flush=True))
    frame, y, specs, mappings, groups, ids = price_dataset(data, config["validation"]["minimum_groups"])
    keys = list(frame.columns)
    small = config["small_sample"]
    micro = small["policy"] == "force" or (small["policy"] == "auto" and len(np.unique(groups)) <= small["max_groups"])
    profile = {"active": micro, "policy": small["policy"], "independent_groups": len(np.unique(groups)),
               "raw_attributes": len(specs), "attributes_per_group": float(len(specs)) / len(np.unique(groups)),
               "selection": "bounded_micro_portfolio" if micro else "optuna_multistage",
               "validation": "nested_group_leave_one_out" if micro else "nested_repeated_group_folds",
               "supervised_feature_screening": not micro, "requested_validation": config["validation"],
               "settings": small}
    if micro:
        from tools.product_price_micro import inner_micro, validation_metrics
        selector = inner_micro
        progress("启用微样本稳健模式：%d组、%d属性；固定小组合与完整分组留一审核。" % (len(np.unique(groups)), len(specs)))
    else:
        selector = _inner
    write_json(output / "sample_profile.json", profile)
    if set(config["engineering"]["coefficients"]) - set(keys):
        raise ValueError("专家成本系数引用未知英文属性代号。")
    categorical_keys = {p["parameter_id"] for p in specs if p["value_type"] == "enum"}
    if set(config["engineering"]["coefficients"]) & categorical_keys:
        raise ValueError("工程成本不能把枚举代号的内部编码当作数值系数；请使用明确的类别成本特征。")
    for pair in config["pipeline"]["interaction_pairs"]:
        if len(pair) != 2 or any(key not in keys for key in pair):
            raise ValueError("交互特征引用未知属性。")
        if set(pair) & categorical_keys:
            raise ValueError("原始枚举编码不能直接用于数值交互。")
    if not np.all(np.isfinite([config["engineering"]["intercept_wan"]] + list(config["engineering"]["coefficients"].values()))):
        raise ValueError("专家成本参数须为有限数。")
    development, holdout = next(GroupShuffleSplit(n_splits=1, test_size=config["validation"]["holdout_fraction"],
                                                 random_state=config["validation"]["holdout_seed"]).split(frame, y, groups))
    dev_frame, dev_y, dev_groups = frame.iloc[development].reset_index(drop=True), y[development], groups[development]
    if np.var(dev_y) <= 1e-12:
        raise ValueError("预先划分后的开发集价格没有变化，无法优化或审核R²；请补充有价格变化的独立记录。")
    if len(holdout) < 2 or len(np.unique(dev_groups)) < 6:
        raise ValueError("分组后至少需2条保留记录和6组开发成品，以完成内外层验证。")
    cache = Path(config["resources"]["cache_directory"] or output / "oof_cache").resolve()
    root = Path(__file__).resolve().parents[1]
    if not (root / "outputs").resolve() in cache.parents and cache != (root / "outputs").resolve():
        raise ValueError("OOF缓存须位于本项目outputs目录内。")
    worker = FitWorker(config, time.monotonic() + config["resources"]["total_seconds"])
    audit = []
    started = time.monotonic()
    try:
        outer_folds = len(np.unique(dev_groups)) if micro else min(config["validation"]["outer_folds"], len(np.unique(dev_groups)) // 2)
        repetitions = 1 if micro else config["validation"]["outer_repeats"]
        outer_predictions = np.zeros((repetitions, len(dev_y)))
        for repetition in range(repetitions):
            partitions = LeaveOneGroupOut().split(dev_frame, dev_y, dev_groups) if micro else grouped_folds(dev_groups, outer_folds, 7919 + repetition * 1009)
            for fold, (training, validation) in enumerate(partitions):
                progress("V2外层审核：重复%d、折%d；正在重跑完整内部搜索。" % (repetition + 1, fold + 1))
                selected = selector(dev_frame.iloc[training].reset_index(drop=True), dev_y[training], dev_groups[training], specs,
                                  config, worker, cache, output / "audit" / ("repeat%d_fold%d" % (repetition, fold)), progress)
                bank, _ = _fit_bank(selected, dev_frame.iloc[training], dev_y[training], dev_frame.iloc[validation], specs, worker)
                prediction = bank.dot(selected["weights"])
                outer_predictions[repetition, validation] = prediction
                audit.append({"repeat": repetition, "fold": fold, "training_ids": [ids[development[i]] for i in training],
                              "validation_ids": [ids[development[i]] for i in validation],
                              "metrics": validation_metrics(dev_y[validation], prediction, single_group=True) if micro else metrics(dev_y[validation], prediction),
                              "minimum_inner_training_rows": len(training) - max(np.count_nonzero(dev_groups[training] == g) for g in np.unique(dev_groups[training])) if micro else None,
                              "weights": selected["weights"].tolist(), "models": [row["config"] for row in selected["candidates"]]})
        outer_scores = [metrics(dev_y, predicted) for predicted in outer_predictions]
        audit_cv = {"mean_mape": float(np.mean([s["mape_percent"] for s in outer_scores])), "std_mape": None if micro else float(np.std([s["mape_percent"] for s in outer_scores])),
                    "mean_r2": float(np.mean([s["r2"] for s in outer_scores])), "std_r2": None if micro else float(np.std([s["r2"] for s in outer_scores]))}
        profile.update(development_groups=len(np.unique(dev_groups)), outer_folds=outer_folds,
                       minimum_outer_training_rows=min(len(item["training_ids"]) for item in audit),
                       minimum_inner_training_rows=int(min(item["minimum_inner_training_rows"] for item in audit)) if micro else None)
        write_json(output / "sample_profile.json", profile)
        write_json(output / "nested_audit.json", {"completed": True, "rerun_full_selection": True, "folds": audit,
                   "pooled_outer_metrics": audit_cv, "validation_protocol": profile["validation"],
                   "r2_scope": "pooled predictions across all development records; single-record R2 undefined" if micro else "pooled by repeat"})
        progress("V2外层审核完成，正在开发集确定最终配置及专家融合权重。")
        selected = selector(dev_frame, dev_y, dev_groups, specs, config, worker, cache, output, progress)
        holdout_bank, _ = _fit_bank(selected, dev_frame, dev_y, frame.iloc[holdout], specs, worker)
        holdout_metrics = diagnostic_metrics(y[holdout], holdout_bank.dot(selected["weights"]))
        progress("V2选择已冻结，保留集评估完成；正在全量重训和导出加权模型。")
        final_bank, fitted = _fit_bank(selected, frame, y, frame.iloc[:min(4, len(frame))], specs, worker)
        if micro:
            from tools.product_price_micro import model_description
            profile["fitted_components"] = [dict(model_description(models[0], frame), label=row.get("label"), weight=float(weight))
                                             for row, weight, models in zip(selected["candidates"], selected["weights"], fitted) if models]
            write_json(output / "sample_profile.json", profile)
        variables, members, weights, ledger = {}, [], [], []
        for index, (row, weight, models) in enumerate(zip(selected["candidates"], selected["weights"], fitted)):
            if weight <= 0:
                continue
            for seed, model in zip(row["seeds"], models):
                name = "C%03d_S%d" % (index + 1, seed)
                variables[name] = model; members.append(name); weights.append(float(weight) / len(models))
                ledger.append({"name": name, "label": row.get("label", LABELS[row["config"]["model"]]), "configuration": index,
                               "family": FAMILIES[row["config"]["model"]], "seed": seed, "weight": float(weight) / len(models),
                               "configuration_weight": float(weight), "target_transform": row["config"].get("target_transform", "identity")})
        path = output / "price_native_bundle.pkl"
        product = data["products"][0]
        metadata = native_field_metadata(frame, specs, mappings)
        for field in metadata.values():
            if not field["required"]:
                field["missing_policy"] = "model_imputer"
        bundle = export_from_notebook(dict(variables, X_train=frame), output=path, product_code=product["product_code"],
                                     product_name=product["product_name"], model_version="prototype-automl-v2-micro" if micro else "prototype-automl-v2-expert",
                                     target_divisor_to_wan=1, field_metadata=metadata,
                                     model_variables=dict((name, name) for name in members), ensemble_model_names=members,
                                     ensemble_weights=weights, strict=True, model_source="namespace", model_output_transform="direct")
        bundle["automl_explanation"] = {"member_ledger": ledger, "expert_groups": selected["fusion"].get("expert_groups", []),
                                       "error_budget_limits": selected["fusion"].get("error_budget_limits"),
                                       "small_sample_profile": profile, "weight_stability": selected["fusion"].get("weight_stability"),
                                       "engineering": config["engineering"], "evaluation_scope": "development_oof_constraints_with_independent_nested_audit",
                                       "evidence_sufficient": len(np.unique(groups)) >= config["quality"]["minimum_independent_groups_for_evidence"],
                                       "field_labels": dict((p["parameter_id"], p["label"]) for p in specs),
                                       "sample_warning": "样本量不足，仅作流程与机制试验" if len(np.unique(groups)) < config["quality"]["minimum_independent_groups_for_evidence"] else "独立验证指标仍须按上线场景验收"}
        residuals = np.abs(dev_y - selected["oof"])
        bundle["residual_calibration"] = {"space": "native", "half_width_native": float(np.quantile(residuals, .95)), "coverage": None,
                                          "source": "development_oof_uncalibrated"}
        save_bundle(path, bundle)
        native = load_bundle(path)
        records_by_id = dict((record["agreement_id"], record) for record in data["agreements"])
        deltas = [abs(predict(native, records_by_id[ids[i]]["params"])["predicted_price_wan"] - float(final_bank[i].dot(selected["weights"])))
                  for i in range(min(4, len(frame)))]
        if max(deltas) > 1e-5:
            raise AssertionError("原生包与内存融合预测不一致。")
        q = config["quality"]
        def met(score):
            return score["mape_percent"] <= q["mape_percent_max"] and score["r2"] >= q["r2_min"]
        independent_groups = len(np.unique(groups))
        report = {"engine": "v2", "selected_model": "微样本稳健专家集成" if micro else "专家约束加权集成", "representative_seed": None,
                  "seed_selection": "fixed_micro_seed_average" if micro else "average_reproducible_stream_with_adaptive_budget",
                  "small_sample_profile": profile, "member_count": len(members), "member_ledger": ledger,
                  "selected_configurations": [row["config"] for row in selected["candidates"]], "weights": selected["weights"].tolist(),
                  "seed_report": [{"config": row["config"], "seeds": row["seeds"], "history": row["seed_history"],
                                   "stop_reason": row.get("seed_stop_reason", "deterministic_single_instance")} for row in selected["candidates"]],
                  "training_rows": len(frame), "feature_count": len(specs), "independent_groups": independent_groups,
                  "development_rows": len(development), "holdout_rows": len(holdout),
                  "development_ids": [ids[i] for i in development], "holdout_ids": [ids[i] for i in holdout],
                  "feature_order": keys, "cv_metrics": selected["cv"], "nested_cv_metrics": audit_cv, "holdout_metrics": holdout_metrics,
                  "nested_error_diagnostics": diagnostic_metrics(dev_y, np.mean(outer_predictions, axis=0)),
                  "targets": q, "cv_targets_met": met({"mape_percent": audit_cv["mean_mape"], "r2": audit_cv["mean_r2"]}),
                  "holdout_targets_met": met(holdout_metrics), "targets_met": met(holdout_metrics) and met({"mape_percent": audit_cv["mean_mape"], "r2": audit_cv["mean_r2"]}),
                  "business_accuracy_certified": False, "evidence_sufficient": independent_groups >= q["minimum_independent_groups_for_evidence"],
                  "holdout_evaluated_before_final_refit": True, "export_refitted_on_all_valid_rows": True,
                  "completed_trials": selected["completed_trials"], "searched_models": selected["searched_models"],
                  "failed_candidates": selected["failures"], "cache_hits": selected["cache_hits"],
                  "fusion": selected["fusion"], "comparison_development_only": selected["comparisons"], "native_equivalence_max_delta_wan": max(deltas),
                  "runtime": {"python": sys.version.split()[0], "sklearn": sklearn.__version__, "numpy": np.__version__, "scipy": scipy.__version__, "optuna": optuna.__version__},
                  "elapsed_seconds": round(time.monotonic() - started, 2), "warning": bundle["automl_explanation"]["sample_warning"]}
        manifest_path = Path(str(path) + ".manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update(bundle_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), automl_explanation=bundle["automl_explanation"],
                        residual_calibration=bundle["residual_calibration"])
        write_json(manifest_path, manifest)
        write_json(output / "training_report.json", report)
        write_json(output / "seed_report.json", report["seed_report"])
        write_json(output / "explanation.json", bundle["automl_explanation"])
        return bundle, report
    finally:
        worker.close()
