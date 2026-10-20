# -*- coding: utf-8 -*-
"""Offline design probe: nested grouped OOF weighting on the locked Py38 stack.

This is a small fixed-library experiment, not the production AutoML V2 engine.
It does not install a model or alter the live product pipeline.
"""
from __future__ import print_function

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import scipy
import sklearn
from scipy.optimize import minimize
from sklearn.kernel_ridge import KernelRidge
from sklearn.linear_model import HuberRegressor, Ridge
from sklearn.model_selection import GroupShuffleSplit
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import PolynomialFeatures

from app.historical_onboarding import HistoricalProductOnboarding
from app.product_table import product_table_rows
from services.price_service.export_native_price_bundle import export_from_notebook
from services.price_service.native_bundle import load_bundle, predict, save_bundle
from tools.product_price_automl import grouped_folds, make_pipeline, metrics, native_field_metadata, price_dataset, write_json


NAMES = ("ridge", "lasso", "bayesian_ridge", "huber", "svr", "extra_trees",
         "gbdt_huber", "gbdt_log", "ridge_interactions", "kernel_ridge")
METHODS = ("best_single", "equal_top3", "greedy_selection", "regularized_weights")


def estimator(name, seed, specs):
    tree = {"n_estimators": 100, "max_depth": 3, "min_samples_leaf": 2,
            "min_samples_split": 2, "max_features": 1.0}
    configs = {"ridge": {"model": "ridge", "alpha": 1.0},
               "lasso": {"model": "lasso", "alpha": .0001},
               "bayesian_ridge": {"model": "bayesian_ridge", "alpha_1": 1e-6,
                                  "alpha_2": 1e-6, "lambda_1": 1e-6, "lambda_2": 1e-6},
               "svr": {"model": "svr", "C": 30.0, "epsilon": .05, "gamma": .01},
               "extra_trees": dict(tree, model="extra_trees", max_depth=6),
               "gbdt_huber": dict(tree, model="gbdt", learning_rate=.05, subsample=1.0),
               "gbdt_log": dict(tree, model="gbdt", learning_rate=.05, subsample=1.0)}
    if name in configs:
        return make_pipeline(configs[name], seed, specs, name == "gbdt_log")
    prepared = make_pipeline(configs["ridge"], seed, specs).named_steps["prepare"]
    if name == "huber":
        return Pipeline([("prepare", prepared), ("model", HuberRegressor(max_iter=1000))])
    if name == "kernel_ridge":
        return Pipeline([("prepare", prepared), ("model", KernelRidge(alpha=.5, kernel="rbf", gamma=.05))])
    if name == "ridge_interactions":
        return Pipeline([("prepare", prepared),
                         ("interactions", PolynomialFeatures(degree=2, interaction_only=True, include_bias=False)),
                         ("model", Ridge(alpha=10.0))])
    raise ValueError(name)


def loss(y, prediction):
    # On fixed y, normalized SSE is exactly 1-R2; units match relative MAPE.
    relative = np.mean(np.abs((y - prediction) / y))
    normalized_sse = np.sum((y - prediction) ** 2) / max(np.sum((y - np.mean(y)) ** 2), 1e-12)
    return float(.7 * relative + .3 * normalized_sse)


def weights_for(method, y, bank):
    width = bank.shape[1]
    ordered = sorted(range(width), key=lambda index: loss(y, bank[:, index]))
    single = np.zeros(width)
    single[ordered[0]] = 1.0
    if method == "best_single":
        return single
    prior = np.zeros(width)
    prior[ordered[:3]] = 1.0 / min(width, 3)
    if method == "equal_top3":
        return prior
    counts = np.zeros(width)
    best, best_score = single.copy(), loss(y, bank.dot(single))
    for _ in range(40):
        options = []
        for index in range(width):
            trial_counts = counts.copy()
            trial_counts[index] += 1
            trial_weights = trial_counts / np.sum(trial_counts)
            options.append((loss(y, bank.dot(trial_weights)), index, trial_weights))
        score, index, trial_weights = min(options, key=lambda option: (option[0], option[1]))
        counts[index] += 1
        if score < best_score - 1e-10:
            best, best_score = trial_weights, score
    if method == "greedy_selection":
        return best
    if method != "regularized_weights":
        raise ValueError(method)
    penalty = .0005
    def objective(weights):
        return loss(y, bank.dot(weights)) + penalty * float(np.sum((weights - prior) ** 2))
    result = minimize(objective, best, method="SLSQP", bounds=[(0.0, 1.0)] * width,
                      constraints=[{"type": "eq", "fun": lambda weights: float(np.sum(weights) - 1.0)}],
                      options={"maxiter": 300, "ftol": 1e-10})
    if result.success and np.all(np.isfinite(result.x)) and abs(np.sum(result.x) - 1) < 1e-6:
        candidate = np.clip(result.x, 0, 1)
        candidate /= np.sum(candidate)
        if objective(candidate) <= objective(best) + 1e-8:
            return candidate
    return best


def oof_bank(frame, y, specs, groups, split_seeds, estimator_seed=101, folds=3):
    bank = np.zeros((len(frame), len(NAMES)))
    for split_seed in split_seeds:
        for train, validation in grouped_folds(groups, folds, split_seed):
            assert not (set(groups[train]) & set(groups[validation]))
            for index, name in enumerate(NAMES):
                fitted = estimator(name, estimator_seed, specs).fit(frame.iloc[train], y[train])
                bank[validation, index] += fitted.predict(frame.iloc[validation])
    bank /= len(split_seeds)
    if not np.all(np.isfinite(bank)):
        raise ValueError("Candidate produced non-finite OOF predictions")
    return bank


def fitted_bank(train_frame, train_y, validation_frame, specs, estimator_seed=101):
    return np.column_stack([estimator(name, estimator_seed, specs).fit(train_frame, train_y).predict(validation_frame)
                            for name in NAMES])


def score(y, prediction):
    result = metrics(y, prediction)
    result["p95_ape_percent"] = float(np.quantile(np.abs((y - prediction) / y) * 100, .95))
    return result


def paired_group_bootstrap(y, selected, comparator, groups, draws=2000):
    """Conditional uncertainty of the locked comparison; not search uncertainty."""
    errors = (np.abs((y - selected) / y) - np.abs((y - comparator) / y)) * 100
    members = [np.flatnonzero(groups == group) for group in np.unique(groups)]
    generator = np.random.RandomState(20261003)
    samples = []
    for _ in range(draws):
        indices = np.concatenate([members[index] for index in generator.randint(0, len(members), len(members))])
        samples.append(float(np.mean(errors[indices])))
    low, high = [float(value) for value in np.quantile(samples, [.025, .975])]
    return {"delta_mape_percentage_points": float(np.mean(errors)), "ci95": [low, high],
            "draws": draws, "resampling_unit": "configuration_group", "independent_groups": len(members),
            "contains_zero": bool(low <= 0 <= high),
            "interpretation": "小于0表示集成误差更小；区间包含0时，不宣称改善。条件区间不覆盖模型搜索和方法选择不确定性。"}


def run(input_path, output_path, virtual_data=False):
    output = Path(output_path).resolve()
    if ROOT not in output.parents or output.parts[len(ROOT.parts)] != "outputs":
        raise ValueError("Probe output must be a new project outputs subdirectory")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Choose an empty probe output directory")
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    installed = ROOT / "services" / "price_service" / "model" / "price_native_bundle.pkl"
    installed_sha = hashlib.sha256(installed.read_bytes()).hexdigest() if installed.exists() else None
    source = Path(input_path)
    raw = source.read_bytes()
    product_table_rows(source.name, raw, require_dual_header=True)
    business = HistoricalProductOnboarding().analyze(source.name, raw, "V2_ENSEMBLE_PROBE", "加权集成方案验证",
                                                     ["-1", "\\", "/"])["data"]
    frame, y, specs, mapping, groups, ids = price_dataset(business)
    development, holdout = next(GroupShuffleSplit(n_splits=1, test_size=.2, random_state=20261002).split(frame, y, groups))
    assert not (set(groups[development]) & set(groups[holdout]))
    x_dev, y_dev, g_dev = frame.iloc[development].reset_index(drop=True), y[development], groups[development]
    repeated_scores = dict((method, []) for method in METHODS)
    for outer_seed in (42, 137):
        outer_predictions = dict((method, np.zeros(len(development))) for method in METHODS)
        for index, (train, validation) in enumerate(grouped_folds(g_dev, 3, outer_seed)):
            print("嵌套分组验证：外层种子%s，第%d/3折" % (outer_seed, index + 1), flush=True)
            inner_bank = oof_bank(x_dev.iloc[train].reset_index(drop=True), y_dev[train], specs,
                                  g_dev[train], [7001 + index])
            validation_bank = fitted_bank(x_dev.iloc[train], y_dev[train], x_dev.iloc[validation], specs)
            for method in METHODS:
                weights = weights_for(method, y_dev[train], inner_bank)
                assert np.min(weights) >= 0 and abs(np.sum(weights) - 1) < 1e-8
                outer_predictions[method][validation] = validation_bank.dot(weights)
        for method in METHODS:
            repeated_scores[method].append(score(y_dev, outer_predictions[method]))
    outer_results = {}
    for method, rows in repeated_scores.items():
        mean_mape = float(np.mean([row["mape_percent"] for row in rows]))
        std_mape = float(np.std([row["mape_percent"] for row in rows]))
        mean_r2 = float(np.mean([row["r2"] for row in rows]))
        outer_results[method] = {"mean_mape_percent": mean_mape, "std_mape_percent": std_mape,
                                 "mean_r2": mean_r2, "mean_p95_ape_percent": float(np.mean([row["p95_ape_percent"] for row in rows])),
                                 "selection_score": (mean_mape + .5 * std_mape) / 100 + .3 * (1 - mean_r2),
                                 "repetitions": rows}
    chosen = min(METHODS, key=lambda method: outer_results[method]["selection_score"])
    print("已在开发集选定融合方法：%s；现在评估保留测试集。" % chosen, flush=True)
    final_oof = oof_bank(x_dev, y_dev, specs, g_dev, [42, 137, 256])
    final_weights = weights_for(chosen, y_dev, final_oof)
    single_weights = weights_for("best_single", y_dev, final_oof)
    holdout_bank = fitted_bank(x_dev, y_dev, frame.iloc[holdout], specs)
    # This fixed comparison is reported after selection; never choose on holdout.
    holdout_results = {"selected": score(y[holdout], holdout_bank.dot(final_weights)),
                       "predeclared_single_comparator": score(y[holdout], holdout_bank.dot(single_weights))}
    comparison_uncertainty = paired_group_bootstrap(y[holdout], holdout_bank.dot(final_weights),
                                                   holdout_bank.dot(single_weights), groups[holdout])
    np.savez_compressed(str(output / "development_oof_bank.npz"), predictions=final_oof, targets=y_dev,
                        groups=g_dev, candidate_names=np.asarray(NAMES), record_ids=np.asarray([ids[index] for index in development]))
    included = [index for index, weight in enumerate(final_weights) if weight > 1e-12]
    final_weights[[index for index in range(len(NAMES)) if index not in included]] = 0
    final_weights /= np.sum(final_weights)
    fitted = dict((NAMES[index], estimator(NAMES[index], 101, specs).fit(frame, y)) for index in included)
    metadata = native_field_metadata(frame, specs, mapping)
    names = [NAMES[index] for index in included]
    weights = [float(final_weights[index]) for index in included]
    namespace = dict(fitted, X_train=frame)
    path = output / "price_native_bundle.pkl"
    bundle = export_from_notebook(namespace, output=path, product_code="V2_ENSEMBLE_PROBE", product_name="加权集成方案验证",
                                  model_version="ensemble-design-probe-v1", target_divisor_to_wan=1, field_metadata=metadata,
                                  model_variables=dict((name, name) for name in names), ensemble_model_names=names,
                                  ensemble_weights=weights, strict=True, model_source="namespace", model_output_transform="direct")
    bundle["residual_calibration"] = {"space": "native", "coverage": None,
                                      "half_width_native": float(np.quantile(np.abs(y_dev - final_oof.dot(final_weights)), .95)),
                                      "source": "development_oof_exploratory_interval"}
    save_bundle(path, bundle)
    native_manifest_path = Path(str(path) + ".manifest.json")
    native_manifest = json.loads(native_manifest_path.read_text(encoding="utf-8"))
    native_manifest["bundle_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    native_manifest["residual_calibration"] = bundle["residual_calibration"]
    write_json(native_manifest_path, native_manifest)
    exported = load_bundle(path)
    maximum_delta = 0.0
    for index, record in enumerate(business["agreements"][:8]):
        direct = sum(weight * float(fitted[name].predict(frame.iloc[[index]])[0]) for name, weight in zip(names, weights))
        actual = predict(exported, record["params"])["predicted_price_wan"]
        maximum_delta = max(maximum_delta, abs(direct - actual))
    assert maximum_delta < 1e-5, "Exported weighted prediction differs from in-memory calculation"
    if installed_sha is not None:
        assert hashlib.sha256(installed.read_bytes()).hexdigest() == installed_sha
    error_corr = np.corrcoef((final_oof - y_dev[:, None]).T)
    report = {"status": "PASS", "experiment_kind": "fixed_library_design_probe_not_full_automl_v2",
              "virtual_data": bool(virtual_data), "input_sha256": hashlib.sha256(raw).hexdigest(),
              "python": sys.version, "sklearn": sklearn.__version__, "scipy": scipy.__version__,
              "rows": len(frame), "candidates": list(NAMES), "outer_split_seeds": [42, 137], "estimator_seed": 101,
              "outer_folds": 3, "inner_folds": 3, "final_oof_repeats": 3,
              "outer_validation": outer_results, "selected_method": chosen,
              "final_weights": dict(zip(names, weights)), "holdout_evaluation": holdout_results,
              "single_comparator_weights": dict((name, float(weight)) for name, weight in zip(NAMES, single_weights) if weight > 0),
              "paired_holdout_bootstrap": comparison_uncertainty, "installed_model_unchanged": True,
              "holdout_ids": [ids[index] for index in holdout], "holdout_used_for_selection": False,
              "error_correlation": [[float(value) if np.isfinite(value) else None for value in row] for row in error_corr],
              "prediction_equivalence_max_delta_wan": maximum_delta,
              "elapsed_seconds": round(time.monotonic() - started, 2),
              "limitations": ["固定模型库验证融合方法；尚未实现完整预算调度和联合AutoML搜索。",
                              "选方法后的外层分数仍参与选择，最终效果以保留测试集为准。",
                              "虚拟数据只验证方法与兼容栈，不能证明真实业务精度或Win7实机兼容。",
                              "未校准预测区间覆盖率；当前生产流水线与模型未修改。"]}
    write_json(output / "ensemble_probe_report.json", report)
    print(json.dumps({"status": report["status"], "selected_method": chosen, "weights": report["final_weights"],
                      "holdout": holdout_results, "elapsed_seconds": report["elapsed_seconds"]}, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input")
    parser.add_argument("--output", required=True)
    parser.add_argument("--virtual-data", action="store_true")
    args = parser.parse_args()
    run(args.input, args.output, args.virtual_data)
