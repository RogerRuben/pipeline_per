# -*- coding: utf-8 -*-
"""Verify the offline Win7 package without importing model pickle files."""
from __future__ import print_function

import argparse
import hashlib
import importlib
import json
import struct
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "runtime": {
        "numpy": "1.24.4", "scipy": "1.10.1", "openpyxl": "3.1.3",
        "sklearn": "1.3.2", "xgboost": "1.7.6", "joblib": "1.4.2",
        "threadpoolctl": "3.5.0",
    },
    "training": {
        "numpy": "1.24.4", "scipy": "1.10.1", "openpyxl": "3.1.3",
        "sklearn": "1.3.2", "xgboost": "1.7.6", "joblib": "1.4.2",
        "threadpoolctl": "3.5.0", "pandas": "1.5.3", "matplotlib": "3.7.5",
        "seaborn": "0.13.2", "notebook": "6.5.6", "ipykernel": "6.29.5",
    },
    "automl024": {
        "numpy": "1.19.5", "scipy": "1.5.4", "pandas": "1.3.5",
        "sklearn": "0.24.2", "joblib": "1.0.1", "threadpoolctl": "2.1.0",
        "optuna": "3.6.0",
    },
}


def version_of(name):
    module = importlib.import_module(name)
    return str(getattr(module, "__version__", "unknown"))


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def check_manifest(path, errors):
    path = Path(path)
    if not path.is_file():
        errors.append("缺少 wheelhouse manifest: %s" % path)
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        errors.append("manifest 无法读取 %s: %s" % (path, exc))
        return None
    for item in raw.get("files", []):
        wheel = path.parent / item.get("name", "")
        if not wheel.is_file():
            errors.append("manifest 中缺少文件: %s" % wheel)
            continue
        if int(item.get("size", -1)) != wheel.stat().st_size:
            errors.append("文件大小不匹配: %s" % wheel)
        if item.get("sha256") and item["sha256"] != sha256(wheel):
            errors.append("SHA-256 不匹配: %s" % wheel)
    return raw


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=sorted(EXPECTED), required=True)
    parser.add_argument("--check-wheelhouses", action="store_true")
    parser.add_argument("--check-automl-bundle", action="store_true")
    args = parser.parse_args()
    errors = []
    warnings = []
    report = {
        "profile": args.profile,
        "python": sys.version,
        "executable": sys.executable,
        "architecture_bits": struct.calcsize("P") * 8,
        "versions": {},
        "errors": errors,
        "warnings": warnings,
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
    if args.check_wheelhouses:
        report["wheelhouses"] = {
            "price": check_manifest(ROOT / "services" / "price_service" / "wheelhouse_win7" / "WHEELHOUSE_MANIFEST.json", errors),
            "effectiveness": check_manifest(ROOT / "services" / "effectiveness_service" / "wheelhouse_win7" / "WHEELHOUSE_MANIFEST.json", errors),
            "automl024": check_manifest(ROOT / "services" / "price_service" / "wheelhouse_automl024_win7" / "WHEELHOUSE_MANIFEST.json", errors),
        }
    if args.check_automl_bundle:
        bundle = ROOT / "vendor" / "automl_sklearn_021_024_bundle.zip"
        if not bundle.is_file():
            errors.append("缺少 AutoML bundle: %s" % bundle)
        else:
            report["automl_bundle"] = {"path": str(bundle), "sha256": sha256(bundle), "size": bundle.stat().st_size}
            try:
                with zipfile.ZipFile(str(bundle), "r") as archive:
                    names = set(archive.namelist())
                    for required in ("sklearn_0_21/automl_search.py", "sklearn_0_24/automl_search.py"):
                        if required not in names:
                            errors.append("AutoML bundle 缺少: %s" % required)
            except Exception as exc:
                errors.append("AutoML bundle 无法读取: %s" % exc)
    report["status"] = "PASS" if not errors else "FAIL"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if not errors else 1)


if __name__ == "__main__":
    main()
