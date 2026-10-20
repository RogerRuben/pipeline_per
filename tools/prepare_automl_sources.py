# -*- coding: utf-8 -*-
"""Extract the user-supplied AutoML source bundle safely and repeatably."""
from __future__ import print_function

import argparse
import json
import shutil
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ZIP = ROOT / "vendor" / "automl_sklearn_021_024_bundle.zip"
DESTINATION = ROOT / "tools" / "automl"
ALLOWED_PREFIXES = ("sklearn_0_21/", "sklearn_0_24/")
ALLOWED_NAMES = {"README.md", "requirements.txt", "requirements_keep_existing_sklearn.txt", "automl_search.py"}


def safe_member(member):
    name = member.filename.replace("\\", "/")
    if not name.startswith(ALLOWED_PREFIXES):
        return None
    parts = name.split("/")
    if len(parts) != 2 or parts[1] not in ALLOWED_NAMES:
        return None
    if any(part in ("", ".", "..") for part in parts):
        return None
    return name


def extract(bundle, force=False):
    bundle = Path(bundle).resolve()
    if not bundle.is_file():
        raise ValueError("AutoML bundle not found: %s" % bundle)
    if DESTINATION.exists() and force:
        shutil.rmtree(str(DESTINATION))
    DESTINATION.mkdir(parents=True, exist_ok=True)
    extracted = []
    with zipfile.ZipFile(str(bundle), "r") as archive:
        for member in archive.infolist():
            name = safe_member(member)
            if not name or member.is_dir():
                continue
            target = DESTINATION / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member, "r") as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            extracted.append(name)
    expected = [
        "sklearn_0_21/automl_search.py",
        "sklearn_0_24/automl_search.py",
        "sklearn_0_24/requirements.txt",
    ]
    missing = [name for name in expected if not (DESTINATION / name).is_file()]
    if missing:
        raise ValueError("AutoML bundle is incomplete: %s" % ", ".join(missing))
    return bundle, sorted(extracted)


def main():
    parser = argparse.ArgumentParser(description="Extract the supplied AutoML source bundle")
    parser.add_argument("--zip", default=str(DEFAULT_ZIP))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    bundle, extracted = extract(args.zip, args.force)
    print(json.dumps({
        "status": "PASS",
        "bundle": str(bundle),
        "destination": str(DESTINATION),
        "files": extracted,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("[FAIL] %s" % exc, file=sys.stderr)
        raise SystemExit(1)
