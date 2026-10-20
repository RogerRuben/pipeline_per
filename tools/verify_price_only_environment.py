# -*- coding: utf-8 -*-
"""Verify the price-only Win7 runtime and native price bundle."""
from __future__ import print_function

import argparse
import hashlib
import importlib
import json
import struct
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
EXPECTED = {
    "runtime": {
        "numpy": "1.24.4", "scipy": "1.10.1", "joblib": "1.4.2",
        "threadpoolctl": "3.5.0", "sklearn": "1.3.2", "xgboost": "1.7.6",
    },
    "training": {
        "numpy": "1.24.4", "scipy": "1.10.1", "joblib": "1.4.2",
        "threadpoolctl": "3.5.0", "sklearn": "1.3.2", "xgboost": "1.7.6",
        "pandas": "1.5.3", "openpyxl": "3.1.3", "matplotlib": "3.7.5",
        "seaborn": "0.13.2", "notebook": "6.5.6", "ipykernel": "6.29.5",
    },
    "automl024": {
        "numpy": "1.19.5", "scipy": "1.5.4", "pandas": "1.3.5",
        "sklearn": "0.24.2", "joblib": "1.0.1", "threadpoolctl": "2.1.0",
        "optuna": "3.6.0",
    },
}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def version_of(name):
    module = importlib.import_module(name)
    return str(getattr(module, "__version__", "unknown"))


def check_manifest(path, errors):
    path = Path(path)
    if not path.is_file():
        errors.append("缺少 wheelhouse manifest: %s" % path)
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    for item in raw.get("files", []):
        wheel = path.parent / item.get("name", "")
        if not wheel.is_file():
            errors.append("manifest 中缺少文件: %s" % wheel)
        elif int(item.get("size", -1)) != wheel.stat().st_size:
            errors.append("文件大小不匹配: %s" % wheel)
        elif item.get("sha256") and item["sha256"] != sha256(wheel):
            errors.append("SHA-256 不匹配: %s" % wheel)
    return raw


def smoke_model(errors, report):
    try:
        from services.price_service.native_bundle import load_bundle, predict
        from services.price_service.app import PriceService
        path = ROOT / "services" / "price_service" / "model" / "price_native_bundle.pkl"
        bundle = load_bundle(path)
        service = PriceService(model_path=path, fallback_json=None)
        request = service.example_request()
        result = predict(bundle, request["parameters"])
        report["model"] = {
            "path": str(path), "sha256": sha256(path),
            "product_code": bundle.get("product_code"),
            "model_version": bundle.get("model_version"),
            "model_names": service.schema().get("model_names"),
            "example_prediction_wan": result.get("predicted_price_wan"),
        }
    except Exception as exc:
        errors.append("原生价格模型 smoke test 失败: %s" % exc)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=sorted(EXPECTED), required=True)
    parser.add_argument("--check-wheelhouse", action="store_true")
    parser.add_argument("--smoke-model", action="store_true")
    args = parser.parse_args()
    errors = []
    report = {
        "profile": args.profile, "python": sys.version,
        "executable": sys.executable, "architecture_bits": struct.calcsize("P") * 8,
        "versions": {}, "errors": errors,
    }
    if sys.version_info[:2] != (3, 8):
        errors.append("必须使用 Python 3.8；当前为 %s" % (sys.version_info[:2],))
    if struct.calcsize("P") * 8 != 64:
        errors.append("必须使用 64 位 Python")
    for name, expected in EXPECTED[args.profile].items():
        try:
            actual = version_of(name)
            report["versions"][name] = actual
            if actual != expected:
                errors.append("%s 版本应为 %s，实际为 %s" % (name, expected, actual))
        except Exception as exc:
            errors.append("%s 导入失败: %s" % (name, exc))
    if args.check_wheelhouse:
        report["wheelhouse"] = check_manifest(
            ROOT / "services" / "price_service" / "wheelhouse_win7" / "WHEELHOUSE_MANIFEST.json", errors
        )
    if args.smoke_model:
        smoke_model(errors, report)
    report["status"] = "PASS" if not errors else "FAIL"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if not errors else 1)


if __name__ == "__main__":
    main()
