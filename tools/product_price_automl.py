# -*- coding: utf-8 -*-
"""Small-sample AutoML that exports the existing native price-service contract."""
from __future__ import print_function

import json
import hashlib
import math
from pathlib import Path

from app.console_compat import enable_plain_console
enable_plain_console()

import numpy as np
import optuna
import pandas as pd
import sklearn
from sklearn.compose import ColumnTransformer, TransformedTargetRegressor
from sklearn.impute import SimpleImputer
from sklearn.metrics import r2_score
from sklearn.model_selection import GroupShuffleSplit, KFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from services.price_service.export_native_price_bundle import export_from_notebook
from services.price_service.native_bundle import save_bundle


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def metrics(y, predicted):
    y, predicted = np.asarray(y, dtype=float), np.asarray(predicted, dtype=float)
    if not np.all(np.isfinite(predicted)):
        raise ValueError("模型预测包含非有限值。")
    if len(y) < 2 or np.ptp(y) <= 1e-12:
        raise ValueError("验证样本不足或价格没有变化，无法可靠计算R方。")
    return {"mape_percent": float(np.mean(np.abs((y - predicted) / y)) * 100),
            "r2": float(r2_score(y, predicted)),
            "mae_wan": float(np.mean(np.abs(y - predicted)))}


def price_dataset(data, min_groups=12):
    labelled = [row for row in data["agreements"] if row.get("historical_price_wan") is not None]
    specs = [p for p in data["parameters"] if p["value_type"] != "text" and p["inference"]["observed_count"]
             and any(row["params"].get(p["parameter_id"]) is not None for row in labelled)]
    if not specs:
        raise ValueError("没有可用于价格训练的数值、布尔或枚举属性。")
    mappings = {}
    for spec in specs:
        if spec["value_type"] == "enum":
            allowed = json.loads(spec.get("allowed_values_json") or "[]")
            mappings[spec["parameter_id"]] = dict((str(x), float(i)) for i, x in enumerate(allowed))
    records, targets, record_ids = [], [], []
    for row in data["agreements"]:
        target = row.get("historical_price_wan")
        if target is None:
            continue
        if not math.isfinite(float(target)) or float(target) <= 0:
            raise ValueError("成品%s的价格必须为正数，MAPE不支持零价格。" % row["agreement_id"])
        values = []
        for spec in specs:
            key = spec["parameter_id"]
            value = row["params"].get(key)
            if value is None:
                values.append(float("nan"))
            elif key in mappings:
                values.append(mappings[key][str(value)])
            else:
                numeric = float(value)
                if not math.isfinite(numeric):
                    raise ValueError("成品%s的属性%s不是有限数值。" % (row["agreement_id"], spec["label"]))
                values.append(numeric)
        records.append(values)
        targets.append(float(target))
        record_ids.append(row["agreement_id"])
    if len(records) < min_groups:
        raise ValueError("AutoML至少需要%d条有效价格记录，以保留独立测试集并进行交叉验证。" % min_groups)
    frame = pd.DataFrame(records, columns=[p["parameter_id"] for p in specs], dtype=float)
    # Identical product inputs stay together, including duplicate rows with different prices.
    signatures = [json.dumps([None if not np.isfinite(x) else float(x) for x in row], separators=(",", ":")) for row in records]
    groups = np.asarray(pd.factorize(np.asarray(signatures, dtype=object))[0])
    if len(np.unique(groups)) < min_groups:
        raise ValueError("至少需要%d组不同属性的成品；重复记录不能增加独立验证样本数。" % min_groups)
    return frame, np.asarray(targets), specs, mappings, groups, record_ids


def native_field_metadata(frame, specs, mappings):
    metadata = {}
    for spec in specs:
        key = spec["parameter_id"]
        values = frame[key].dropna()
        if values.empty:
            raise ValueError("属性%s在所有有效价格记录中都缺失。" % spec["label"])
        default = float(values.mode().iloc[0]) if key in mappings or spec["value_type"] == "boolean" else float(values.median())
        if key in mappings:
            # The service parses raw category names before the fitted encoder.
            default = next(label for label, encoded in mappings[key].items() if encoded == default)
        metadata[key] = {"field_name": key, "field_label": spec["label"], "dtype": spec["value_type"],
                         "unit": spec.get("unit", ""), "required": bool(spec["required"]),
                         "missing_policy": "reject" if spec["required"] else "default", "default_value": default,
                         "category_mapping": mappings.get(key),
                         "allowed_values": list(mappings[key]) if key in mappings else None,
                         "parser": "ip_grade" if spec["value_type"] == "ip_grade" else None}
    return metadata


def make_pipeline(config, seed, specs, log_target=False):
    from tools.automl.sklearn_0_24.automl_search import build_estimator
    numeric = [i for i, p in enumerate(specs) if p["value_type"] not in ("enum", "boolean")]
    boolean = [i for i, p in enumerate(specs) if p["value_type"] == "boolean"]
    categorical = [i for i, p in enumerate(specs) if p["value_type"] == "enum"]
    transforms = []
    if numeric:
        transforms.append(("numeric", Pipeline([("imputer", SimpleImputer(strategy="median")),
                                               ("scale", StandardScaler())]), numeric))
    if categorical:
        transforms.append(("categorical", Pipeline([("imputer", SimpleImputer(strategy="most_frequent")),
                                                   ("encode", OneHotEncoder(handle_unknown="ignore", sparse=False))]), categorical))
    if boolean:
        transforms.append(("boolean", Pipeline([("imputer", SimpleImputer(strategy="most_frequent")),
                                               ("scale", StandardScaler())]), boolean))
    pipeline = Pipeline([("prepare", ColumnTransformer(transforms)),
                         ("model", build_estimator(config, seed))])
    if log_target:
        return TransformedTargetRegressor(regressor=pipeline, func=np.log, inverse_func=np.exp)
    return pipeline


def grouped_folds(groups, folds, seed):
    unique = np.unique(groups)
    for train, validation in KFold(n_splits=folds, shuffle=True, random_state=int(seed)).split(unique):
        yield np.flatnonzero(np.isin(groups, unique[train])), np.flatnonzero(np.isin(groups, unique[validation]))


def evaluate(frame, y, specs, groups, config, seeds, folds, log_target):
    per_seed, scores, errors = [], [], []
    for seed in seeds:
        seed_scores = []
        for train, validation in grouped_folds(groups, folds, seed):
            fitted = make_pipeline(config, seed, specs, log_target).fit(frame.iloc[train], y[train])
            predicted = fitted.predict(frame.iloc[validation])
            score = metrics(y[validation], predicted)
            seed_scores.append(score)
            scores.append(score)
            errors.extend(np.abs(y[validation] - predicted).tolist())
        per_seed.append({"seed": int(seed),
                         "mean_mape": float(np.mean([x["mape_percent"] for x in seed_scores])),
                         "mean_r2": float(np.mean([x["r2"] for x in seed_scores]))})
    return {"mean_mape": float(np.mean([x["mape_percent"] for x in scores])),
            "std_mape": float(np.std([x["mape_percent"] for x in scores])),
            "mean_r2": float(np.mean([x["r2"] for x in scores])),
            "std_r2": float(np.std([x["r2"] for x in scores])),
            "per_seed": per_seed, "absolute_errors_wan": errors}


def choose_seed(rows):
    ms = np.asarray([x["mean_mape"] for x in rows])
    rs = np.asarray([x["mean_r2"] for x in rows])
    md, rd = max(float(np.std(ms)), 1e-12), max(float(np.std(rs)), 1e-12)
    for row in rows:
        row["representative_distance"] = float(math.hypot((row["mean_mape"] - float(np.median(ms))) / md,
                                                          (row["mean_r2"] - float(np.median(rs))) / rd))
    return int(min(rows, key=lambda r: (r["representative_distance"], r["seed"]))["seed"])


def choose_trial(trials, target_mape, target_r2):
    qualified = [t for t in trials if t.user_attrs["cv"]["mean_mape"] <= target_mape and t.user_attrs["cv"]["mean_r2"] >= target_r2]
    pool = qualified or trials
    # Select on stability-adjusted objectives, among all completed trials satisfying thresholds.
    lo_m, hi_m = min(t.values[0] for t in pool), max(t.values[0] for t in pool)
    lo_r, hi_r = min(t.values[1] for t in pool), max(t.values[1] for t in pool)
    def distance(t):
        return math.hypot((t.values[0] - lo_m) / max(hi_m - lo_m, 1e-12),
                          (hi_r - t.values[1]) / max(hi_r - lo_r, 1e-12))
    return min(pool, key=lambda t: (distance(t), t.number)), bool(qualified)


def train_price(data, output_dir, trials=24, folds=3, seeds=(42, 137, 256),
                search_seed=2026, holdout_seed=20261002, target_mape=10.0, target_r2=0.85,
                timeout_seconds=180, models=None, log_target=False, progress=None):
    from tools.automl.sklearn_0_24.automl_search import SUPPORTED_MODELS, suggest_config
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    models = list(models or SUPPORTED_MODELS)
    if not models or len(set(models)) != len(models) or any(m not in SUPPORTED_MODELS for m in models):
        raise ValueError("价格模型名单包含未知或重复模型。")
    seeds = list(dict.fromkeys(int(s) for s in seeds))
    if len(seeds) < 2:
        raise ValueError("稳定性验证至少需要两个随机种子。")
    if folds < 2 or trials < len(models) or timeout_seconds <= 0:
        raise ValueError("折数至少2，搜索次数至少覆盖每个候选模型一次，时间预算必须为正。")
    if not math.isfinite(target_mape) or target_mape < 0 or not math.isfinite(target_r2) or target_r2 > 1:
        raise ValueError("MAPE阈值须非负，R方阈值须不大于1。")
    frame, y, specs, mappings, groups, record_ids = price_dataset(data)
    development, holdout = next(GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=holdout_seed).split(frame, y, groups))
    dev_frame, dev_y, dev_groups = frame.iloc[development].reset_index(drop=True), y[development], groups[development]
    folds = min(int(folds), len(np.unique(dev_groups)) // 2)
    if folds < 2:
        raise ValueError("分组后可用于交叉验证的独立成品不足。")
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    study = optuna.create_study(directions=["minimize", "maximize"], sampler=optuna.samplers.TPESampler(seed=int(search_seed)))
    for model in models:
        study.enqueue_trial({"model": model})
    def objective(trial):
        config = suggest_config(trial, models)
        trial.set_user_attr("config", config)
        cv = evaluate(dev_frame, dev_y, specs, dev_groups, config, seeds, folds, log_target)
        trial.set_user_attr("cv", dict((k, v) for k, v in cv.items() if k != "absolute_errors_wan"))
        return cv["mean_mape"] + 0.5 * cv["std_mape"], cv["mean_r2"] - 0.5 * cv["std_r2"]
    def on_trial(study, trial):
        if progress:
            progress("价格AutoML：已完成%d/%d次搜索" % (len([t for t in study.trials if t.state.is_finished()]), trials))
    study.optimize(objective, n_trials=int(trials), timeout=float(timeout_seconds), catch=(ValueError, FloatingPointError), callbacks=[on_trial])
    completed = [t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE]
    if not completed:
        raise RuntimeError("所有AutoML候选均失败，请查看搜索报告或增加有效样本。")
    selected, cv_met = choose_trial(completed, float(target_mape), float(target_r2))
    config = selected.user_attrs["config"]
    cv = evaluate(dev_frame, dev_y, specs, dev_groups, config, seeds, folds, log_target)
    representative_seed = choose_seed(cv["per_seed"])
    # The untouched holdout is evaluated only after all model/seed decisions.
    validation_model = make_pipeline(config, representative_seed, specs, log_target).fit(dev_frame, dev_y)
    holdout_predictions = validation_model.predict(frame.iloc[holdout])
    holdout_metrics = metrics(y[holdout], holdout_predictions)
    holdout_met = holdout_metrics["mape_percent"] <= target_mape and holdout_metrics["r2"] >= target_r2
    final_model = make_pipeline(config, representative_seed, specs, log_target).fit(frame, y)
    metadata = native_field_metadata(frame, specs, mappings)
    product = data["products"][0]
    path = output_dir / "price_native_bundle.pkl"
    bundle = export_from_notebook({"X_train": frame, "automl_winner": final_model}, output=path,
                                 product_code=product["product_code"], product_name=product["product_name"],
                                 model_version="prototype-automl-v1", target_divisor_to_wan=1,
                                 field_metadata=metadata, model_variables={config["model"]: "automl_winner"},
                                 ensemble_model_names=[config["model"]], ensemble_weights=[1.0],
                                 strict=True, model_source="namespace", model_output_transform="direct")
    bundle["residual_calibration"] = {"space": "native", "half_width_native": float(np.quantile(cv["absolute_errors_wan"], 0.95)),
                                      "coverage": None, "source": "development_cv_exploratory_interval"}
    save_bundle(path, bundle)
    manifest_path = Path(str(path) + ".manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["bundle_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest["residual_calibration"] = bundle["residual_calibration"]
    write_json(manifest_path, manifest)
    trial_rows = [{"trial": t.number, "status": t.state.name, "config": t.user_attrs.get("config"),
                   "cv": t.user_attrs.get("cv"), "objectives": list(t.values) if t.values else None} for t in study.trials if t.state.is_finished()]
    report = {"selected_model": config["model"], "selected_config": config, "selected_trial": selected.number,
              "representative_seed": representative_seed, "seed_selection": "closest_to_median_multi_seed_behavior",
              "search_seed": int(search_seed), "holdout_seed": int(holdout_seed), "cv_seeds": seeds, "folds": folds,
              "requested_trials": int(trials), "completed_trials": len(completed), "pareto_trials": [t.number for t in study.best_trials],
              "requested_models": models, "searched_models": sorted(set(t.user_attrs["config"]["model"] for t in completed)),
              "feature_order": list(frame.columns), "categorical_encoding": "one_hot_fitted_inside_each_training_fold",
              "training_rows": len(frame), "development_rows": len(development), "holdout_rows": len(holdout),
              "development_ids": [record_ids[i] for i in development], "holdout_ids": [record_ids[i] for i in holdout],
              "duplicate_group_policy": "identical_product_inputs_never_cross_validation_boundaries",
              "cv_metrics": dict((k, v) for k, v in cv.items() if k != "absolute_errors_wan"), "holdout_metrics": holdout_metrics,
              "targets": {"mape_percent_max": float(target_mape), "r2_min": float(target_r2)},
              "cv_targets_met": cv_met, "holdout_targets_met": bool(holdout_met), "targets_met": bool(cv_met and holdout_met),
              "holdout_evaluated_before_final_refit": True, "export_refitted_on_all_valid_rows": True,
              "prediction_interval": "开发集交叉验证残差的探索区间，未验证95%覆盖率",
              "log_target": bool(log_target), "sklearn_version": sklearn.__version__,
              "warning": "未达目标时仍保留试用模型并如实标记。独立测试指标来自全部数据重训之前。"}
    write_json(output_dir / "training_report.json", report)
    write_json(output_dir / "search_trials.json", trial_rows)
    write_json(output_dir / "seed_report.json", cv["per_seed"])
    pd.DataFrame([dict({"trial": t.number, "model": t.user_attrs["config"]["model"]},
                       **{k: v for k, v in t.user_attrs["cv"].items() if k != "per_seed"}) for t in completed]).to_csv(output_dir / "leaderboard.csv", index=False, encoding="utf-8-sig")
    return bundle, report
