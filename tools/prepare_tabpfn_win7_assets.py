# -*- coding: utf-8 -*-
"""Prepare the completely offline Python 3.8 recommendation/effect client kit."""
from __future__ import print_function

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.prepare_tabpfn_offline_assets import download, sha256


def main():
    output = ROOT / "runtime" / "tabpfn_win7_assets"
    output.mkdir(parents=True, exist_ok=True)
    download("https://www.python.org/ftp/python/3.8.10/python-3.8.10-embed-amd64.zip", output / "python-3.8.10-embed-amd64.zip")
    (output / "python_embed.sha256").write_text(sha256(output / "python-3.8.10-embed-amd64.zip"), encoding="ascii")
    # Every transitive dependency is explicitly pinned. Cross-version resolution
    # otherwise evaluates pandas' Python-version markers against the downloader.
    subprocess.check_call([sys.executable, "-m", "pip", "download", "--no-deps", "--dest", str(output / "wheelhouse"), "--only-binary=:all:",
                           "--platform", "win_amd64", "--python-version", "38", "--implementation", "cp", "--abi", "cp38",
                           "--index-url", "https://pypi.org/simple", "-r", str(ROOT / "services" / "tabpfn_price_service" / "requirements_win7_client.txt")])
    manifest = {"python": "3.8.10", "platform": "win_amd64", "role": "LAN client and local effectiveness, no TabPFN execution",
                "artifacts": [{"path": str(path.relative_to(output)), "size": path.stat().st_size, "sha256": sha256(path)}
                              for path in sorted(output.rglob("*")) if path.is_file() and path.name != "assets_manifest.json"]}
    (output / "assets_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
