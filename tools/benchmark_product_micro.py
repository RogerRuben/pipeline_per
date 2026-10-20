# -*- coding: utf-8 -*-
"""Predeclared 10x40 scenarios; external records never enter fitting."""
from __future__ import print_function
import argparse
import copy
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.console_compat import enable_plain_console
enable_plain_console()
import numpy as np
from app.historical_onboarding import HistoricalProductOnboarding
from tools.product_pipeline import fixture_csv
from tools.product_price_automl_v2 import train_price_v2
from tools.product_price_automl import metrics, write_json
from services.price_service.native_bundle import predict_batch

SCENARIOS = ("expert_cost", "latent", "sparse_known", "noise", "wrong_expert")


def scenario(name, seed, external_rows=256):
    rng = np.random.RandomState(seed)
    count = 10 + external_rows
    keys = ["attr_%02d" % i for i in range(40)]
    if name == "latent":
        z = rng.normal(size=(count, 2))
        loading = np.random.RandomState(991).normal(size=(2, 40))
        X = z.dot(loading) + rng.normal(scale=.1, size=(count, 40))
        y = 12 + 2 * z[:, 0] - z[:, 1] + rng.normal(scale=.2, size=count)
    else:
        X = rng.normal(size=(count, 40))
        y = 12 + 2 * X[:, 0] - X[:, 1] + rng.normal(scale=.2, size=count)
    if name == "noise":
        y = 15 + rng.normal(scale=.5, size=count)
    y = np.maximum(y, .25)
    rows = [["B%03d" % i, "对照样本%02d" % i, round(float(y[i]), 6)] + np.round(X[i], 6).tolist() for i in range(10)]
    fixture = {"labels": ["成品编号", "成品名称", "价格(万元)"] + ["数值属性%02d" % i for i in range(40)],
               "field_ids": ["product_id", "product_name", "price_wan"] + keys, "rows": rows}
    data = HistoricalProductOnboarding().analyze("benchmark.csv", fixture_csv(fixture), "MICRO_BENCH", "微样本对照", require_dual_header=True)["data"]
    config = json.loads((ROOT / "tests/fixtures/product_pipeline_v2/price_config.json").read_text(encoding="utf-8"))
    config["experts_from_workbook"] = False
    config["models"] = [m for m in config["models"] if not m.startswith("engineering")]
    config["engineering"] = {"intercept_wan": 0., "coefficients": {}}
    config["pipeline"]["interaction_pairs"] = []
    config["resources"]["total_seconds"] = 900
    config["ensemble"]["family_priors"] = [{"family": "linear", "weight": .5, "confidence": .5},
                                            {"family": "tree", "weight": .3, "confidence": .5},
                                            {"family": "kernel", "weight": .2, "confidence": .5}]
    if name in ("expert_cost", "wrong_expert"):
        config["models"] += ["engineering_cost", "engineering_residual"]
        config["engineering"] = {"intercept_wan": 12., "coefficients": {keys[0]: 2., keys[1]: -1.}}
        if name == "wrong_expert":
            config["engineering"] = {"intercept_wan": 50., "coefficients": {keys[0]: 8.}}
        config["ensemble"]["family_priors"] = [{"family": "engineering", "weight": .35, "confidence": .85},
                                                {"family": "linear", "weight": .35, "confidence": .8},
                                                {"family": "tree", "weight": .2, "confidence": .7},
                                                {"family": "kernel", "weight": .1, "confidence": .65}]
    config["small_sample"] = {"policy": "force", "expert_features": keys[:2] if name == "sparse_known" else []}
    external = [dict(zip(keys, row.tolist())) for row in X[10:]]
    return data, config, external, y[10:]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--seeds", default="41,73")
    parser.add_argument("--scenarios", default=",".join(SCENARIOS))
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if (ROOT / "outputs").resolve() not in output.parents:
        raise ValueError("Benchmark output must stay under workspace outputs")
    output.mkdir(parents=True, exist_ok=True)
    seeds = [int(s) for s in args.seeds.split(",")]
    names = args.scenarios.split(",")
    results = []
    for name in names:
        for seed in seeds:
            data, configured, external, truth = scenario(name, seed)
            for policy in ("off", "force"):
                directory = output / ("%s_%s_%d" % (name, policy, seed))
                if (directory / "benchmark_result.json").is_file():
                    result = json.loads((directory / "benchmark_result.json").read_text(encoding="utf-8"))
                else:
                    config = copy.deepcopy(configured); config["small_sample"]["policy"] = policy
                    bundle, report = train_price_v2(data, directory, config_overrides=config, progress=lambda message: None)
                    predicted = np.asarray([row["predicted_price_wan"] for row in predict_batch(bundle, external)])
                    result = {"scenario": name, "data_seed": seed, "policy": policy, "external_rows": len(truth),
                              "external_metrics": metrics(truth, predicted), "nested_cv": report["nested_cv_metrics"],
                              "holdout": report["holdout_metrics"], "seconds": report["elapsed_seconds"],
                              "expert_shares": {g["family"]: g["learned_weight"] for g in report["fusion"].get("expert_groups", [])}}
                    write_json(directory / "benchmark_result.json", result)
                results.append(result)
                write_json(output / "results.json", {"predeclared_scenarios": names, "data_seeds": seeds,
                           "training_records": 10, "raw_attributes": 40, "results": results,
                           "external_data_used_for_selection": False,
                           "note": "合成机制对照，不代表真实精度；sparse_known为R3新增结构先验能力，需单独理解信息优势。"})
                print("%s seed=%d %s: external MAPE %.3f%%, R2 %.3f, %.1fs" %
                      (name, seed, policy, result["external_metrics"]["mape_percent"], result["external_metrics"]["r2"], result["seconds"]), flush=True)


if __name__ == "__main__":
    main()
