# -*- coding: utf-8 -*-
"""A bounded portfolio and complete nested group-LOO for very small datasets."""
from __future__ import print_function

import copy
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
from sklearn.model_selection import LeaveOneGroupOut

from tools.product_price_automl import metrics, write_json
from tools.product_price_automl_v2 import LABELS, FAMILIES, BudgetExpired, _fusion, _loss
from tools.price_expert_fusion_probe import FusionOptimizationError
from services.price_service.ensemble_estimators import EngineeringPriceRegressor


def validation_metrics(y, prediction, single_group=False):
    y, prediction = np.asarray(y), np.asarray(prediction)
    result = {"mape_percent": float(np.mean(np.abs((y - prediction) / y)) * 100),
              "mae_wan": float(np.mean(np.abs(y - prediction))), "r2": None,
              "r2_defined": bool(not single_group and len(y) >= 2 and np.var(y) > 1e-12)}
    if result["r2_defined"]:
        result["r2"] = metrics(y, prediction)["r2"]
    return result


def portfolio(specs, config):
    settings = config["small_sample"]
    allowed = set(config["models"])
    keys = [p["parameter_id"] for p in specs]
    requested = settings["expert_features"] or [key for key, value in config["engineering"]["coefficients"].items() if value != 0]
    if len(set(requested)) != len(requested) or set(requested) - set(keys):
        raise ValueError("微样本专家价格特征须为已存在且不重复的英文代号。")
    if len(requested) > settings["expert_max_features"] and settings["expert_features"]:
        raise ValueError("专家价格特征超过微样本上限，请明确选取少量价格相关属性。")
    if len(requested) > settings["expert_max_features"]:
        requested = []  # Keep the full fixed formula; do not rank coefficients with incomparable units.
    common = {"pipeline": {"micro": True, "imputation": "median", "scaler": "standard",
                            "numeric_transform": "identity", "select_k": "all", "missing_indicator": True},
              "target_transform": "identity", "interactions": False}
    candidates = []
    def add(name, label, **params):
        candidate = dict(copy.deepcopy(common), model=name, **params)
        candidates.append({"config": candidate, "label": label})
    candidates.append({"config": {"model": "baseline"}, "label": "常量价格参考（MAPE中位数）"})
    if "ridge" in allowed:
        for alpha in settings["ridge_alphas"]:
            add("ridge", "全部属性强收缩岭回归（%g）" % alpha, alpha=float(alpha))
        if settings["log_target"]:
            add("ridge", "相对价格强收缩岭回归", alpha=1.)
            candidates[-1]["config"]["target_transform"] = "log"
        add("ridge", "折内%d维压缩岭回归" % settings["latent_components"], alpha=10.)
        candidates[-1]["config"]["pipeline"]["latent_components"] = settings["latent_components"]
        if requested:
            for alpha in (1., 10.):
                add("ridge", "专家价格属性子集岭回归（%g）" % alpha, alpha=alpha, feature_subset=list(requested))
    if "bayesian_ridge" in allowed:
        add("bayesian_ridge", "折内低维贝叶斯岭回归")
        candidates[-1]["config"]["pipeline"]["latent_components"] = settings["latent_components"]
    if "svr" in allowed:
        add("svr", "折内低维线性支持向量回归", kernel="linear", C=1., epsilon=.1)
        candidates[-1]["config"]["pipeline"]["latent_components"] = settings["latent_components"]
    tree = next((name for name in ("extra_trees", "random_forest", "gbdt") if name in allowed), None)
    if tree:
        add(tree, "浅树对照（深度1，种子平均）", max_depth=1, min_samples_leaf=2, n_estimators=64)
    if "engineering_cost" in allowed:
        candidates.append({"config": {"model": "engineering_cost"}, "label": "固定专家工程成本"})
    if "engineering_residual" in allowed:
        candidates.append({"config": {"model": "engineering_residual", "micro_calibration": True,
                                        "alpha": settings["calibration_strength"]}, "label": "工程成本低维收缩校准"})
    return candidates


def micro_oof(frame, y, groups, specs, candidate, seeds, config, worker, cache):
    source = Path(__file__).read_bytes() + Path(sys.modules[EngineeringPriceRegressor.__module__].__file__).read_bytes()
    source += Path(sys.modules[worker.__class__.__module__].__file__).read_bytes()
    identity = {"candidate": candidate, "seeds": seeds, "protocol": "group_loo_v1",
                "engineering": config["engineering"], "micro_settings": config["small_sample"],
                "versions": [sys.version.split()[0], __import__("sklearn").__version__, np.__version__, __import__("scipy").__version__],
                "schema": [(p["parameter_id"], p["value_type"]) for p in specs], "code": hashlib.sha256(source).hexdigest()}
    key = hashlib.sha256(frame.values.tobytes() + y.tobytes() + groups.tobytes() + json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()
    path = cache / (key + ".npz")
    if path.is_file():
        with np.load(str(path), allow_pickle=False) as saved:
            predictions = saved["predictions"]
        if predictions.shape == (len(seeds), len(y)) and np.all(np.isfinite(predictions)) and np.all(predictions > 0):
            return predictions, True
    predictions = np.zeros((len(seeds), len(y)))
    for training, validation in LeaveOneGroupOut().split(frame, y, groups):
        if set(groups[training]) & set(groups[validation]):
            raise AssertionError("微样本业务组跨折。")
        for index, seed in enumerate(seeds):
            predictions[index, validation] = worker.fit(candidate, seed, specs, frame.iloc[training], y[training], frame.iloc[validation])[0]
    cache.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        np.savez_compressed(handle, predictions=predictions)
    return predictions, False


def model_description(model, frame):
    from sklearn.pipeline import Pipeline
    from services.price_service.ensemble_estimators import MAPEConstantRegressor, EngineeringCalibratedRegressor
    if isinstance(model, MAPEConstantRegressor):
        return {"kind": "constant_reference", "learned_price_parameters": 1}
    if isinstance(model, EngineeringCalibratedRegressor):
        return {"kind": "anchored_engineering_calibration", "learned_correction_parameters": 2,
                "correction_intercept_wan": float(model.correction_[0]),
                "relative_slope": 1. + float(model.correction_[1] / model.base_scale_)}
    if isinstance(model, EngineeringPriceRegressor):
        return {"kind": "fixed_engineering", "learned_price_parameters": 0}
    pipeline = getattr(model, "regressor_", model)
    if isinstance(pipeline, Pipeline):
        values = frame
        for _, step in pipeline.steps[:-1]:
            values = step.transform(values)
        regressor = pipeline.steps[-1][1]
        result = {"kind": "regularized_statistical", "transformed_dimensions": values.shape[1]}
        if hasattr(regressor, "effective_df_"):
            result.update(effective_linear_df=float(regressor.effective_df_), fitted_ridge_alpha=float(regressor.fitted_alpha_),
                          effective_df_limit=float(regressor.df_limit_))
        return result
    return {"kind": "statistical"}


def stabilize_weights(y, bank, groups, candidates, config, initial):
    """Deletion sensitivity of fixed OOF rows, not an independent meta-CV score."""
    initial = copy.deepcopy(initial)
    first = np.asarray(initial["weights"])
    replicates, failures = [], []
    if config["small_sample"]["weight_jackknife"]:
        for group in np.unique(groups):
            retained = groups != group
            if np.var(y[retained]) <= 1e-12:
                failures.append("删除组后价格无变化")
                continue
            try:
                fitted = _fusion(y[retained], bank[retained], candidates, config)
                replicates.append(fitted["weights"])
            except (ValueError, FusionOptimizationError) as exc:
                failures.append(str(exc))
    target = np.mean(replicates, axis=0) if replicates else first
    limits = initial.get("error_budget_limits")
    def acceptable(weights):
        if limits:
            score = metrics(y, bank.dot(weights))
            if score["mape_percent"] > limits["mape_percent"] + 1e-6 or score["r2"] < limits["r2"] - 1e-8:
                return False
        for item in initial.get("expert_groups", []):
            share = sum(weights[i] for i in item["members"])
            if share < item.get("lower_bound", 0) - 1e-7 or share > item.get("upper_bound", 1) + 1e-7:
                return False
        return True
    chosen, fraction = first, 0.
    for amount in config["small_sample"]["jackknife_blends"]:
        proposed = (1. - amount) * first + amount * target
        if acceptable(proposed):
            chosen, fraction = proposed, amount
    score = metrics(y, bank.dot(chosen))
    initial["weights"] = chosen.tolist()
    initial["development_metrics"] = {key: score[key] for key in ("mape_percent", "r2")}
    reference = initial.get("data_reference_metrics")
    if reference:
        initial["mape_delta_percentage_points"] = score["mape_percent"] - reference["mape_percent"]
        initial["r2_drop"] = reference["r2"] - score["r2"]
    for item in initial.get("expert_groups", []):
        item["learned_weight"] = float(sum(chosen[i] for i in item["members"]))
        item["deviation"] = item["learned_weight"] - item["weight"] if item.get("weight") is not None else None
    initial["weight_stability"] = {"successful_deletions": len(replicates), "failed_deletions": failures,
                                    "weight_std": np.std(replicates, axis=0).tolist() if replicates else [0.] * len(chosen),
                                    "blend_toward_deletion_mean": fraction,
                                    "interpretation": "固定开发OOF的删除敏感性诊断；不是独立泛化分数，外层仍重跑全流程。"}
    return initial


def inner_micro(frame, y, groups, specs, config, worker, cache, directory, progress):
    directory.mkdir(parents=True, exist_ok=True)
    settings = config["small_sample"]
    stream = [int(s) for s in np.random.SeedSequence(config["seed_bagging"]["base"]).generate_state(settings["tree_seeds"])]
    candidates, failures, cache_hits = [], [], 0
    for template in portfolio(specs, config):
        candidate = template["config"]
        seeds = stream if FAMILIES[candidate["model"]] == "tree" else stream[:1]
        try:
            predicted, hit = micro_oof(frame, y, groups, specs, candidate, seeds, config, worker, cache)
            cache_hits += int(hit)
            value = predicted.mean(axis=0)
            row = dict(template, seeds=seeds, seed_history=[], seed_stop_reason="fixed_micro_seed_average" if len(seeds) > 1 else "deterministic_single_instance",
                       oof=value, oof_repeats=value[None, :], screen_metrics=validation_metrics(y, value))
            candidates.append(row)
            progress("微样本固定候选%d：%s" % (len(candidates), row["label"]))
        except BudgetExpired:
            raise
        except Exception as exc:
            failures.append({"config": candidate, "reason": str(exc)})
    if not candidates or candidates[0]["config"]["model"] != "baseline":
        raise RuntimeError("微样本常量参考模型无法完成验证。")
    baseline = candidates[0]
    if np.var(y) <= 1e-12:
        if any(item.get("lower_bound", 0) > 0 for item in config["ensemble"]["family_priors"]):
            raise ValueError("训练折价格无变化，无法在R²目标下验证专家硬性最低份额。")
        final = [baseline]
        fused = {"mode": "constant_training_fold", "weights": [1.], "expert_groups": [], "r2_defined": False}
    else:
        base_loss = _loss(y, baseline["oof"], config["ensemble"]["alpha"])
        ranked = sorted(candidates[1:], key=lambda row: _loss(y, row["oof"], config["ensemble"]["alpha"]))
        required = {item["family"] for item in config["ensemble"]["family_priors"] if item.get("lower_bound", 0) > 0}
        eligible = []
        base_ape = np.abs((y - baseline["oof"]) / y) * 100.
        for row in ranked:
            gain = base_ape - np.abs((y - row["oof"]) / y) * 100.
            group_gain = np.asarray([np.mean(gain[groups == group]) for group in np.unique(groups)])
            fraction = float(np.mean(group_gain > 0))
            row["baseline_skill"] = {"mape_gain_pp": float(np.mean(gain)), "group_win_fraction": fraction,
                                     "interpretation": "开发OOF稳定性启发式，不是统计显著性检验。"}
            reliable = (_loss(y, row["oof"], config["ensemble"]["alpha"]) <= base_loss + 1e-12
                        and float(np.mean(gain)) >= settings["minimum_mape_gain_pp"]
                        and fraction >= settings["minimum_group_win_fraction"])
            if reliable or FAMILIES[row["config"]["model"]] in required:
                eligible.append(row)
        final = [baseline]
        for family in sorted(required):
            row = next((row for row in eligible if FAMILIES[row["config"]["model"]] == family), None)
            if row is None:
                raise ValueError("微样本可用候选无法满足专家硬性模型族：" + family)
            final.append(row)
        limit = min(settings["max_members"], config["ensemble"]["max_members"])
        if len(final) > limit:
            raise ValueError("微样本成员上限与专家硬界冲突。")
        represented = {FAMILIES[row["config"]["model"]] for row in final}
        for row in eligible:
            if len(final) >= limit:
                break
            family = FAMILIES[row["config"]["model"]]
            if family not in represented:
                final.append(row); represented.add(family)
        bank = np.column_stack([row["oof"] for row in final])
        fused = stabilize_weights(y, bank, groups, final, config, _fusion(y, bank, final, config))
    weights = np.asarray(fused["weights"])
    bank = np.column_stack([row["oof"] for row in final])
    prediction = bank.dot(weights)
    score = validation_metrics(y, prediction)
    cv = {"mean_mape": score["mape_percent"], "std_mape": None,
          "mean_r2": score["r2"] if score["r2"] is not None else 0., "std_r2": None,
          "protocol": "pooled_group_loo", "r2_defined": score["r2_defined"]}
    records = [dict((key, value) for key, value in row.items() if key not in ("oof", "oof_repeats")) for row in candidates]
    write_json(directory / "search_trials.json", {"strategy": "bounded_micro_portfolio", "valid_candidates": records,
                "failed_candidates": failures, "cache_hits": cache_hits, "supervised_feature_screening": False})
    write_json(directory / "weight_report.json", fused)
    np.savez_compressed(str(directory / "development_oof_bank.npz"), y=y, predictions=bank, groups=groups, weights=weights)
    return {"candidates": final, "weights": weights, "fusion": fused, "cv": cv, "oof": prediction,
            "comparisons": {"selected": score, "constant_baseline": baseline["screen_metrics"],
                            "best_single": min((row["screen_metrics"] for row in candidates), key=lambda s: s["mape_percent"])},
            "searched_models": sorted({row["config"]["model"] for row in candidates}), "completed_trials": len(candidates),
            "cache_hits": cache_hits, "failures": failures}
