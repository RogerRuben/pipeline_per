# -*- coding: utf-8 -*-
"""Build separate reproducible Win11 CPU and Win7 LAN-client ZIPs."""
from __future__ import print_function

import argparse
import json
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))
from tools.prepare_tabpfn_offline_assets import sha256

TOOLS = ("product_pipeline.py", "product_price_automl.py", "product_price_automl_v2.py", "product_price_tabpfn.py",
         "product_effectiveness_bootstrap.py", "price_expert_fusion_probe.py", "install_tabpfn_offline.py",
         "product_price_micro.py", "product_price_automl_r4.py", "product_r4_input.py", "verify_tabpfn_virtual_generalization.py",
         "run_tabpfn_win7_client.py", "bootstrap_tabpfn_win11.ps1", "prepare_tabpfn_offline_assets.py",
         "prepare_tabpfn_win7_assets.py", "build_tabpfn_offline_delivery.py")
TOOLS += ("check_tabpfn_environment.py", "launch_tabpfn_demo.py", "price_attribute_input.py", "price_candidate_recovery.py")
BATCH = ("INSTALL_TABPFN_WIN11_OFFLINE.bat", "TRAIN_TABPFN_PRODUCT_WIN11.bat", "START_TABPFN_PRICE_WIN11.bat",
         "START_TABPFN_LOCAL_WIN11.bat", "START_TABPFN_DEMO_WIN7.bat", "INSTALL_TABPFN_WIN7_CLIENT_OFFLINE.bat")
BATCH += ("ENSURE_TABPFN_WIN11_ENV.bat",)


def copy_filtered(source, destination, suffixes, excluded_names=()):
    for path in source.rglob("*"):
        if path.is_file() and path.suffix.lower() in suffixes and "__pycache__" not in path.parts and path.name not in excluded_names:
            target = destination / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def copy_code(stage):
    copy_filtered(ROOT / "automl_pipeline", stage / "automl_pipeline", {".py", ".json", ".txt", ".md"})
    copy_filtered(ROOT / "app", stage / "app", {".py", ".html", ".css", ".js", ".svg", ".png", ".ico"})
    copy_filtered(ROOT / "services", stage / "services", {".py", ".txt"})
    copy_filtered(ROOT / "models", stage / "models", {".json"})
    copy_filtered(ROOT / "tools" / "automl", stage / "tools" / "automl", {".py", ".txt"})
    for name in TOOLS:
        (stage / "tools").mkdir(exist_ok=True)
        shutil.copy2(ROOT / "tools" / name, stage / "tools" / name)
    for name in BATCH:
        shutil.copy2(ROOT / name, stage / name)
    for directory in ("tests/fixtures/product_pipeline_v2", "docs/examples"):
        copy_filtered(ROOT / directory, stage / directory, {".xlsx", ".csv", ".json"})
    (stage / "tests").mkdir(exist_ok=True)
    shutil.copy2(ROOT / "tests" / "product_price_tabpfn_test.py", stage / "tests" / "product_price_tabpfn_test.py")
    shutil.copy2(ROOT / "tests" / "tabpfn_attribute_scores_test.py", stage / "tests" / "tabpfn_attribute_scores_test.py")
    (stage / "docs").mkdir(exist_ok=True)
    shutil.copy2(ROOT / "docs" / "TABPFN_WIN11_WIN7_DELIVERY.md", stage / "docs" / "TABPFN_WIN11_WIN7_DELIVERY.md")
    shutil.copy2(ROOT / "docs" / "PRICE_TABPFN_MICRO_FIX.md", stage / "docs" / "PRICE_TABPFN_MICRO_FIX.md")


def refresh_assets_manifest(assets):
    manifest_path = assets / "assets_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"] = [{"path": str(path.relative_to(assets)), "size": path.stat().st_size, "sha256": sha256(path)}
                             for path in sorted(assets.rglob("*")) if path.is_file() and path != manifest_path and not path.name.endswith(".partial")]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def package(output, prototype, name, role, assets):
    stage = output / name
    archive_path = output / (name + ".zip")
    if stage.exists() or archive_path.exists():
        raise ValueError("交付目标已经存在，请选择新的输出目录。")
    stage.mkdir(parents=True)
    copy_code(stage)
    copy_filtered(prototype, stage / "prototype", {".json", ".md", ".pkl", ".db", ".csv", ".xlsx", ".js", ".html", ".css", ".svg", ".png"},
                  excluded_names=("running.json", "client_running.json"))
    if role == "win7_client":
        # The price model stays on the modern host. Its checksum stays in the manifest.
        for path in (stage / "prototype" / "price").glob("*.pkl"):
            path.unlink()
    shutil.copytree(assets, stage / "assets", ignore=shutil.ignore_patterns("*.partial"))
    refresh_assets_manifest(stage / "assets")
    (stage / "README_FIRST.md").write_text((ROOT / "docs" / "TABPFN_WIN11_WIN7_DELIVERY.md").read_text(encoding="utf-8"), encoding="utf-8")
    install_entry = "INSTALL_TABPFN_WIN11_OFFLINE.bat" if role == "win11" else "INSTALL_TABPFN_WIN7_CLIENT_OFFLINE.bat"
    start_entry = "START_TABPFN_LOCAL_WIN11.bat" if role == "win11" else "START_TABPFN_DEMO_WIN7.bat"
    for filename, target in (("INSTALL.bat", install_entry), ("START.bat", start_entry)):
        body = '@echo off\r\nchcp 65001 >nul\r\ncall "%%~dp0%s" %%*\r\nexit /b %%errorlevel%%\r\n' % target
        (stage / filename).write_bytes(body.encode("utf-8"))
    (stage / "runtime").mkdir(exist_ok=True)
    if role == "win11":
        launcher = '@echo off\r\ncall "%~dp0..\\START_TABPFN_LOCAL_WIN11.bat" "%~dp0."\r\n'
        (stage / "prototype" / "START_PROTOTYPE_WIN11.bat").write_bytes(launcher.encode("utf-8"))
    records = [{"path": str(path.relative_to(stage)), "size": path.stat().st_size, "sha256": sha256(path)}
               for path in sorted(stage.rglob("*")) if path.is_file()]
    manifest = {"format": "tabpfn-offline-delivery-1.0", "role": role, "python": "3.11.9" if role == "win11" else "3.8.10",
                "architecture": "Windows x64", "trained_on_virtual_data": True, "requires_lan_price_host": role == "win7_client", "files": records}
    (stage / "delivery_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as z:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                z.write(path, str(path.relative_to(stage)))
    digest = sha256(archive_path)
    Path(str(archive_path) + ".sha256").write_text(digest + "  " + archive_path.name + "\n", encoding="ascii")
    return {"role": role, "path": str(archive_path), "sha256": digest, "size_bytes": archive_path.stat().st_size, "files": len(records)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT / "deliverables" / "tabpfn_install_fixed_20261014"))
    parser.add_argument("--prototype", default=str(ROOT / "outputs" / "product_tabpfn_micro_fixed"))
    parser.add_argument("--allow-unqualified-prototype", action="store_true", help="仅用于保留未达标的软件诊断包")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if ROOT not in output.parents or output.parts[len(ROOT.parts)] not in ("deliverables", "outputs"):
        raise ValueError("交付包须写入项目deliverables或outputs目录。")
    output.mkdir(parents=True, exist_ok=True)
    prototype = Path(args.prototype).resolve()
    manifest = json.loads((prototype / "prototype_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("status") != "ready" or manifest.get("price_engine") != "tabpfn":
        raise ValueError("需要已经验证完成的TabPFN成品目录。")
    if not manifest.get("targets_met") and not args.allow_unqualified_prototype:
        raise ValueError("价格MAPE/R²未同时达到门槛，禁止打成验收交付包；诊断用途须显式声明。")
    for key in ("price_model", "effectiveness_model", "business_package"):
        path = (prototype / manifest[key]).resolve()
        if prototype not in path.parents or sha256(path) != manifest["artifact_sha256"][key]:
            raise ValueError("构建产物摘要不一致：" + key)
    results = [package(output, prototype, "TabPFN_WIN11_CPU_OFFLINE", "win11", ROOT / "runtime" / "tabpfn_offline_assets"),
               package(output, prototype, "TabPFN_WIN7_LAN_CLIENT_OFFLINE", "win7_client", ROOT / "runtime" / "tabpfn_win7_assets")]
    (output / "package_report.json").write_text(json.dumps({"status": "ready_for_verification", "packages": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
