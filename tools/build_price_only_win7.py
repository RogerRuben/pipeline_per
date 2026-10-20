# -*- coding: utf-8 -*-
"""Build a price-model-only offline delivery ZIP for 64-bit Windows 7."""
from __future__ import print_function

import argparse
import datetime
import hashlib
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "README_PRICE_MODEL_ONLY_WIN7.md", "VERSION.txt",
    "BUILD_PRICE_ONLY_WIN7.bat", "CREATE_PRICE_RUNTIME_ENV_WIN7.bat",
    "CREATE_PRICE_TRAINING_ENV_PY38.bat",
    "CREATE_AUTOML_ENV_PY38_WIN7.bat", "DOWNLOAD_AUTOML_024_WHEELS_PY38_WIN64.bat",
    "DOWNLOAD_PRICE_TRAINING_WHEELS_PY38_WIN64.bat", "RUN_AUTOML_024_WIN7.bat",
    "START_PRICE_MODEL_WIN7.bat", "TRAIN_PRICE_SERVICE_MODEL_WIN7.bat",
    "VERIFY_PRICE_ONLY_WIN7.bat",
)
DIR_FILES = (
    "services/common/__init__.py", "services/common/http_service.py",
    "services/price_service/__init__.py", "services/price_service/app.py",
    "services/price_service/export_native_price_bundle.py",
    "services/price_service/native_bundle.py",
    "services/price_service/ensemble_estimators.py",
    "services/price_service/requirements_win7_exact.txt",
    "services/price_service/requirements_training_py38.txt",
    "services/price_service/requirements_automl_024_py38_win7.txt",
    "services/price_service/model/price_native_bundle.pkl",
    "services/price_service/model/price_native_bundle.pkl.manifest.json",
    "tools/train_price_service_model.py", "tools/verify_price_only_environment.py", "tools/build_price_only_win7.py",
    "tools/prepare_automl_sources.py", "tools/automl",
    "vendor/automl_sklearn_021_024_bundle.zip",
    "app/__init__.py", "app/historical_onboarding.py", "app/wide_import.py",
    "docs/PRICE_TRAINING_AND_OFFLINE_ENVIRONMENT.md",
)
DIRS = (
    "services/price_service/wheelhouse_win7",
    "services/price_service/wheelhouse_automl024_win7",
)
EXCLUDED_PARTS = {"__pycache__", ".pytest_cache", ".git", "venv", ".venv", "logs"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".log"}


def should_skip(path):
    return any(part in EXCLUDED_PARTS for part in path.parts) or path.suffix.lower() in EXCLUDED_SUFFIXES


def copy_one(stage, relative):
    source = ROOT / relative
    if not source.exists():
        raise FileNotFoundError("价格专用包缺少文件: %s" % source)
    if source.is_dir():
        for child in source.rglob("*"):
            if should_skip(child) or not child.is_file():
                continue
            target = stage / child.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(child), str(target))
    else:
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(source), str(target))


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def make_zip(stage, target):
    with zipfile.ZipFile(str(target), "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                archive.write(str(path), str(path.relative_to(stage)).replace(os.sep, "/"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=str(ROOT / "deliverables" / "price_only_win7"))
    parser.add_argument("--name", default="")
    args = parser.parse_args()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    name = args.name or ("IndustrialProtocolDemo_V19_6_14_PRICE_ONLY_WIN7_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    stage = output_dir / ("." + name + ".stage")
    target = output_dir / (name + ".zip")
    for path in (stage, target, Path(str(target) + ".sha256"), Path(str(target) + ".json")):
        if path.is_dir():
            shutil.rmtree(str(path))
        elif path.exists():
            path.unlink()
    stage.mkdir(parents=True)
    for relative in FILES + DIR_FILES + DIRS:
        copy_one(stage, relative)
    (stage / "runtime").mkdir(parents=True, exist_ok=True)
    (stage / "runtime" / "README_WIN7.txt").write_text(
        "此目录用于目标机创建价格专用虚拟环境。\n"
        "目标机需要预先安装 64 位 CPython 3.8。\n", encoding="utf-8"
    )
    make_zip(stage, target)
    digest = sha256(target)
    Path(str(target) + ".sha256").write_text(digest + "  " + target.name + "\n", encoding="ascii")
    manifest = {
        "status": "PASS", "package": str(target), "sha256": digest,
        "size": target.stat().st_size,
        "python_requirement": "CPython 3.8 x64 on Windows 7",
        "scope": "price-model-only",
        "includes": ["native price bundle", "price inference runtime wheelhouse", "price training wheelhouse", "AutoML sklearn 0.21/0.24 sources and 0.24 wheelhouse"],
        "excludes": ["effectiveness service", "recommendation UI", "main application", "effectiveness model"],
    }
    Path(str(target) + ".json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.rmtree(str(stage))
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("[FAIL] %s" % exc, file=sys.stderr)
        raise SystemExit(1)
