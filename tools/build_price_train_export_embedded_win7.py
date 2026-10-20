# -*- coding: utf-8 -*-
"""Build a self-contained price-training package with embedded Python 3.8."""
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
EMBED_CANDIDATES = (
    Path(r"C:\Users\HP\.codex\worktrees\8a8d\industrialprotol_deepseek_verse\offline_assets\python38_price_sklearn024"),
    Path(r"C:\Users\HP\.codex\worktrees\8a8d\industrialprotol_deepseek_verse\offline_assets\python38_pyinstaller"),
)
SITE_SOURCE = ROOT / "runtime" / "venvs" / "price_training38" / "Lib" / "site-packages"
SITE_ITEMS = (
    "numpy", "numpy-1.24.4.dist-info", "scipy", "scipy-1.10.1.dist-info", "scipy.libs",
    "pandas", "pandas-1.5.3.dist-info", "sklearn", "scikit_learn-1.3.2.dist-info",
    "joblib", "joblib-1.4.2.dist-info", "threadpoolctl.py", "threadpoolctl-3.5.0.dist-info",
    "dateutil", "python_dateutil-2.9.0.post0.dist-info", "pytz", "pytz-2026.3.post1.dist-info",
    "six.py", "six-1.17.0.dist-info", "packaging", "packaging-26.2.dist-info",
)
FILES = (
    "README_PRICE_TRAIN_EXPORT_ONLY_WIN7.md", "VERSION.txt",
    "PRICE_TRAIN_AND_EXPORT_WIN7.bat", "sample_virtual_price_history.csv",
    "tools/train_price_service_model.py",
    "services/price_service/__init__.py", "services/price_service/export_native_price_bundle.py",
    "services/price_service/native_bundle.py", "services/price_service/requirements_price_train_only_py38.txt",
    "services/price_service/model/README_PRICE_TRAIN_EXPORT_ONLY.txt",
    "app/__init__.py", "app/historical_onboarding.py", "app/wide_import.py",
)
EXCLUDED_PARTS = {"__pycache__", ".pytest_cache"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".pdb"}


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


def copy_tree(source, target):
    source = Path(source)
    if source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(source), str(target))
        return
    for child in source.rglob("*"):
        if not child.is_file() or any(part in EXCLUDED_PARTS for part in child.parts) or child.suffix.lower() in EXCLUDED_SUFFIXES:
            continue
        destination = target / child.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(child), str(destination))


def locate_embed_source():
    for candidate in EMBED_CANDIDATES:
        if (candidate / "python.exe").is_file() and (candidate / "python38.zip").is_file():
            return candidate
    raise FileNotFoundError("找不到可携带 Python 3.8.10 运行时")


def copy_embedded_runtime(stage):
    source = locate_embed_source()
    if not SITE_SOURCE.is_dir():
        raise FileNotFoundError("缺少已安装的价格训练 site-packages: %s" % SITE_SOURCE)
    target = stage / "runtime" / "python38"
    target.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        if child.is_file() and (child.suffix.lower() in (".dll", ".pyd") or child.name in (
            "python.exe", "pythonw.exe", "python3.dll", "python38.dll", "python38._pth", "python38.zip", "LICENSE.txt"
        )):
            shutil.copy2(str(child), str(target / child.name))
    site_target = target / "Lib" / "site-packages"
    for relative in SITE_ITEMS:
        source_item = SITE_SOURCE / relative
        if not source_item.exists():
            raise FileNotFoundError("嵌入环境缺少依赖: %s" % source_item)
        copy_tree(source_item, site_target / relative)
    manifest = {
        "python": "3.8.10",
        "architecture": "win_amd64",
        "site_packages_source": "embedded price-training runtime",
        "packages": {"numpy": "1.24.4", "scipy": "1.10.1", "pandas": "1.5.3", "scikit-learn": "1.3.2", "joblib": "1.4.2", "threadpoolctl": "3.5.0", "python-dateutil": "2.9.0.post0", "pytz": "2026.3.post1", "six": "1.17.0", "packaging": "26.2"},
    }
    (target / "PRICE_TRAINING_RUNTIME_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=str(ROOT / "deliverables" / "price_train_export_only_win7"))
    parser.add_argument("--name", default="IndustrialProtocolDemo_V19_6_14_PRICE_TRAIN_EXPORT_ONLY_WIN7")
    args = parser.parse_args()
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    name = args.name
    stage = Path(r"D:\\pt_price_train_stage")
    target = output / (name + ".zip")
    for path in (stage, target, Path(str(target) + ".sha256"), Path(str(target) + ".json")):
        if path.is_dir():
            shutil.rmtree(str(path))
        elif path.exists():
            path.unlink()
    stage.mkdir(parents=True)
    for relative in FILES:
        copy_file(stage, relative)
    runtime_manifest = copy_embedded_runtime(stage)
    (stage / "runtime" / "README_WIN7.txt").write_text(
        "本包自带 Python 3.8.10 x64 和价格训练依赖，目标机无需安装 Python 或 pip。\n", encoding="utf-8"
    )
    with zipfile.ZipFile(str(target), "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                archive.write(str(path), str(path.relative_to(stage)).replace(os.sep, "/"))
    digest = sha256(target)
    Path(str(target) + ".sha256").write_text(digest + "  " + target.name + "\n", encoding="ascii")
    manifest = {
        "status": "PASS", "package": str(target), "sha256": digest, "size": target.stat().st_size,
        "scope": "self-contained-price-training-and-export-only",
        "runtime": runtime_manifest,
        "includes": ["embedded CPython 3.8.10 x64", "installed price training libraries", "one training/export BAT", "virtual CSV acceptance data"],
        "excludes": ["online service", "AutoML", "Notebook", "effectiveness model", "main application"],
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
