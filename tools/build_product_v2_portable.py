# -*- coding: utf-8 -*-
"""Assemble installed CPython and pinned wheels, with no machine-bound venv."""
from __future__ import print_function

import argparse
import hashlib
import json
import os
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ("product_portable.py", "product_saved_start.py", "product_pipeline.py", "product_price_automl.py", "product_price_automl_v2.py",
         "product_price_micro.py", "benchmark_product_micro.py", "price_expert_fusion_probe.py", "product_effectiveness_bootstrap.py", "product_v2_fixture.py")
EMBED_CANDIDATES = (
    ROOT / "deliverables/product_r41_portable/ProductAutoML_R4_1_20261013/runtime/python38",
    ROOT / "deliverables/product_v2_portable/ProductAutoML_V2_R3_20261008/runtime/python38",
    Path(r"C:\Users\HP\.codex\worktrees\8a8d\industrialprotol_deepseek_verse\offline_assets\python38_price_sklearn024"),
)
EMBED = next((path for path in EMBED_CANDIDATES if (path / "python.exe").is_file()), EMBED_CANDIDATES[0])
SKIP = {"__pycache__", ".pytest_cache", "node_modules", ".git"}


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def copy_file(source, target):
    if not source.is_file() or source.is_symlink():
        raise ValueError("Missing or linked source: " + str(source))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(source), str(target))


def copy_tree(source, target):
    for path in source.rglob("*"):
        if path.is_file() and not set(path.relative_to(source).parts) & SKIP and path.suffix not in (".pyc", ".pyo", ".log"):
            copy_file(path, target / path.relative_to(source))


def install_wheels(stage):
    wheelhouse = ROOT / "services" / "price_service" / "wheelhouse_automl024_win7"
    manifest = json.loads((wheelhouse / "WHEELHOUSE_MANIFEST.json").read_text(encoding="utf-8"))
    site = stage / "runtime" / "python38" / "Lib" / "site-packages"
    for record in manifest["files"]:
        wheel = wheelhouse / record["name"]
        if sha(wheel) != record["sha256"] or wheel.stat().st_size != record["size"]:
            raise ValueError("Wheel checksum mismatch: " + wheel.name)
        with zipfile.ZipFile(str(wheel)) as archive:
            for member in archive.infolist():
                parts = Path(member.filename).parts
                if member.is_dir():
                    continue
                # Library test fixtures are unused at runtime and can exceed
                # Win7's path limit after extraction into an ordinary folder.
                if set(parts) & {"tests", "test", "benchmarks"}:
                    continue
                if ".." in parts or Path(member.filename).is_absolute() or "\\" in member.filename or ":" in member.filename:
                    raise ValueError("Unsafe wheel member")
                if parts[0].endswith(".data"):
                    category = parts[1]
                    if category in ("purelib", "platlib"):
                        target = site.joinpath(*parts[2:])
                    elif category == "data":
                        target = (stage / "runtime" / "python38").joinpath(*parts[2:])
                    elif category in ("scripts", "headers"):
                        continue  # This application has no console-entrypoint dependency.
                    else:
                        raise ValueError("Unknown wheel layout")
                else:
                    target = site / member.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    write_json(stage / "runtime" / "WHEEL_PROVENANCE.json", manifest)


def assemble(stage, embed):
    if stage.exists():
        raise ValueError("Choose a new stage directory; existing files are never deleted.")
    stage.mkdir(parents=True)
    runtime = stage / "runtime" / "python38"
    for file in embed.iterdir():
        if file.is_file() and (file.suffix.lower() in (".dll", ".pyd") or file.name in ("python.exe", "pythonw.exe", "python38.zip", "LICENSE.txt", "python.cat")):
            copy_file(file, runtime / file.name)
    (runtime / "python38._pth").write_bytes(b"python38.zip\n.\nLib\\site-packages\n..\\..\nimport site\n")
    install_wheels(stage)
    copy_tree(ROOT / "app", stage / "app")
    copy_tree(ROOT / "automl_pipeline", stage / "automl_pipeline")
    copy_file(ROOT / "tools" / "portable_sitecustomize.py", runtime / "Lib" / "site-packages" / "sitecustomize.py")
    for relative in ("services/__init__.py", "services/price_service/__init__.py", "services/price_service/app.py",
                     "services/price_service/native_bundle.py", "services/price_service/ensemble_estimators.py",
                     "services/price_service/export_native_price_bundle.py", "services/price_service/synthetic_models.py",
                     "services/effectiveness_service/__init__.py", "services/effectiveness_service/app.py"):
        copy_file(ROOT / relative, stage / relative)
    copy_tree(ROOT / "services" / "common", stage / "services" / "common")
    for name in TOOLS:
        copy_file(ROOT / "tools" / name, stage / "tools" / name)
    copy_tree(ROOT / "tools" / "automl" / "sklearn_0_24", stage / "tools" / "automl" / "sklearn_0_24")
    copy_file(ROOT / "tests" / "portable_compatibility_test.py", stage / "tests" / "portable_compatibility_test.py")
    copy_file(ROOT / "tests" / "product_price_micro_test.py", stage / "tests" / "product_price_micro_test.py")
    copy_file(ROOT / "tests/fixtures/product_pipeline_v2/price_config.json", stage / "tests/fixtures/product_pipeline_v2/price_config.json")
    for name in ("PRICE_AUTOML_V2_IMPLEMENTED.md", "PRODUCT_PROTOTYPE_PIPELINE.md", "PRODUCT_V2_PORTABLE.md", "PRICE_MICRO_SAMPLE_R3.md"):
        copy_file(ROOT / "docs" / name, stage / "docs" / name)
    sample = ROOT / "tests" / "fixtures" / "product_pipeline_v2" / "virtual_10x50.xlsx"
    copy_file(sample, stage / "input" / "products.xlsx")
    copy_file(sample, stage / "templates" / "10条50属性_示例及专家配置.xlsx")
    copy_file(ROOT / "tests" / "fixtures" / "product_pipeline_v2" / "virtual_10x50.csv", stage / "templates" / "双行表头示例.csv")
    fixture_price = json.loads((ROOT / "tests" / "fixtures" / "product_pipeline_v2" / "price_config.json").read_text(encoding="utf-8"))
    fixture_price["experts_from_workbook"] = False
    fixture_price["models"] = [name for name in fixture_price["models"] if not name.startswith("engineering")]
    fixture_price["engineering"] = {"intercept_wan": 0., "coefficients": {}}
    fixture_price["pipeline"]["interaction_pairs"] = []
    fixture_price["resources"].update(total_seconds=900, fit_timeout_seconds=90)
    fixture_price["validation"]["minimum_groups"] = 10
    fixture_price["small_sample"] = {"policy": "auto", "max_groups": 29, "ridge_alphas": [1., 10., 100.],
                "max_effective_df": 3., "latent_components": 2, "max_members": 4, "tree_seeds": 4,
                "expert_features": [], "expert_max_features": 8, "calibration_strength": 8., "log_target": True,
                "minimum_group_win_fraction": .6, "minimum_mape_gain_pp": .1,
                "weight_jackknife": True, "jackknife_blends": [0., .25, .5, .75, 1.]}
    fixture_price["ensemble"]["family_priors"] = [
        {"family": "linear", "label": "正则化线性模型", "weight": .5, "confidence": .5},
        {"family": "tree", "label": "树模型", "weight": .3, "confidence": .5},
        {"family": "kernel", "label": "核与近邻模型", "weight": .2, "confidence": .5}]
    write_json(stage / "config" / "price_automl_v2.json", fixture_price)
    write_json(stage / "config" / "portable.json", {"product_code": "PORTABLE_PRODUCT", "product_name": "成品模型与智能推荐",
               "read_expert_sheets": True, "missing_tokens": ["-1", "\\", "/"], "demo_input_sha256": sha(sample),
               "ports": {"price": 18701, "effectiveness": 18702, "recommendation": 18791}})
    for name, command in (("START.bat", "start"), ("REBUILD.bat", "build"), ("VERIFY.bat", "verify --full"), ("CHECK_INPUT.bat", "inspect")):
        content = '@echo off\r\nsetlocal\r\nchcp 65001 >nul\r\ncd /d "%~dp0"\r\n"%~dp0runtime\python38\python.exe" -X utf8 "%~dp0tools\product_portable.py" ' + command + ' %*\r\nif errorlevel 1 (\r\n  echo ERROR - See logs\\last_error.log\r\n  if not defined PRODUCT_NO_PAUSE pause\r\n  exit /b 1\r\n)\r\nendlocal\r\n'
        (stage / name).write_bytes(content.encode("ascii"))
    from tools.product_saved_start import saved_start_launcher_bytes
    (stage / "RUN_SAVED.bat").write_bytes(saved_start_launcher_bytes())
    (stage / "TEST_COMPATIBILITY.bat").write_bytes(b'@echo off\r\nsetlocal\r\ncd /d "%~dp0"\r\n"%~dp0runtime\\python38\\python.exe" -X utf8 "%~dp0tests\\portable_compatibility_test.py"\r\nif errorlevel 1 (\r\n  if not defined PRODUCT_NO_PAUSE pause\r\n  exit /b 1\r\n)\r\nendlocal\r\n')
    (stage / "TEST_MICRO_SAMPLE.bat").write_bytes(b'@echo off\r\nsetlocal\r\ncd /d "%~dp0"\r\n"%~dp0runtime\\python38\\python.exe" -X utf8 "%~dp0tests\\product_price_micro_test.py"\r\nif errorlevel 1 (\r\n  if not defined PRODUCT_NO_PAUSE pause\r\n  exit /b 1\r\n)\r\nendlocal\r\n')
    copy_file(ROOT / "docs" / "PRODUCT_V2_PORTABLE.md", stage / "使用说明.md")
    (stage / "logs").mkdir()
    (stage / "outputs").mkdir()
    (stage / "state").mkdir()
    return stage


def seal(stage, target):
    if target.exists():
        raise ValueError("Archive already exists; choose a new filename.")
    mutable = {"input", "config", "state", "logs", "outputs", "exports"}
    files = [{"path": p.relative_to(stage).as_posix(), "size": p.stat().st_size, "sha256": sha(p)}
             for p in sorted(stage.rglob("*")) if p.is_file() and p.relative_to(stage).parts[0] not in mutable
             and "__pycache__" not in p.parts and p.name != "PACKAGE_MANIFEST.json"]
    settings = json.loads((stage / "config" / "portable.json").read_text(encoding="utf-8"))
    write_json(stage / "PACKAGE_MANIFEST.json", {"format": "product-v2-portable-1.0", "python": "3.8.10", "architecture": "win_amd64",
               "compatibility_revision": "R2", "training_revision": "R4_price_attribute_priors" if settings.get("price_engine") == "r4" else "R3_micro_sample", "startup_policy": settings.get("startup_policy", "auto_build"), "console_policy": "plain_device_safe_with_spawn_bootstrap", "input_diagnostics": "logs/input_diagnostics.json",
               "environment_embedded": True, "internet_required": False, "python_install_required": False,
               "immutable_files": files, "mutable_directories": sorted(mutable), "target": "Windows x64; Win7 SP1 requires OS prerequisites",
               "win7_machine_tested": False, "entrypoint": "START.bat"})
    target.parent.mkdir(parents=True, exist_ok=True)
    ready_outputs = set()
    for directory in (stage / "outputs").glob("run_*"):
        manifest = directory / "prototype_manifest.json"
        if manifest.is_file() and json.loads(manifest.read_text(encoding="utf-8")).get("status") == "ready":
            ready_outputs.add(directory.name)
    with zipfile.ZipFile(str(target), "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
        for path in sorted(stage.rglob("*")):
            relative = path.relative_to(stage)
            if not path.is_file() or "__pycache__" in relative.parts or path.suffix in (".pyc", ".pyo"):
                continue
            if relative.parts[0] in ("logs", "exports") or "oof_cache" in relative.parts or path.name in ("running.json", "price_service.log", "effectiveness_service.log"):
                continue
            if relative.parts[0] == "outputs" and len(relative.parts) > 1 and relative.parts[1].startswith("run_") and relative.parts[1] not in ready_outputs:
                continue
            archive.write(str(path), stage.name + "/" + relative.as_posix())
    digest = sha(target)
    Path(str(target) + ".sha256").write_text(digest + "  " + target.name + "\n", encoding="utf-8")
    result = {"status": "PACKAGED", "path": str(target), "bytes": target.stat().st_size, "sha256": digest,
              "immutable_file_count": len(files), "embedded_environment": True}
    write_json(Path(str(target) + ".json"), result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True)
    parser.add_argument("--embed", default=str(EMBED))
    parser.add_argument("--zip", help="Seal an already assembled and validated stage into this ZIP")
    args = parser.parse_args()
    stage = Path(args.stage).resolve()
    permitted = (ROOT / "deliverables").resolve()
    if permitted not in stage.parents:
        raise ValueError("Stage must be under the workspace deliverables directory.")
    if args.zip:
        target = Path(args.zip).resolve()
        if permitted not in target.parents:
            raise ValueError("ZIP must be under the workspace deliverables directory.")
        print(json.dumps(seal(stage, target), indent=2))
    else:
        assemble(stage, Path(args.embed))
        print("ASSEMBLED: " + str(stage))


if __name__ == "__main__":
    main()
