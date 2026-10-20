# -*- coding: utf-8 -*-
"""Build the minimal price-training-and-export-only Win7 ZIP."""
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
    "README_PRICE_TRAIN_EXPORT_ONLY_WIN7.md", "VERSION.txt",
    "PRICE_TRAIN_AND_EXPORT_WIN7.bat", "sample_virtual_price_history.csv",
    "tools/train_price_service_model.py",
    "services/price_service/__init__.py", "services/price_service/export_native_price_bundle.py",
    "services/price_service/native_bundle.py", "services/price_service/requirements_price_train_only_py38.txt",
    "services/price_service/ensemble_estimators.py",
    "services/price_service/model/README_PRICE_TRAIN_EXPORT_ONLY.txt",
    "app/__init__.py", "app/historical_onboarding.py", "app/wide_import.py",
)
WHEELS = (
    "numpy-1.24.4-cp38-cp38-win_amd64.whl",
    "scipy-1.10.1-cp38-cp38-win_amd64.whl",
    "pandas-1.5.3-cp38-cp38-win_amd64.whl",
    "scikit_learn-1.3.2-cp38-cp38-win_amd64.whl",
    "joblib-1.4.2-py3-none-any.whl",
    "threadpoolctl-3.5.0-py3-none-any.whl",
    "python_dateutil-2.9.0.post0-py2.py3-none-any.whl",
    "pytz-2026.3.post1-py2.py3-none-any.whl",
    "six-1.17.0-py2.py3-none-any.whl",
)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def copy_file(stage, relative):
    source = ROOT / relative
    if not source.is_file():
        raise FileNotFoundError("缺少文件: %s" % source)
    target = stage / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(source), str(target))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=str(ROOT / "deliverables" / "price_train_export_only_win7"))
    parser.add_argument("--name", default="")
    args = parser.parse_args()
    out = Path(args.output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    name = args.name or ("IndustrialProtocolDemo_V19_6_14_PRICE_TRAIN_EXPORT_ONLY_WIN7_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    stage = out / "._price_train_export_stage"
    target = out / (name + ".zip")
    for path in (stage, target, Path(str(target) + ".sha256"), Path(str(target) + ".json")):
        if path.is_dir():
            shutil.rmtree(str(path))
        elif path.exists():
            path.unlink()
    stage.mkdir(parents=True)
    for relative in FILES:
        copy_file(stage, relative)
    wheel_source = ROOT / "services" / "price_service" / "wheelhouse_win7"
    wheel_target = stage / "services" / "price_service" / "wheelhouse_price_train_only_win7"
    wheel_target.mkdir(parents=True, exist_ok=True)
    manifest_files = []
    for name_item in WHEELS:
        source = wheel_source / name_item
        if not source.is_file():
            raise FileNotFoundError("缺少训练 wheel: %s" % source)
        target_item = wheel_target / name_item
        shutil.copy2(str(source), str(target_item))
        manifest_files.append({"name": name_item, "size": target_item.stat().st_size, "sha256": sha256(target_item)})
    (wheel_target / "WHEELHOUSE_MANIFEST.json").write_text(json.dumps({
        "format_version": "price-train-only-wheelhouse-1.0",
        "target": {"python": "3.8", "implementation": "CPython", "platform": "win_amd64"},
        "files": manifest_files,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (stage / "runtime").mkdir(parents=True, exist_ok=True)
    (stage / "runtime" / "README_WIN7.txt").write_text("用于创建价格训练虚拟环境。\n", encoding="utf-8")
    with zipfile.ZipFile(str(target), "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                archive.write(str(path), str(path.relative_to(stage)).replace(os.sep, "/"))
    digest = sha256(target)
    Path(str(target) + ".sha256").write_text(digest + "  " + target.name + "\n", encoding="ascii")
    manifest = {"status": "PASS", "package": str(target), "sha256": digest, "size": target.stat().st_size,
                "scope": "price-training-and-export-only", "python_requirement": "CPython 3.8 x64 on Windows 7",
                "wheel_count": len(WHEELS), "includes": ["one training/export BAT", "price training CLI", "minimal offline dependencies", "virtual CSV acceptance data"],
                "excludes": ["online service", "AutoML", "Notebook", "effectiveness model", "main application"]}
    Path(str(target) + ".json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.rmtree(str(stage))
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("[FAIL] %s" % exc, file=sys.stderr)
        raise SystemExit(1)
