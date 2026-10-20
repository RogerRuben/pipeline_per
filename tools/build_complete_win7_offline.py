# -*- coding: utf-8 -*-
"""Build a self-contained source + wheelhouse delivery ZIP for Win7."""
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
INCLUDE_DIRS = ("app", "config", "data", "data_master", "deploy", "docs", "models", "services", "tests", "tools", "vendor")
INCLUDE_ROOT_FILES = ("README.md", "VERSION.txt", "requirements.txt", "*.bat", "*.md", "*.ipynb")
EXCLUDED_NAMES = {"__pycache__", ".pytest_cache", ".git", "venv", ".venv", "node_modules", "backups", "logs", "outputs"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".log", ".rar"}


def should_skip(path):
    if any(part in EXCLUDED_NAMES for part in path.parts):
        return True
    if path.suffix.lower() in EXCLUDED_SUFFIXES:
        return True
    if path.name in ("runtime",):
        return True
    return False


def copy_tree(source, destination):
    for path in source.rglob("*"):
        if should_skip(path):
            continue
        rel = path.relative_to(source)
        target = destination / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(path), str(target))


def copy_project(stage):
    for relative in INCLUDE_DIRS:
        source = ROOT / relative
        if source.is_dir():
            copy_tree(source, stage / relative)
    for pattern in INCLUDE_ROOT_FILES:
        for source in ROOT.glob(pattern):
            if source.is_file() and not should_skip(source):
                (stage / source.name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(source), str(stage / source.name))
    (stage / "runtime").mkdir(parents=True, exist_ok=True)
    (stage / "runtime" / "README_WIN7.txt").write_text(
        "This directory is created by the offline installers.\\n"
        "Virtual environments are intentionally rebuilt on the target machine.\\n",
        encoding="utf-8",
    )


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def make_zip(stage, zip_path):
    with zipfile.ZipFile(str(zip_path), "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                archive.write(str(path), str(path.relative_to(stage)).replace(os.sep, "/"))


def main():
    parser = argparse.ArgumentParser(description="Build the complete offline Win7 package")
    parser.add_argument("--output-dir", default=str(ROOT / "deliverables" / "full_win7"))
    parser.add_argument("--name", default="")
    args = parser.parse_args()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    name = args.name or ("IndustrialProtocolDemo_V19_6_14_FULL_WIN7_OFFLINE_" + stamp)
    stage = output_dir / ("." + name + ".stage")
    zip_path = output_dir / (name + ".zip")
    hash_path = Path(str(zip_path) + ".sha256")
    for path in (stage, zip_path, hash_path):
        if path.is_dir():
            shutil.rmtree(str(path))
        elif path.exists():
            path.unlink()
    stage.mkdir(parents=True)
    copy_project(stage)
    make_zip(stage, zip_path)
    digest = sha256(zip_path)
    hash_path.write_text(digest + "  " + zip_path.name + "\n", encoding="ascii")
    manifest = {
        "status": "PASS",
        "package": str(zip_path),
        "sha256": digest,
        "size": zip_path.stat().st_size,
        "python_requirement": "CPython 3.8 x64 on Windows 7",
        "includes": ["project source", "production/runtime wheelhouses", "AutoML 0.24 wheelhouse if downloaded", "user-supplied AutoML bundle"],
    }
    (output_dir / (name + ".json")).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.rmtree(str(stage))
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("[FAIL] %s" % exc, file=sys.stderr)
        raise SystemExit(1)
