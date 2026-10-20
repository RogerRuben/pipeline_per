# -*- coding: utf-8 -*-
"""Evaluate a frozen bundle on predeclared independent virtual draws."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.historical_onboarding import HistoricalProductOnboarding
from services.price_service.native_bundle import load_bundle, predict_batch
from tools.product_pipeline import fixture_csv
from tools.product_v2_fixture import tiny_fixture
from tools.product_price_automl import metrics, write_json

TEST_SEEDS = [2026101401, 2026101402, 2026101403, 2026101404, 2026101405, 2026101406]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--baseline", default=str(ROOT / "outputs" / "product_tabpfn_attribute_scores" / "price" / "price_native_bundle.pkl"))
    args = parser.parse_args()
    output = Path(args.output).resolve()
    training = json.loads((output / "price" / "training_report.json").read_text(encoding="utf-8"))
    model_path = output / "price" / "price_native_bundle.pkl"
    frozen_sha = hashlib.sha256(model_path.read_bytes()).hexdigest()
    manifest = json.loads((output / "prototype_manifest.json").read_text(encoding="utf-8"))
    assert manifest["artifact_sha256"]["price_model"] == frozen_sha
    rows, targets, raw_files = [], [], []
    directory = output / "independent_virtual_test"
    directory.mkdir(exist_ok=True)
    for seed in TEST_SEEDS:
        raw = fixture_csv(tiny_fixture(seed=seed))
        path = directory / ("seed_%d.csv" % seed)
        path.write_bytes(raw)
        data = HistoricalProductOnboarding().analyze(path.name, raw, manifest["product_code"], manifest["product_name"], ["-1", "\\", "/"])["data"]
        for record in data["agreements"]:
            rows.append(record["params"]); targets.append(record["historical_price_wan"])
        raw_files.append({"path": path.name, "sha256": hashlib.sha256(raw).hexdigest()})
    results = {}
    for name, path in [("corrected", model_path), ("previous_failed", Path(args.baseline))]:
        if not path.is_file():
            continue
        bundle = load_bundle(path)
        predicted = [item["predicted_price_wan"] for item in predict_batch(bundle, rows)]
        score = metrics(targets, predicted)
        per_seed = [dict(seed=seed, **metrics(targets[i * 10:(i + 1) * 10], predicted[i * 10:(i + 1) * 10])) for i, seed in enumerate(TEST_SEEDS)]
        results[name] = {"metrics": score, "per_seed": per_seed, "predictions_wan": predicted}
    assert hashlib.sha256(model_path.read_bytes()).hexdigest() == frozen_sha
    q = training["targets"]
    corrected = results["corrected"]["metrics"]
    per_seed_passed = all(row["mape_percent"] <= q["mape_percent_max"] and row["r2"] >= q["r2_min"] for row in results["corrected"]["per_seed"])
    passed = training["targets_met"] and corrected["mape_percent"] <= q["mape_percent_max"] and corrected["r2"] >= q["r2_min"] and per_seed_passed
    report = {"status": "PASS" if passed else "FAIL", "training_records": 10, "features": 50, "test_records": len(rows),
              "seeds": TEST_SEEDS, "model_frozen_before_test": True, "model_sha256": frozen_sha, "source_files": raw_files,
              "same_virtual_generator_unmodified": True, "test_used_for_selection": False, "targets": q,
              "nested_training_metrics": training["nested_cv_metrics"], "results": results,
              "per_seed_targets_met": per_seed_passed, "business_accuracy_certified": False,
              "interpretation": "Fresh virtual draws test the frozen model under the existing generator; real-data accuracy is not certified."}
    write_json(output / "virtual_generalization_report.json", report)
    print(json.dumps({"status": report["status"], "nested": report["nested_training_metrics"], "independent": {key: value["metrics"] for key, value in results.items()}}, ensure_ascii=False, indent=2))
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
