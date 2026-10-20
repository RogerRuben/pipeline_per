# -*- coding: utf-8 -*-
"""Joint multi-objective Optuna optimization for the original six-model price ensemble.

Usage:
    python price_automl_optuna_v2.py train --data '增强样本(修改后).xlsx' --trials 150
    python price_automl_optuna_v2.py predict --model results/price_ensemble_bundle.pkl --data new_rows.xlsx
"""
import argparse
import json
import os
import sys
import warnings
from datetime import datetime

import numpy as np
import pandas as pd
import optuna

from .price_automl_core import (
    EXPERT_MAP, MODEL_NAMES, TARGET_DEFAULT, fit_ensemble, load_bundle,
    load_training_data, model_params_from_trial, normalized_weights,
    predict_bundle, read_table, regression_metrics, save_bundle, split_data,
)


def _json_ready(value):
    if isinstance(value, dict):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, np.ndarray):
        return [_json_ready(x) for x in value.tolist()]
    return value


def _evaluate(cfg, X, y, source_rows, expert_map, test_size, price_bins):
    Xtr, ytr, Xte, yte, tr_rows, te_rows = split_data(
        X, y, source_rows, cfg["split_seed"], test_size, price_bins)
    bundle = fit_ensemble(Xtr, ytr, cfg, expert_map)
    prediction, components = predict_bundle(bundle, Xte)
    scores = regression_metrics(yte, prediction)
    return scores, bundle, (Xtr, ytr, Xte, yte, tr_rows, te_rows, prediction, components)


def _select_pareto(study, r2_target, mape_target):
    pareto = [t for t in study.best_trials
              if t.values is not None and np.all(np.isfinite(t.values)) and t.user_attrs.get("valid")]
    if not pareto:
        raise RuntimeError("Every Optuna trial failed. Review optuna_trials.csv for errors")
    feasible = [t for t in pareto if t.values[0] >= r2_target and t.values[1] <= mape_target]
    pool = feasible if feasible else pareto
    if feasible:
        r2s = np.array([t.values[0] for t in pool])
        mapes = np.array([t.values[1] for t in pool])
        r2_range = max(float(r2s.max() - r2s.min()), 1e-8)
        mape_range = max(float(mapes.max() - mapes.min()), 1e-8)
        # Distance from the best attainable Pareto point for each objective;
        # scale by Pareto ranges, not an arbitrary combined loss.
        def ranking(t):
            dr2 = (r2s.max() - t.values[0]) / r2_range
            dmape = (t.values[1] - mapes.min()) / mape_range
            return (float(np.hypot(dr2, dmape)), t.values[1], -t.values[0])
        chosen = min(feasible, key=ranking)
        mode = "feasible_pareto_ideal_point"
    else:
        # Pick the least violation of the two user targets if no feasible point exists.
        def ranking(t):
            r2_miss = max(0.0, r2_target - t.values[0]) / max(0.1, abs(r2_target))
            mape_miss = max(0.0, t.values[1] - mape_target) / max(mape_target, 1e-8)
            return (r2_miss + mape_miss, mape_miss, -t.values[0])
        chosen = min(pareto, key=ranking)
        mode = "nearest_target_not_feasible"
    return chosen, feasible, mode



def _original_baseline(seed):
    """Parameters printed by the user-uploaded original notebook, used as warm starts.

    They are *candidate starting points*, not assumed reproducible metrics:
    this version has a configurable price stratification and outlier policy.
    """
    cfg = {
        "split_seed": int(seed), "lasso_alpha": 0.001, "ridge_alpha": 0.1,
        "svr_kernel": "linear", "svr_C": 1.0, "svr_epsilon": 0.01,
        "svr_gamma": 0.01,
        "gbdt_n_estimators": 300, "gbdt_max_depth": 3,
        "gbdt_min_samples_split": 2, "gbdt_learning_rate": 0.1,
        "gbdt_subsample": 0.7,
        "et_n_estimators": 300, "et_max_depth": 10,
        "et_min_samples_split": 4, "et_max_features": 1.0,
        "tree_max_depth": 5, "tree_min_samples_leaf": 1,
        "tree_lambda_expert": 0.8, "tree_min_gain_split": 0.1,
    }
    for name in MODEL_NAMES:
        cfg["weight_" + name] = 0.5
    return cfg


def run_training(data_path, output_dir="optuna_results", n_trials=150,
                 target=TARGET_DEFAULT, test_size=0.2, price_bins=3,
                 r2_target=0.90, mape_target=0.10,
                 seed_low=1, seed_high=10000,
                 optimizer_seed=42, drop_first=True, remove_outliers=False,
                 verbose=True, prepared_data=None, expert_scores=None,
                 progress_callback=None, timeout_seconds=None, model_backend=None,
                 search_driver=None, history_identity=None, min_samples=8):
    """Perform joint search and save split validation plus a full-data deployment bundle.

    Notes: validation metrics are SEARCHED on holdout labels, NOT untouched independent
    test scores. Full-data bundle must not be evaluated as a holdout model.
    """
    if n_trials <= 0:
        raise ValueError("n_trials must be positive")
    if not (0.0 < test_size < 0.8):
        raise ValueError("test_size must be between 0 and 0.8")
    if seed_low < 0 or seed_high < seed_low:
        raise ValueError("invalid seed range")
    if not isinstance(min_samples,int) or isinstance(min_samples,bool) or min_samples<4:raise ValueError('min_samples must be an integer >= 4')
    # Optional typed input/search orchestration; numeric V2 objective stays unchanged.
    _evaluate = model_backend.evaluate if model_backend is not None else globals()['_evaluate']
    fit_ensemble = model_backend.fit if model_backend is not None else globals()['fit_ensemble']
    predict_bundle = model_backend.predict if model_backend is not None else globals()['predict_bundle']
    os.makedirs(output_dir, exist_ok=True)
    if prepared_data is None:
        X, y, source_rows = load_training_data(
            data_path, target=target, drop_first=drop_first,
            remove_outliers=remove_outliers)
    else:
        # The pipeline has already parsed bilingual headers and numeric codes.
        # Complete prepared data follows the same six-model training algorithm.
        X, y, source_rows = prepared_data
        X=X.copy();y=np.asarray(y,dtype=float);source_rows=np.asarray(source_rows,dtype=int)
        if len(X)<min_samples or len(X)!=len(y) or len(y)!=len(source_rows):
            raise ValueError('Prepared data requires at least %d aligned records'%min_samples)
        validation_rows=int(np.ceil(len(X)*test_size))
        if validation_rows<2 or len(X)-validation_rows<2:raise ValueError('Prepared split requires at least 2 training and 2 validation records')
        if X.columns.duplicated().any() or not np.isfinite(X.to_numpy(dtype=float)).all() or not np.isfinite(y).all() or np.any(y<=0):
            raise ValueError('Prepared data requires unique features, finite values and positive prices')
        if remove_outliers:raise ValueError('Pipeline prepared data does not silently remove outliers')
    expert_map = {name: (expert_scores if expert_scores is not None else EXPERT_MAP).get(name, 1) for name in X.columns}
    if verbose:
        print("Records: {} | Features: {} | Models: {}".format(len(y), X.shape[1], len(MODEL_NAMES)))
        print("Search: {} trials | R2 >= {:.2f} | MAPE <= {:.1f}%".format(
            n_trials, r2_target, mape_target * 100))
        print("Missing expert scores (treated as 1):", [k for k in X.columns if k not in EXPERT_MAP])
    sampler = optuna.samplers.NSGAIISampler(seed=int(optimizer_seed), population_size=min(30, max(10, n_trials)))
    study = optuna.create_study(directions=["maximize", "minimize"], sampler=sampler,
                                study_name="price_ensemble_joint_optuna")
    # Baseline hyperparameters / promising split seeds are sourced from the uploaded Notebook.
    # Warm starts are counted in n_trials and are evaluated from scratch on this input data.
    for warm_seed in (7527, 6192, 9300, 9431, 3308)[:min(5, n_trials)]:
        if seed_low <= warm_seed <= seed_high:
            study.enqueue_trial(_original_baseline(warm_seed))
    optuna.logging.set_verbosity(optuna.logging.WARNING if not verbose else optuna.logging.INFO)

    def objective(trial):
        cfg = model_params_from_trial(trial, seed_low, seed_high)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                scores, _, _ = _evaluate(cfg, X, y, source_rows, expert_map, test_size, price_bins)
            if not np.isfinite(scores["r2"]) or not np.isfinite(scores["mape"]):
                raise ValueError("Nonfinite metric")
            trial.set_user_attr("valid", True)
            trial.set_user_attr("mae", scores["mae"])
            trial.set_user_attr("weights", normalized_weights(cfg).tolist())
            return scores["r2"], scores["mape"]
        except (ValueError, FloatingPointError, OverflowError, RuntimeError) as exc:
            trial.set_user_attr("valid", False)
            trial.set_user_attr("error", repr(exc))
            return -1e6, 1e6

    def report_trial(current_study,trial):
        if progress_callback is not None:
            if trial.user_attrs.get('valid'):
                progress_callback('Optuna V2 试验%d/%d：划分种子%s，开发R² %.6f，MAPE %.3f%%。'%(trial.number+1,n_trials,trial.params.get('split_seed'),trial.values[0],trial.values[1]*100.))
            else:progress_callback('Optuna V2 试验%d/%d失败：%s'%(trial.number+1,n_trials,trial.user_attrs.get('error','未知原因')))
    if search_driver is None:
        study.optimize(objective, n_trials=int(n_trials), n_jobs=1, show_progress_bar=False,
                       callbacks=[report_trial], timeout=timeout_seconds)
    else:
        study=search_driver(objective,study,timeout_seconds)
    trial_records = []
    for t in study.trials:
        rec = {"trial": t.number, "state": str(t.state), "r2": None,
               "mape": None, "valid": t.user_attrs.get("valid", False),
               "error": t.user_attrs.get("error", "")}
        if t.values is not None:
            rec["r2"], rec["mape"] = t.values
        if search_driver is not None:rec.update(phase=t.user_attrs.get('phase','history'),anchor=t.user_attrs.get('anchor'),
            evaluation_diagnostic=json.dumps(t.user_attrs.get('evaluation_diagnostic',{}),ensure_ascii=False),
            sampler_fallbacks=json.dumps(t.user_attrs.get('sampler_fallbacks',[]),ensure_ascii=False))
        rec.update(t.params)
        trial_records.append(rec)
    pd.DataFrame(trial_records).to_csv(os.path.join(output_dir, "optuna_trials.csv"), index=False,
                                        encoding="utf-8-sig")
    if search_driver is not None:
        pd.DataFrame(trial_records).to_csv(os.path.join(output_dir,'all_trials.csv'),index=False,encoding='utf-8-sig')
    chosen, feasible, choice_mode = _select_pareto(study, r2_target, mape_target)
    if history_identity is not None:
        from .search_history import save_history
        save_history(study,os.path.join(output_dir,'study_history.json'),history_identity,chosen.number)
    pareto_records = []
    for t in sorted(study.best_trials, key=lambda item: item.number):
        if t.user_attrs.get("valid"):
            pareto_records.append({"trial": t.number, "r2": t.values[0], "mape": t.values[1],
                                   "feasible": t.values[0] >= r2_target and t.values[1] <= mape_target,
                                   "selected": t.number == chosen.number, "split_seed": t.params["split_seed"]})
    pd.DataFrame(pareto_records).to_csv(os.path.join(output_dir, "pareto_front.csv"),
                                        index=False, encoding="utf-8-sig")
    best_cfg = chosen.params
    best_weights = normalized_weights(best_cfg)
    scores, validation_bundle, detail = _evaluate(best_cfg, X, y, source_rows, expert_map,
                                                   test_size, price_bins)
    Xtr, ytr, Xte, yte, tr_rows, te_rows, pred, components = detail
    validation_bundle['target']=target
    validation_bundle['selected_trial']=chosen.number
    validation_bundle['model_scope']='selected_searched_split'
    validation_bundle['validation_scores']=dict(scores)
    validation_model_path=os.path.join(output_dir,'selected_split_bundle.pkl')
    save_bundle(validation_bundle,validation_model_path)
    validation = pd.DataFrame({"source_excel_row": te_rows, "actual_price": yte,
                               "predicted_price": pred,
                               "absolute_percentage_error": np.abs(yte - pred) / yte})
    for name in MODEL_NAMES:
        validation["pred_" + name] = np.asarray(components[name])
    validation.to_csv(os.path.join(output_dir, "selected_split_predictions.csv"),
                      index=False, encoding="utf-8-sig")

    # Deploying on new rows: fit the chosen config once on ALL available records.
    # This bundle differs from the split-only validation fit; that's intentional.
    full_bundle = fit_ensemble(X, y, best_cfg, expert_map, best_weights)
    full_bundle["target"] = target
    full_bundle["selected_trial"] = chosen.number
    full_bundle["validation_scores"] = dict(scores)
    full_bundle["selected_split_seed"] = int(best_cfg["split_seed"])
    model_path = os.path.join(output_dir, "price_ensemble_bundle.pkl")
    save_bundle(full_bundle, model_path)
    # Reload check: preprocessor + expert feature-weight transformations remain identical.
    restored = load_bundle(model_path)
    first_n = min(3, len(X))
    check1, _ = predict_bundle(full_bundle, X.iloc[:first_n])
    check2, _ = predict_bundle(restored, X.iloc[:first_n])
    if not np.allclose(check1, check2, atol=1e-10, rtol=1e-10):
        raise AssertionError("Bundle reload changed predictions")

    result = {
        "input_file": os.path.abspath(data_path),
        "note": "R2/MAPE are searched-split validation scores; not an untouched test set.",
        "n_rows": len(y), "n_features": X.shape[1], "feature_names": list(X.columns),
        "n_trials": sum(t.state==optuna.trial.TrialState.COMPLETE for t in study.trials), "selected_trial": chosen.number,
        "selection_mode": choice_mode, "feasible_pareto_count": len(feasible),
        "r2_target": r2_target, "mape_target": mape_target,
        "selected_split_seed": int(best_cfg["split_seed"]),
        "validation_train_rows": len(ytr), "validation_test_rows": len(yte),
        "validation_metrics": scores, "ensemble_weights": dict(zip(MODEL_NAMES, best_weights.tolist())),
        "selected_parameters": dict(best_cfg), "saved_model": os.path.abspath(model_path),
        "saved_validation_model": os.path.abspath(validation_model_path),
        "training_full_data_model": True,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    if search_driver is not None:result.update(search_driver.summary(study,chosen))
    with open(os.path.join(output_dir, "best_config.json"), "w", encoding="utf-8") as f:
        json.dump(_json_ready(result), f, ensure_ascii=False, indent=2)
    if verbose:
        print("\nSelected trial:", chosen.number, "| Split seed:", best_cfg["split_seed"])
        print("Search-split R2: {:.6f} | MAPE: {:.2f}% | MAE: {:.6f}".format(
            scores["r2"], scores["mape"] * 100, scores["mae"]))
        print("Target reached:", scores["r2"] >= r2_target and scores["mape"] <= mape_target)
        print("Weights:", result["ensemble_weights"])
        print("Saved:", os.path.abspath(output_dir))
    return result


def run_prediction(bundle_path, data_path, out_path="new_predictions.csv"):
    bundle = load_bundle(bundle_path)
    data = read_table(data_path)
    price, components = predict_bundle(bundle, data)
    result = pd.DataFrame({"predicted_price": price})
    for name in MODEL_NAMES:
        result["pred_" + name] = np.asarray(components[name])
    out_folder = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(out_folder, exist_ok=True)
    result.to_csv(out_path, index=False, encoding="utf-8-sig")
    print("Wrote {} predictions to {}".format(len(result), out_path))
    return result


def build_parser():
    parser = argparse.ArgumentParser(description="Optuna joint search of seed, hyperparams and ensemble weights")
    sub = parser.add_subparsers(dest="action")
    tr = sub.add_parser("train")
    tr.add_argument("--data", default="增强样本(修改后).xlsx")
    tr.add_argument("--out", default="optuna_results")
    tr.add_argument("--target", default=TARGET_DEFAULT)
    tr.add_argument("--trials", type=int, default=150)
    tr.add_argument("--test-size", type=float, default=0.2)
    tr.add_argument("--price-bins", type=int, default=3)
    tr.add_argument("--r2-target", type=float, default=0.9)
    tr.add_argument("--mape-target", type=float, default=0.10)
    tr.add_argument("--seed-low", type=int, default=1)
    tr.add_argument("--seed-high", type=int, default=10000)
    tr.add_argument("--optimizer-seed", type=int, default=42)
    tr.add_argument("--include-first-column", action="store_true",
                    help="Original notebook drops the first column (row ID) by default")
    tr.add_argument("--remove-outliers", action="store_true",
                    help="Optionally reproduce outlier filtering (on input features)")
    pr = sub.add_parser("predict")
    pr.add_argument("--model", required=True)
    pr.add_argument("--data", required=True)
    pr.add_argument("--out", default="new_predictions.csv")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.action == "train":
        run_training(data_path=args.data, output_dir=args.out, n_trials=args.trials,
                     target=args.target, test_size=args.test_size,
                     price_bins=args.price_bins, r2_target=args.r2_target,
                     mape_target=args.mape_target, seed_low=args.seed_low,
                     seed_high=args.seed_high, optimizer_seed=args.optimizer_seed,
                     drop_first=not args.include_first_column,
                     remove_outliers=args.remove_outliers)
    elif args.action == "predict":
        run_prediction(args.model, args.data, args.out)
    else:
        build_parser().print_help()


if __name__ == "__main__":
    main()
