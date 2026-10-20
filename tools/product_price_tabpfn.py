# -*- coding: utf-8 -*-
"""CPU TabPFN candidates + engineering priors, scored on MAPE and R squared."""
from __future__ import print_function

import copy
import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import sklearn
from sklearn.compose import ColumnTransformer, TransformedTargetRegressor
from sklearn.impute import SimpleImputer
from sklearn.dummy import DummyRegressor
from sklearn.linear_model import Ridge, BayesianRidge
from sklearn.model_selection import GroupShuffleSplit, LeaveOneGroupOut
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from services.tabpfn_price_service.estimators import TabPFNContextRegressor, AttributePriorRidge, checkpoint_path
from services.price_service.ensemble_estimators import EngineeringPriceRegressor, EngineeringResidualRegressor, SmallSampleSelector
from services.price_service.export_native_price_bundle import export_from_notebook
from services.price_service.native_bundle import load_bundle, save_bundle, predict
from tools.product_price_automl import price_dataset, grouped_folds, native_field_metadata, metrics, write_json
from tools.product_price_automl_v2 import FitWorker, BudgetExpired, diagnostic_metrics
from tools.price_expert_fusion_probe import fit_expert_weights

DEFAULT = {
    "schema_version": "price-tabpfn-1.0", "checkpoint": None, "checkpoint_sha256": None,
    "resources": {"total_seconds": 900, "fit_timeout_seconds": 60, "threads": 2},
    "validation": {"minimum_groups": 8, "holdout_fraction": .2, "holdout_seed": 20261002,
                   "folds": 2, "repeats": 1, "split_seed": 311, "outer_folds": 2, "outer_repeats": 1,
                   "protocol": "auto", "micro_max_groups": 29},
    "tabpfn": {"n_estimators": 4, "softmax_temperature": .9, "fit_mode": "fit_preprocessors", "inference_config": None,
               "variants": [{"target_transform": "identity", "output_type": "mean"},
                            {"target_transform": "log", "output_type": "mean"},
                            {"target_transform": "identity", "output_type": "median"}]},
    "seed_bagging": {"base": 101, "milestones": [2, 4], "prediction_change_percent": .1,
                     "mape_gain_percentage_points": .05, "stable_rounds": 1, "micro_milestones": [2]},
    "baselines": {"ridge_alphas": [1.0, 100.0], "select_k": ["all", 6], "bayesian_ridge": True,
                  "engineering_residual": True, "target_transforms": ["identity", "log"], "positive_median": True},
    "resilience": {"require_tabpfn": False},
    "engineering": {"intercept_wan": 0.0, "coefficients": {}},
    "ensemble": {"mode": "data_only", "alpha": .7, "prior_strength": .01,
                 "max_mape_delta_pp": .1, "max_r2_drop": .005, "minimum_weight": 1e-8,
                 "family_priors": []},
    "expert": {"scores": {}, "example_scores": {}, "records": [], "field_types": {}, "attribute_groups": {}, "group_strengths": {},
               "from_workbook": True, "core_threshold": 4., "strengths": [0., .5, 1.]},
    "quality": {"mape_percent_max": 10.0, "r2_min": .85, "minimum_independent_groups_for_evidence": 30, "hard_gate": False}}


def load_config(path=None, overrides=None):
    supplied = json.loads(Path(path).read_text(encoding="utf-8-sig")) if path else {}
    from tools.price_attribute_input import migrate_bundled_example
    supplied = migrate_bundled_example(path, supplied)
    config = copy.deepcopy(DEFAULT)
    def merge(target, source, location=""):
        if not isinstance(source, dict):
            raise ValueError("配置须为对象：" + location)
        for key, value in source.items():
            if key not in target:
                raise ValueError("未知TabPFN设置：" + location + key)
            if isinstance(target[key], dict) and key not in ("coefficients", "inference_config", "scores", "example_scores", "field_types", "attribute_groups", "group_strengths"):
                merge(target[key], value, location + key + ".")
            else:
                target[key] = value
    merge(config, supplied)
    if overrides:
        merge(config, overrides)
    if config["schema_version"] != DEFAULT["schema_version"]:
        raise ValueError("TabPFN配置版本无效。")
    v, s, t, b = (config[key] for key in ("validation", "seed_bagging", "tabpfn", "baselines"))
    if v["minimum_groups"] < 8 or not 0 < v["holdout_fraction"] < .5 or min(v["folds"], v["outer_folds"]) < 2 or min(v["repeats"], v["outer_repeats"]) < 1:
        raise ValueError("TabPFN验证至少8组、2折，保留比例须介于0和0.5之间。")
    if v["protocol"] not in ("auto", "group_loo", "holdout_nested") or not 8 <= v["micro_max_groups"] <= 100:
        raise ValueError("验证策略须为auto/group_loo/holdout_nested，微样本组数上限须在8—100。")
    if not isinstance(config["quality"]["hard_gate"], bool):
        raise ValueError("质量硬门槛须为布尔值。")
    if s["milestones"] != sorted(set(s["milestones"])) or not s["milestones"] or min(s["milestones"]) < 1 or max(s["milestones"]) > 16:
        raise ValueError("TabPFN种子档位须递增，范围1至16。")
    if not s["micro_milestones"] or s["micro_milestones"] != sorted(set(s["micro_milestones"])) or min(s["micro_milestones"]) < 2 or max(s["micro_milestones"]) > 16:
        raise ValueError("微样本种子档位须递增，范围2至16。")
    if min(config["resources"].values()) <= 0 or t["n_estimators"] < 1 or t["fit_mode"] not in ("low_memory", "fit_preprocessors", "fit_with_cache"):
        raise ValueError("TabPFN资源/推理设置无效。")
    if not t["variants"] or any(set(row) != {"target_transform", "output_type"} or row["target_transform"] not in ("identity", "log") or row["output_type"] not in ("mean", "median") for row in t["variants"]):
        raise ValueError("TabPFN候选只支持原价/对数目标和均值/中位数输出。")
    if len({json.dumps(row, sort_keys=True) for row in t["variants"]}) != len(t["variants"]):
        raise ValueError("TabPFN候选不能重复。")
    if any(float(alpha) <= 0 for alpha in b["ridge_alphas"]) or any(k != "all" and (not isinstance(k, int) or k < 1) for k in b["select_k"]):
        raise ValueError("基线参数无效。")
    families = config["ensemble"]["family_priors"]
    if any(row["family"] not in ("engineering", "linear", "tabpfn") for row in families) or len({row["family"] for row in families}) != len(families):
        raise ValueError("TabPFN模型族设置无效。")
    if config["ensemble"]["mode"] not in ("data_only", "soft_prior", "performance_budget"):
        raise ValueError("TabPFN融合模式无效。")
    if not isinstance(config["resilience"]["require_tabpfn"], bool) or not isinstance(b["positive_median"], bool):
        raise ValueError("候选容错与正价格基线开关须为布尔值。")
    if not b["target_transforms"] or any(value not in ("identity", "log") for value in b["target_transforms"]) or len(set(b["target_transforms"])) != len(b["target_transforms"]):
        raise ValueError("线性基线目标变换须为不重复的identity/log数组。")
    e = config["expert"]
    if not isinstance(e["from_workbook"], bool) or not 1 <= e["core_threshold"] <= 5:
        raise ValueError("属性评分读取开关或专家核心阈值无效。")
    for source in ("scores", "example_scores"):
        if not isinstance(e[source], dict):
            raise ValueError("expert.%s须为英文代号到1—5分的对象。" % source)
        for key, value in e[source].items():
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not np.isfinite(value) or not 1 <= value <= 5:
                raise ValueError("价格属性%s评分须为1—5分。" % key)
    if not e["strengths"] or any(not np.isfinite(value) or not 0 <= value <= 2 for value in e["strengths"]):
        raise ValueError("价格属性先验强度须在0—2之间。")
    if e["strengths"] != sorted(set(e["strengths"])) or e["strengths"][0] != 0:
        raise ValueError("价格属性先验强度须递增并包含0，保留无先验对照。")
    if any(not np.isfinite(value) or not 0 <= value <= 2 for value in e["group_strengths"].values()):
        raise ValueError("价格属性组先验强度须在0—2之间。")
    return config


def factory(candidate, seed, specs, config):
    keys = [p["parameter_id"] for p in specs]
    if candidate["family"] == "tabpfn":
        settings = config["tabpfn"]
        categorical = [i for i, spec in enumerate(specs) if spec["value_type"] in ("enum", "boolean")]
        subset = candidate.get("feature_subset")
        indices = [keys.index(key) for key in subset] if subset else None
        return TabPFNContextRegressor(n_estimators=settings["n_estimators"], random_state=int(seed),
            target_transform=candidate["target_transform"], output_type=candidate["output_type"], categorical_indices=categorical,
            checkpoint=config["checkpoint"], checkpoint_sha256=config["checkpoint_sha256"], threads=config["resources"]["threads"],
            softmax_temperature=settings["softmax_temperature"], fit_mode=settings["fit_mode"], inference_config=settings["inference_config"], feature_indices=indices)
    if candidate["model"] == "positive_median":
        return DummyRegressor(strategy="median")
    if candidate["model"] == "attribute_ridge":
        expert = config["expert"]
        scores = [float(expert["scores"].get(key, 3.)) for key in keys]
        field_strengths = {key: expert["group_strengths"].get(expert["attribute_groups"].get(key), 1.) for key in keys}
        return AttributePriorRidge(specs, scores, {"model": "ridge", "parameters": {"alpha": candidate["alpha"]},
            "expert_strength": candidate["expert_strength"], "view": candidate["view"], "core_threshold": expert["core_threshold"],
            "imputation": "median", "scaler": "standard", "target_transform": "identity", "field_strengths": field_strengths}, int(seed))
    core = EngineeringPriceRegressor(keys, config["engineering"]["coefficients"], config["engineering"]["intercept_wan"])
    if candidate["model"] == "engineering_cost":
        return core
    numeric = [i for i, p in enumerate(specs) if p["value_type"] != "enum"]
    categorical = [i for i, p in enumerate(specs) if p["value_type"] == "enum"]
    transforms = [("numeric", Pipeline([("impute", SimpleImputer(strategy="median")), ("scale", StandardScaler())]), numeric)] if numeric else []
    if categorical:
        transforms.append(("enum", Pipeline([("impute", SimpleImputer(strategy="most_frequent")),
                                              ("encode", OneHotEncoder(handle_unknown="ignore", sparse_output=False))]), categorical))
    model = BayesianRidge() if candidate["model"] == "bayesian_ridge" else Ridge(alpha=candidate["alpha"])
    pipeline = Pipeline([("prepare", ColumnTransformer(transforms)), ("select", SmallSampleSelector(candidate.get("select_k", "all"))), ("model", model)])
    if candidate["model"] == "engineering_residual":
        return EngineeringResidualRegressor(core, pipeline)
    return TransformedTargetRegressor(regressor=pipeline, func=np.log, inverse_func=np.exp) if candidate.get("target_transform") == "log" else pipeline


def candidates(config, specs=None):
    result = [{"model": "tabpfn", "family": "tabpfn", "label": "TabPFN（%s，%s）" % ("对数价格" if row["target_transform"] == "log" else "原价", "中位数" if row["output_type"] == "median" else "均值"), **row}
              for row in config["tabpfn"]["variants"]]
    expert = config["expert"]
    if expert["scores"]:
        keys = [spec["parameter_id"] for spec in specs] if specs else list(expert["scores"])
        core = [key for key in keys if expert["scores"].get(key, 3.) >= expert["core_threshold"]]
        if core and len(core) < len(keys):
            result.extend(dict(candidate, label=candidate["label"] + "，专家价格属性子集", feature_subset=core)
                          for candidate in list(result))
        for alpha in config["baselines"]["ridge_alphas"]:
            for strength in expert["strengths"]:
                for view in (["all", "expert_core"] if core else ["all"]):
                    result.append({"model": "attribute_ridge", "family": "linear", "label": "价格属性先验岭回归（强度%g，%s）" % (strength, "全部属性" if view == "all" else "专家子集"),
                                   "alpha": alpha, "expert_strength": strength, "view": view})
    for transform in config["baselines"]["target_transforms"]:
        suffix = "（对数价格，保证反变换为正）" if transform == "log" else ""
        for alpha in config["baselines"]["ridge_alphas"]:
            for count in config["baselines"]["select_k"]:
                result.append({"model": "ridge", "family": "linear", "label": "岭回归" + suffix, "alpha": alpha, "select_k": count, "target_transform": transform})
        if config["baselines"]["bayesian_ridge"]:
            result.append({"model": "bayesian_ridge", "family": "linear", "label": "贝叶斯岭回归" + suffix, "target_transform": transform})
    if config["baselines"]["positive_median"]:
        result.append({"model": "positive_median", "family": "baseline", "label": "正价格中位数基线"})
    if config["engineering"]["coefficients"]:
        result.append({"model": "engineering_cost", "family": "engineering", "label": "专家工程成本"})
        if config["baselines"]["engineering_residual"]:
            result.append({"model": "engineering_residual", "family": "engineering", "label": "工程成本与残差修正", "alpha": 10.0})
    return result


def _oof(frame, y, groups, specs, candidate, seeds, config, worker, cache):
    signature = {"candidate": candidate, "seeds": seeds, "config": config, "specs": specs,
                 "versions": [sklearn.__version__, np.__version__],
                 "code": hashlib.sha256(Path(__file__).read_bytes() + Path(sys.modules[TabPFNContextRegressor.__module__].__file__).read_bytes() +
                                        Path(sys.modules[AttributePriorRidge.__mro__[1].__module__].__file__).read_bytes()).hexdigest()}
    identity = hashlib.sha256(frame.values.tobytes() + y.tobytes() + groups.tobytes() + json.dumps(signature, sort_keys=True).encode()).hexdigest()
    path = cache / (identity + ".npz")
    repeats = config["validation"]["repeats"]
    if path.exists():
        with np.load(path, allow_pickle=False) as saved:
            bank = saved["predictions"]
        if bank.shape == (len(seeds), repeats, len(y)) and np.all(np.isfinite(bank)) and np.all(bank > 0):
            return bank, True
    bank = np.zeros((len(seeds), repeats, len(y)))
    folds = min(config["validation"]["folds"], len(np.unique(groups)) // 2)
    if folds < 2:
        raise ValueError("内层验证至少需要4组独立记录。")
    for repeat in range(repeats):
        partitions = LeaveOneGroupOut().split(frame, y, groups) if config["validation"]["protocol"] == "group_loo" else grouped_folds(groups, folds, config["validation"]["split_seed"] + repeat * 1009)
        for training, validation in partitions:
            if set(groups[training]) & set(groups[validation]):
                raise AssertionError("相同属性配置跨越验证边界。")
            for index, seed in enumerate(seeds):
                from tools.price_candidate_recovery import positive_predictions
                bank[index, repeat, validation] = positive_predictions(worker.fit(candidate, seed, specs, frame.iloc[training], y[training], frame.iloc[validation])[0],len(validation))
    cache.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, predictions=bank)
    return bank, False


def _select(frame, y, groups, specs, config, worker, cache, directory, progress):
    micro = config["validation"]["protocol"] == "group_loo"
    seed_cap = max(config["seed_bagging"]["milestones"] + config["seed_bagging"]["micro_milestones"])
    stream = [int(x) for x in np.random.SeedSequence(config["seed_bagging"]["base"]).generate_state(seed_cap)]
    from tools.price_candidate_recovery import blend_candidates, failure_event
    good, failures, cache_hits = [], [], 0
    directory.mkdir(parents=True, exist_ok=True)
    for candidate in candidates(config, specs):
        history, prior, stable, accumulated = [], None, 0, []
        validated = None
        milestones = config["seed_bagging"]["milestones"] if candidate["family"] == "tabpfn" and not micro else [1]
        try:
            for count in milestones:
                values, hit = _oof(frame, y, groups, specs, candidate, stream[len(accumulated):count], config, worker, cache)
                cache_hits += int(hit)
                accumulated.extend(values)
                repeat_bank = np.mean(accumulated, axis=0)
                current = repeat_bank.mean(axis=0)
                change = None if prior is None else float(np.mean(np.abs((current - prior) / y)) * 100)
                gain = None if prior is None else metrics(y, prior)["mape_percent"] - metrics(y, current)["mape_percent"]
                history.append({"count": count, "prediction_change_percent": change, "mape_gain_percentage_points": gain, "metrics": metrics(y, current)})
                validated = {"config": candidate, "seeds": stream[:count], "seed_history": history, "oof": current, "repeats": repeat_bank, "metrics": metrics(y, current)}
                settings = config["seed_bagging"]
                stable = stable + 1 if change is not None and change <= settings["prediction_change_percent"] and abs(gain) <= settings["mape_gain_percentage_points"] else 0
                prior = current
                if stable >= settings["stable_rounds"]:
                    break
            good.append({"config": candidate, "seeds": stream[:count], "seed_history": history, "oof": current, "repeats": repeat_bank,
                         "metrics": metrics(y, current), "seed_stop_reason": "stable" if stable >= config["seed_bagging"]["stable_rounds"] else "configured_cap"})
            progress("候选完成：" + candidate["label"])
        except BudgetExpired:
            raise
        except Exception as exc:
            if validated is not None:
                validated["seed_stop_reason"] = "expansion_failed_kept_validated_prefix"
                good.append(validated)
                event = failure_event(candidate, exc, "seed_expansion", action="retain_validated_seed_prefix")
                event["retained_seeds"] = list(validated["seeds"])
                failures.append(event)
                progress("扩种失败，保留此前完整验证通过的%d个种子：%s；%s" % (len(validated["seeds"]), candidate["label"], exc))
            else:
                failures.append(failure_event(candidate, exc, "oof_screening"))
                progress("淘汰失败候选，继续搜索：%s；%s" % (candidate["label"], exc))
            write_json(directory / "candidate_failures.json", failures)
    if micro:
        alpha = config["ensemble"]["alpha"]
        tabs = [row for row in good if row["config"]["family"] == "tabpfn"]
        best = min(tabs, key=lambda row: alpha * row["metrics"]["mape_percent"] / 100 + (1-alpha) * (1-row["metrics"]["r2"])) if tabs else None
        if best is not None:
            for count in config["seed_bagging"]["micro_milestones"]:
                try:
                    values, hit = _oof(frame, y, groups, specs, best["config"], stream[:count], config, worker, cache)
                    cache_hits += int(hit)
                    repeats = values.mean(axis=0); averaged = repeats.mean(axis=0)
                    change = float(np.mean(np.abs((averaged-best["oof"])/y))*100)
                    gain = best["metrics"]["mape_percent"]-metrics(y,averaged)["mape_percent"]
                    best["seed_history"].append({"count":count,"prediction_change_percent":change,"mape_gain_percentage_points":gain,"metrics":metrics(y,averaged)})
                    best.update(seeds=stream[:count],oof=averaged,repeats=repeats,metrics=metrics(y,averaged),seed_stop_reason="configured_micro_cap")
                except BudgetExpired:
                    raise
                except Exception as exc:
                    event=failure_event(best["config"],exc,"seed_expansion",action="retain_validated_seed_prefix")
                    event["retained_seeds"]=list(best["seeds"]);failures.append(event)
                    best["seed_stop_reason"]="expansion_failed_kept_validated_prefix"
                    write_json(directory/"candidate_failures.json",failures)
                    progress("扩展种子失败，保留此前完整验证通过的%d个种子并继续：%s；%s" % (len(best["seeds"]),best["config"]["label"],exc))
                    break
        for row in good:
            if row["config"]["family"]=="tabpfn" and row is not best:
                row["seed_stop_reason"]="screening_only"
    selected=blend_candidates(good,y,config,failures,cache_hits,directory)
    selected["_progress"]=progress
    if selected["best_tabpfn"] is None:
        progress("TabPFN候选不可用，继续使用验证有效的其他价格模型；不生成纯TabPFN对比模型。")
    return selected


def _fit_bank(selection, frame, y, validation, specs, worker, indices=None, return_models=False, stage="refit"):
    from tools.price_candidate_recovery import fit_selection_bank
    return fit_selection_bank(selection,frame,y,validation,specs,worker,return_models,stage)


def train_price(data, output_dir, config_path=None, overrides=None, progress=None, input_path=None):
    from tools.check_tabpfn_environment import validate_training_runtime
    validate_training_runtime()
    progress = progress or (lambda message: print(message, flush=True))
    config = load_config(config_path, overrides)
    from tools.product_r4_input import ratings_from_workbook, apply_field_types
    known = {p["parameter_id"] for p in data["parameters"]}
    scores, records, types, groups = {}, [], {}, {}
    if input_path and config["expert"]["from_workbook"]:
        scores, records, types, groups = ratings_from_workbook(input_path, known)
    from tools.price_attribute_input import resolve_price_expert, price_attribute_audit
    config["expert"], input_resolution = resolve_price_expert(config["expert"], data["parameters"], scores, records, types, groups)
    for item in input_resolution["ignored_example_scores"]:
        progress("示例价格评分%s不在当前表中，未作为实际专家意见使用。" % item["field_id"])
    load_config(overrides=config)
    apply_field_types(data, config["expert"]["field_types"])
    config["checkpoint"] = str(checkpoint_path(config["checkpoint"]))
    actual_hash = hashlib.sha256(Path(config["checkpoint"]).read_bytes()).hexdigest()
    if config["checkpoint_sha256"] and actual_hash != config["checkpoint_sha256"]:
        raise ValueError("训练权重摘要不一致。")
    config["checkpoint_sha256"] = actual_hash
    output = Path(output_dir); output.mkdir(parents=True, exist_ok=True)
    write_json(output / "effective_config.json", config)
    frame, y, specs, mappings, groups, ids = price_dataset(data, config["validation"]["minimum_groups"])
    keys = list(frame.columns)
    ratings = price_attribute_audit(data["parameters"], keys, config["expert"], input_resolution)
    excluded = [row for row in ratings if not row["used_in_training"]]
    input_resolution.update({"declared_attribute_count": len(data["parameters"]), "training_attribute_count": len(keys),
                             "excluded_attributes": excluded, "status": "PASS"})
    for row in excluded:
        progress("价格属性%s（%s）代号有效，本次未参与训练：%s。" % (row["label"], row["field_id"], row["exclusion_reason"]))
    write_json(output / "input_validation.json", input_resolution)
    write_json(output / "price_attribute_scores.json", {"attributes": ratings, "records": config["expert"]["records"],
               "scope": "price_input_attributes", "input_validation": input_resolution})
    if set(config["engineering"]["coefficients"]) - set(keys):
        raise ValueError("工程成本先验含未知英文属性代号。")
    if any(spec["parameter_id"] in config["engineering"]["coefficients"] and spec["value_type"] == "enum" for spec in specs):
        raise ValueError("无序枚举不能直接用作数值成本系数。")
    v = config["validation"]
    if v["protocol"] == "auto":
        v["protocol"] = "group_loo" if len(np.unique(groups)) <= v["micro_max_groups"] else "holdout_nested"
    micro = v["protocol"] == "group_loo"
    if micro:
        development, holdout = np.arange(len(frame)), np.asarray([], dtype=int)
        v["repeats"] = 1; v["outer_repeats"] = 1
    else:
        development, holdout = next(GroupShuffleSplit(n_splits=1, test_size=v["holdout_fraction"], random_state=v["holdout_seed"]).split(frame, y, groups))
    write_json(output / "effective_config.json", config)
    if len(np.unique(groups[development])) < 6 or (not micro and len(np.unique(groups[holdout])) < 2):
        raise ValueError("至少6组开发记录与2组最终保留记录。")
    started = time.monotonic()
    worker = FitWorker(config, started + config["resources"]["total_seconds"], factory)
    cache = output / "oof_cache"
    dev_frame, dev_y, dev_groups = frame.iloc[development], y[development], groups[development]
    audit, outer_scores, outer_tab_scores = [], [], []
    outer_predictions = np.zeros((v["outer_repeats"], len(dev_y)))
    outer_tab_predictions = np.full_like(outer_predictions, np.nan)
    try:
        for repeat in range(v["outer_repeats"]):
            folds = min(v["outer_folds"], len(np.unique(dev_groups)) // 2)
            partitions = LeaveOneGroupOut().split(dev_frame, dev_y, dev_groups) if micro else grouped_folds(dev_groups, folds, v["split_seed"] + 7907 + repeat * 1009)
            for index, (training, validation) in enumerate(partitions):
                selected = _select(dev_frame.iloc[training], dev_y[training], dev_groups[training], specs, config, worker, cache,
                                   output / "outer" / ("%d_%d" % (repeat, index)), progress)
                bank, _ = _fit_bank(selected, dev_frame.iloc[training], dev_y[training], dev_frame.iloc[validation], specs, worker, stage="outer_refit")
                predicted = bank.dot(selected["weights"])
                tab_predicted = bank[:, selected["best_tabpfn"]] if selected["best_tabpfn"] is not None else None
                outer_predictions[repeat, validation] = predicted
                if tab_predicted is not None:
                    outer_tab_predictions[repeat, validation] = tab_predicted
                from tools.product_price_automl_r4 import score as pooled_score
                score = pooled_score(dev_y[validation], predicted)
                tab_score = pooled_score(dev_y[validation], tab_predicted) if tab_predicted is not None else {"mape_percent": None, "r2": None, "status": "unavailable"}
                outer_scores.append(score); outer_tab_scores.append(tab_score)
                audit.append({"repeat": repeat, "fold": index, "training_ids": [ids[development[i]] for i in training],
                              "validation_ids": [ids[development[i]] for i in validation], "metrics": score, "tabpfn_only_metrics": tab_score,
                              "failed_candidate_attempts": selected["failures"], "refit_recoveries": selected.get("refit_recoveries", []),
                              "minimum_inner_training_groups": len(np.unique(dev_groups[training])) - 1 if micro else None})
        if micro:
            outer_scores = [metrics(dev_y, prediction) for prediction in outer_predictions]
            outer_tab_scores = [metrics(dev_y, prediction) for prediction in outer_tab_predictions] if np.all(np.isfinite(outer_tab_predictions)) else []
        write_json(output / "nested_audit.json", {"completed": True, "rerun_full_selection": True, "folds": audit,
            "protocol": v["protocol"], "r2_scope": "pooled_outer_predictions" if micro else "folds_with_multiple_records"})
        write_json(output / "outer_predictions.json", {"ids": [ids[i] for i in development], "targets_wan": dev_y.tolist(),
            "fused_predictions_wan": outer_predictions.mean(axis=0).tolist(), "tabpfn_predictions_wan": [float(np.mean(column)) if np.all(np.isfinite(column)) else None for column in outer_tab_predictions.T]})
        selected = _select(dev_frame, dev_y, dev_groups, specs, config, worker, cache, output / "development", progress)
        holdout_score = {"mape_percent": None, "r2": None, "status": "not_provided"}
        tab_holdout = dict(holdout_score)
        if not micro:
            bank, _ = _fit_bank(selected, dev_frame, dev_y, frame.iloc[holdout], specs, worker, stage="holdout_refit")
            holdout_score = diagnostic_metrics(y[holdout], bank.dot(selected["weights"]))
            tab_holdout = diagnostic_metrics(y[holdout], bank[:, selected["best_tabpfn"]]) if selected["best_tabpfn"] is not None else {"mape_percent": None, "r2": None, "status": "unavailable"}
            write_json(output / "holdout_predictions.json", {"record_ids": [ids[i] for i in holdout], "targets_wan": y[holdout].tolist(),
                "fused_predictions_wan": bank.dot(selected["weights"]).tolist(), "tabpfn_predictions_wan": bank[:, selected["best_tabpfn"]].tolist() if selected["best_tabpfn"] is not None else None,
                "audited_weights_before_final_refit": selected["weights"].tolist(), "audited_configs": [row["config"] for row in selected["candidates"]]})
        final_bank, fitted = _fit_bank(selected, frame, y, frame, specs, worker, return_models=True, stage="final_refit")
        tab_best = selected["best_tabpfn"]
        if tab_best is None:
            progress("纯TabPFN对比模型不可用；有效价格融合模型继续导出，报告明确标记降级。")
        field_metadata = native_field_metadata(frame, specs, mappings)
        for spec in specs:
            item = field_metadata[spec["parameter_id"]]
            item.update(generation_min=spec.get("min_value"), generation_max=spec.get("max_value"),
                        missing_policy="reject" if item["required"] else "model_imputer")
        ledger, variables, names, weights = [], {}, [], []
        tab_ledger, tab_variables, tab_names = [], {}, []
        for index, (row, weight, models) in enumerate(zip(selected["candidates"], selected["weights"], fitted)):
            for seed, model in zip(row["seeds"], models):
                name = "C%03d_S%d" % (index + 1, seed)
                entry = {"name": name, "label": row["config"]["label"], "configuration": index, "family": row["config"]["family"],
                         "seed": seed, "weight": float(weight) / len(models), "configuration_weight": float(weight)}
                if weight > 0:
                    variables[name] = model; names.append(name); weights.append(entry["weight"]); ledger.append(entry)
                if index == selected["best_tabpfn"]:
                    tab_variables[name] = model; tab_names.append(name); tab_ledger.append(dict(entry, weight=1.0 / len(models), configuration_weight=1.0))
        def export(path, models, member_names, member_weights, member_ledger, expert_groups, development_prediction):
            bundle = export_from_notebook(dict(models, X_train=frame), path, product_code=data["products"][0]["product_code"],
                product_name=data["products"][0]["product_name"], model_version="tabpfn-price-prototype-1.0",
                model_variables=dict((name, name) for name in member_names), model_source="namespace", strict=True,
                ensemble_model_names=member_names, ensemble_weights=member_weights, model_output_transform="direct",
                field_metadata=field_metadata, target_divisor_to_wan=1.0)
            bundle["automl_explanation"] = {"member_ledger": member_ledger, "expert_groups": expert_groups,
                "field_labels": dict((p["parameter_id"], p["label"]) for p in specs),
                "evidence_sufficient": len(np.unique(groups)) >= config["quality"]["minimum_independent_groups_for_evidence"],
                "sample_warning": "样本量不足，仅作流程与机制试验" if len(np.unique(groups)) < config["quality"]["minimum_independent_groups_for_evidence"] else "须按真实验收场景验证",
                "method": "价格AutoML候选、属性先验与MAPE/R²验证融合",
                "tabpfn_available": tab_best is not None, "candidate_failure_count": len(selected["failures"]), "execution_requirement": "Win11 Python3.11服务"}
            bundle["automl_explanation"].update(price_attribute_scores=ratings, expert_records=config["expert"]["records"],
                method="价格输入属性1—5分先验；TabPFN原始/专家子集与属性加权岭回归比较；融合份额由验证学习")
            bundle["residual_calibration"] = {"space": "native", "half_width_native": float(np.quantile(np.abs(dev_y - development_prediction), .95)),
                                              "coverage": None, "source": "development_oof_uncalibrated"}
            save_bundle(path, bundle)
            sidecar = Path(str(path) + ".manifest.json")
            manifest = json.loads(sidecar.read_text(encoding="utf-8"))
            manifest.update(bundle_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), runtime_requirement="Python3.11/sklearn1.5.2/TabPFN2.0.9")
            write_json(sidecar, manifest)
            return bundle
        path = output / "price_native_bundle.pkl"
        bundle = export(path, variables, names, weights, ledger, selected["fusion"].get("expert_groups", []), selected["oof"])
        if tab_names:
            export(output / "tabpfn_only_bundle.pkl", tab_variables, tab_names, [1.0 / len(tab_names)] * len(tab_names), tab_ledger, [],
                   selected["candidates"][tab_best]["oof"])
        write_json(output / "tabpfn_only_status.json", {"status": "available" if tab_names else "unavailable",
                   "artifact": "tabpfn_only_bundle.pkl" if tab_names else None, "reason": None if tab_names else "没有通过筛选和重拟合的TabPFN候选；主价格模型使用其他有效候选"})
        native = load_bundle(path)
        by_id = dict((row["agreement_id"], row) for row in data["agreements"])
        deltas = [abs(predict(native, by_id[ids[i]]["params"])["predicted_price_wan"] - final_bank[i].dot(selected["weights"])) for i in range(len(frame))]
        if max(deltas) > 1e-5:
            raise AssertionError("导出服务模型与内存融合预测不一致。")
        def aggregate(scores):
            if not scores or any(row.get("mape_percent") is None or row.get("r2") is None for row in scores):
                return {"mean_mape": None, "std_mape": None, "mean_r2": None, "std_r2": None, "status": "unavailable"}
            return {"mean_mape": float(np.mean([x["mape_percent"] for x in scores])), "std_mape": None if len(scores) == 1 else float(np.std([x["mape_percent"] for x in scores])),
                    "mean_r2": float(np.mean([x["r2"] for x in scores])), "std_r2": None if len(scores) == 1 else float(np.std([x["r2"] for x in scores]))}
        q, outer = config["quality"], aggregate(outer_scores)
        met = lambda s: s["mape_percent"] <= q["mape_percent_max"] and s["r2"] >= q["r2_min"]
        report = {"engine": "tabpfn", "selected_model": "价格属性评分引导的验证择优预测", "representative_seed": None,
                  "member_count": len(names), "training_rows": len(frame), "feature_count": len(specs), "development_rows": len(development), "holdout_rows": len(holdout),
                  "development_ids": [ids[i] for i in development], "holdout_ids": [ids[i] for i in holdout],
                  "cv_metrics": selected["cv"], "nested_cv_metrics": outer, "holdout_metrics": holdout_score, "validation_protocol": v["protocol"],
                  "minimum_inner_training_groups": min(row["minimum_inner_training_groups"] for row in audit) if micro else None,
                  "tabpfn_only": {"status": "available" if tab_best is not None else "unavailable",
                                  "selected_config": selected["candidates"][tab_best]["config"] if tab_best is not None else None,
                                  "development_oof": selected["candidates"][tab_best]["metrics"] if tab_best is not None else None,
                                  "nested_cv_metrics": aggregate(outer_tab_scores), "holdout_metrics": tab_holdout},
                  "fusion": selected["fusion"], "weights": selected["weights"].tolist(), "member_ledger": ledger,
                  "seed_report": [{k: row[k] for k in ("config", "seeds", "seed_history", "seed_stop_reason")} for row in selected["candidates"]],
                  "failed_candidates": selected["failures"], "refit_recoveries": selected.get("refit_recoveries", []),
                  "failure_policy": "isolate_candidate_retain_validated_seeds_reblend_from_training_oof", "completed_trials": len(selected["candidates"]), "cache_hits": selected["cache_hits"],
                  "price_attribute_scores": ratings, "price_input_validation": input_resolution, "expert_scores_scope": "price_input_attributes",
                  "targets": q, "targets_met": (micro or met(holdout_score)) and met({"mape_percent": outer["mean_mape"], "r2": outer["mean_r2"]}),
                  "business_accuracy_certified": False, "evidence_sufficient": bundle["automl_explanation"]["evidence_sufficient"],
                  "holdout_evaluated_before_final_refit": not micro, "export_refitted_on_all_valid_rows": True,
                  "native_equivalence_max_delta_wan": max(deltas), "runtime": {"python": sys.version.split()[0], "sklearn": sklearn.__version__, "tabpfn": "2.0.9", "device": "cpu"},
                  "elapsed_seconds": round(time.monotonic() - started, 2), "warning": bundle["automl_explanation"]["sample_warning"]}
        write_json(output / "training_report.json", report)
        write_json(output / "weight_report.json", selected["fusion"])
        write_json(output / "seed_report.json", report["seed_report"])
        if q["hard_gate"] and not report["targets_met"]:
            raise ValueError("价格质量门槛未同时达到：已保留失败报告与模型诊断，禁止发布或自动激活。")
        return bundle, report
    finally:
        worker.close()
