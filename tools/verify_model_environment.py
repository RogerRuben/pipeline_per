# -*- coding: utf-8 -*-
"""Verify the isolated Python 3.8 model runtime or price-training environment."""
from __future__ import print_function

import argparse
import importlib
import json
import struct
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


EXPECTED = {
    "numpy": "1.24.4",
    "scipy": "1.10.1",
    "openpyxl": "3.1.3",
    "sklearn": "1.3.2",
    "xgboost": "1.7.6",
    "joblib": "1.4.2",
    "threadpoolctl": "3.5.0",
    "pandas": "1.5.3",
    "matplotlib": "3.7.5",
    "seaborn": "0.13.2",
    "notebook": "6.5.6",
    "ipykernel": "6.29.5",
}


def module_version(name):
    module = importlib.import_module(name)
    return str(getattr(module, "__version__", "unknown"))


def verify_modules(profile):
    required = [
        "numpy", "scipy", "openpyxl", "sklearn", "xgboost",
        "joblib", "threadpoolctl",
    ]
    if profile == "training":
        required.extend([
            "pandas", "matplotlib", "seaborn", "notebook", "ipykernel",
        ])
    versions = {}
    errors = []
    for name in required:
        try:
            actual = module_version(name)
            versions[name] = actual
            expected = EXPECTED.get(name)
            if expected and actual != expected:
                errors.append("%s版本应为%s，实际为%s" % (name, expected, actual))
        except Exception as exc:
            errors.append("%s导入失败: %s" % (name, exc))
    return versions, errors


def smoke_current_models():
    from services.effectiveness_service.app import backend_from_package
    from services.price_service.app import PriceService

    price_path = ROOT / "services" / "price_service" / "model" / "price_native_bundle.pkl"
    effect_path = (
        ROOT / "services" / "effectiveness_service" / "model" / "current"
        / "effectiveness_runtime_manifest.json"
    )
    errors = []
    warnings = []
    result = {}
    try:
        price = PriceService(price_path, fallback_json=None)
        result["price"] = price.health()
        if price.backend != "native_pickle":
            errors.append("当前价格服务不是native_pickle")
        price._one(price.example_request())
    except Exception as exc:
        errors.append("当前价格模型冒烟失败: %s" % exc)
    try:
        effect = backend_from_package(effect_path)
        result["effectiveness"] = {
            "backend": effect.name,
            "product_code": effect.product_code,
            "model_version": effect.model_version,
        }
        schema = effect.schema()
        sample = {}
        for field in schema.get("fields") or []:
            allowed = field.get("allowed_values") or []
            lo, hi = field.get("generation_min"), field.get("generation_max")
            if allowed:
                value = allowed[0]
            elif lo is not None and hi is not None:
                value = (float(lo) + float(hi)) / 2.0
            elif field.get("default_value") is not None:
                value = field.get("default_value")
            elif lo is not None:
                value = lo
            else:
                value = 0
            sample[field.get("field_name")] = value
        evaluated = effect.evaluate(sample)
        if evaluated.get("effectiveness_score") is None:
            errors.append("当前效能模型实算没有返回效能分")
    except Exception as exc:
        errors.append("当前效能模型冒烟失败: %s" % exc)
    if (
        result.get("price", {}).get("product_code")
        and result.get("effectiveness", {}).get("product_code")
        and result["price"]["product_code"] != result["effectiveness"]["product_code"]
    ):
        warnings.append("价格与效能服务声明的product_code不一致；实际HTTP返回字段正确时仍允许运行")
    return result, errors, warnings


def main():
    parser = argparse.ArgumentParser(description="验证模型Python隔离环境")
    parser.add_argument("--profile", choices=("runtime", "training"), required=True)
    parser.add_argument("--smoke-current-models", action="store_true")
    args = parser.parse_args()
    report = {
        "profile": args.profile,
        "python": sys.version,
        "executable": sys.executable,
        "architecture_bits": struct.calcsize("P") * 8,
        "versions": {},
        "models": None,
        "errors": [],
        "warnings": [],
    }
    if sys.version_info[:2] != (3, 8):
        report["errors"].append("必须使用Python 3.8")
    if struct.calcsize("P") * 8 != 64:
        report["errors"].append("必须使用64位Python")
    versions, errors = verify_modules(args.profile)
    report["versions"] = versions
    report["errors"].extend(errors)
    if args.smoke_current_models:
        models, model_errors, model_warnings = smoke_current_models()
        report["models"] = models
        report["errors"].extend(model_errors)
        report["warnings"].extend(model_warnings)
    report["status"] = "PASS" if not report["errors"] else "FAIL"
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    raise SystemExit(0 if report["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
