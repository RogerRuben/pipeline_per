# -*- coding: utf-8 -*-
"""Download a CPython 3.11 Windows CPU wheelhouse and pinned public weights."""
from __future__ import print_function

import argparse
import hashlib
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url, path, expected=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and (not expected or sha256(path) == expected):
        return
    temporary = path.with_suffix(path.suffix + ".partial")
    print("Downloading " + path.name, flush=True)
    with urllib.request.urlopen(url, timeout=120) as response, temporary.open("wb") as stream:
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            stream.write(block)
    if expected and sha256(temporary) != expected:
        raise ValueError("Downloaded artifact checksum mismatch: " + path.name)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT / "runtime" / "tabpfn_offline_assets"))
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    download("https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip", output / "python-3.11.9-embed-amd64.zip")
    (output / "python_embed.sha256").write_text(sha256(output / "python-3.11.9-embed-amd64.zip"), encoding="ascii")
    download("https://aka.ms/vs/17/release/vc_redist.x64.exe", output / "vc_redist.x64.exe")
    model_repo = "Prior-Labs/TabPFN-v2-reg"
    with urllib.request.urlopen("https://huggingface.co/api/models/" + model_repo, timeout=40) as response:
        info = json.load(response)
    revision = info["sha"]
    with urllib.request.urlopen("https://huggingface.co/api/models/%s/tree/%s" % (model_repo, revision), timeout=40) as response:
        files = json.load(response)
    checkpoint = next(item for item in files if item["path"] == "tabpfn-v2-regressor.ckpt")
    model_hash = checkpoint.get("lfs", {}).get("oid")
    model_hash = model_hash.removeprefix("sha256:") if model_hash else None
    for name in ("tabpfn-v2-regressor.ckpt", "LICENSE.txt", "README.md"):
        download("https://huggingface.co/%s/resolve/%s/%s" % (model_repo, revision, name), output / "model" / name,
                 model_hash if name.endswith(".ckpt") else None)
    subprocess.check_call([sys.executable, "-m", "pip", "download", "--dest", str(output / "wheelhouse"),
                           "--only-binary=:all:", "--platform", "win_amd64", "--python-version", "311",
                           "--implementation", "cp", "--abi", "cp311", "--index-url", "https://pypi.org/simple",
                           "--extra-index-url", "https://download.pytorch.org/whl/cpu", "-r",
                           str(ROOT / "services" / "tabpfn_price_service" / "requirements_win11_cpu.txt")])
    manifest = {"model_repo": model_repo, "model_revision": revision, "model_sha256": sha256(output / "model" / "tabpfn-v2-regressor.ckpt"),
                "python": "3.11.9", "platform": "win_amd64", "device": "cpu",
                "artifacts": [{"path": str(path.relative_to(output)), "size": path.stat().st_size, "sha256": sha256(path)}
                              for path in sorted(output.rglob("*")) if path.is_file() and path.name != "assets_manifest.json"]}
    (output / "assets_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Ready: " + str(output), flush=True)


if __name__ == "__main__":
    main()
