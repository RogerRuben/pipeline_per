# -*- coding: utf-8 -*-
"""Rebuild the isolated embedded Python environment using only local wheels."""
from __future__ import print_function

import argparse
import email
import hashlib
import json
import os
import subprocess
import sys
import datetime
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def extract_checked(archive, destination):
    destination = Path(destination).resolve()
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            target = (destination / info.filename).resolve()
            if target != destination and destination not in target.parents:
                raise ValueError("Unsafe archive member: " + info.filename)
        z.extractall(destination)


def install(assets, runtime, profile="win11"):
    assets, runtime = Path(assets).resolve(), Path(runtime).resolve()
    if ROOT not in runtime.parents or runtime.parent != ROOT / "runtime":
        raise ValueError("环境目录必须在本项目/解压包内。")
    manifest = json.loads((assets / "assets_manifest.json").read_text(encoding="utf-8"))
    for item in manifest["artifacts"]:
        path = (assets / item["path"]).resolve()
        if assets not in path.parents or sha256(path) != item["sha256"]:
            raise ValueError("离线文件摘要校验失败：" + item["path"])
    version, stem, pip_version = ("3.8.10", "python38", "24.0") if profile == "win7_client" else ("3.11.9", "python311", "24.3.1")
    python = runtime / "python.exe"
    runtime.mkdir(parents=True, exist_ok=True)
    if not python.exists():
        extract_checked(assets / ("python-%s-embed-amd64.zip" % version), runtime)
    project_relative = "..\\.."
    # This fixed location is part of the offline bundle layout.
    if runtime.parent != ROOT / "runtime":
        raise ValueError("请将环境放在runtime下的单独目录。")
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    # Long sklearn extension filenames can exceed Windows MAX_PATH when the
    # package root and environment generation both have long names.
    site = runtime / "Lib" / ("s_" + uuid.uuid4().hex[:8])
    site.mkdir(parents=True, exist_ok=True)
    pip_wheel = next((assets / "wheelhouse").glob("pip-%s-*.whl" % pip_version))
    bootstrap = runtime / ("pip_bootstrap_" + stamp)
    extract_checked(pip_wheel, bootstrap)
    extract_checked(pip_wheel, site)
    pins = []
    for path in sorted((assets / "wheelhouse").glob("*.whl")):
        with zipfile.ZipFile(path) as z:
            metadata = next(name for name in z.namelist() if name.endswith(".dist-info/METADATA"))
            package = email.message_from_bytes(z.read(metadata))
        pins.append("%s==%s --hash=sha256:%s" % (package["Name"], package["Version"], sha256(path)))
    lock = assets / "requirements.lock.txt"
    lock.write_text("\n".join(pins) + "\n", encoding="utf-8")
    install_lock = assets / "requirements.install.lock.txt"
    # pip is bootstrapped from its verified wheel, not asked to replace itself
    # through a runpy entry point (which Windows pip intentionally refuses).
    install_lock.write_text("\n".join(line for line in pins if not line.lower().startswith("pip==")) + "\n", encoding="utf-8")
    target = str(site)
    if os.name == "nt":
        target = "\\\\?\\UNC\\" + target[2:] if target.startswith("\\\\") else "\\\\?\\" + target
    boot_code = "import sys,runpy; sys.path[:]=[p for p in sys.path if not any(marker in p.lower().replace(chr(92),'/') for marker in ('/lib/site-packages','/lib/s_'))]; sys.path.insert(0,sys.argv[1]); sys.argv=['pip']+sys.argv[2:]; runpy.run_module('pip',run_name='__main__')"
    subprocess.check_call([str(python), "-S", "-c", boot_code, str(bootstrap), "install", "--target", target, "--upgrade", "--no-index", "--no-warn-script-location", "--find-links", str(assets / "wheelhouse"),
                           "--require-hashes", "-r", str(install_lock)])
    subprocess.check_call([str(python), "-S", "-c", boot_code, str(site), "check"])
    checker = ROOT / "tools" / "check_tabpfn_environment.py"
    verification = ROOT / "logs" / ("install_" + profile + "_verification.json")
    command = [str(python), "-S", str(checker), "--candidate-site", str(site), "--assets", str(assets), "--profile", profile,
               "--report", str(verification)]
    if profile == "win11":
        command.append("--deep")
    subprocess.check_call(command)
    # Activate only after every import and real prediction succeeds. Existing
    # running processes retain their original paths; no old environment is deleted.
    pth = runtime / (stem + "._pth")
    pending = runtime / (stem + "._pth.pending")
    pending.write_text(stem + ".zip\n.\n" + str(site.relative_to(runtime)) + "\n" + project_relative + "\nimport site\n", encoding="ascii")
    pending.replace(pth)
    ready = {"status": "PASS", "profile": profile, "python": version, "site_directory": str(site.relative_to(runtime)),
             "verification_report": str(verification.relative_to(ROOT)), "requirements_sha256": sha256(lock)}
    (runtime / "environment_ready.json").write_text(json.dumps(ready, indent=2), encoding="utf-8")
    print("离线安装及实际预测检查已完成：" + str(python), flush=True)
    return python


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", default=str(ROOT / "assets"))
    parser.add_argument("--runtime", default=str(ROOT / "runtime" / "tabpfn311"))
    parser.add_argument("--profile", choices=["win11", "win7_client"], default="win11")
    args = parser.parse_args()
    try:
        install(args.assets, args.runtime, args.profile)
    except Exception as exc:
        path = ROOT / "logs" / ("install_" + args.profile + "_failure.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"status": "FAIL", "message": str(exc), "profile": args.profile}, ensure_ascii=False, indent=2), encoding="utf-8")
        print("安装未完成，原环境未替换；详情：" + str(path), flush=True)
        raise


if __name__ == "__main__":
    main()
