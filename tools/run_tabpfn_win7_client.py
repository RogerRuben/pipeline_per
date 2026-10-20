# -*- coding: utf-8 -*-
"""Python 3.8 recommendation/effectiveness client for a LAN TabPFN price host."""
from __future__ import print_function

import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def fetch(url, timeout=30):
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def port_number(preferred):
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", int(preferred)))
        except OSError:
            sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT / "prototype"))
    parser.add_argument("--price-url", required=True, help="例如http://192.168.1.20:18901")
    parser.add_argument("--effect-port", type=int, default=18902)
    parser.add_argument("--app-port", type=int, default=18991)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if os.name == "nt":
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    output = Path(args.output).resolve()
    manifest = json.loads((output / "prototype_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("status") != "ready" or manifest.get("price_engine") != "tabpfn":
        raise ValueError("请提供本版本已经构建完成的TabPFN成品目录。")
    price_url = args.price_url.rstrip("/")
    parsed = urlparse(price_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("价格服务地址须为不含账号信息的HTTP/HTTPS地址。")
    health, schema = fetch(price_url + "/health"), fetch(price_url + "/api/v1/schema")
    if health.get("status") != "ok" or schema.get("product_code") != manifest["product_code"]:
        raise ValueError("Win11价格服务未就绪或成品代号不一致。")
    if health.get("model_sha256") != manifest["artifact_sha256"]["price_model"]:
        raise ValueError("Win11服务模型与该验收草稿的价格摘要不一致。")
    for key in ("effectiveness_model", "business_package"):
        artifact = (output / manifest[key]).resolve()
        if output not in artifact.parents or hashlib.sha256(artifact.read_bytes()).hexdigest() != manifest["artifact_sha256"][key]:
            raise ValueError("Win7验收产物摘要不一致：" + key)
    existing = os.environ.get("no_proxy", "")
    os.environ["no_proxy"] = ",".join(filter(None, [existing, parsed.hostname, "127.0.0.1", "localhost"]))
    workspace = output / "recommendation"
    if manifest.get("workspace") != "recommendation":
        raise ValueError("推荐工作区位置无效。")
    effect_port, app_port = port_number(args.effect_port), port_number(args.app_port)
    if effect_port == app_port:
        raise ValueError("推荐和效能服务须使用不同端口。")
    config = {"execution_mode": "services", "local_fallback": False, "price_service_url": price_url,
              "effectiveness_service_url": "http://127.0.0.1:%d" % effect_port, "timeout_seconds": 180, "batch_size": 64}
    (workspace / "config").mkdir(parents=True, exist_ok=True)
    (workspace / "config" / "model_services.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    log = (output / "client_effectiveness_service.log").open("ab")
    child, server = None, None
    try:
        env = dict(os.environ, PYTHONIOENCODING="utf-8", EFFECT_SOURCE_ROOT="", EFFECT_WORKBOOK="", EFFECT_RUNTIME_PACKAGE="")
        child = subprocess.Popen([sys.executable, "-m", "services.effectiveness_service.app", "--package", "", "--snapshot",
                                  str(output / manifest["effectiveness_model"]), "--port", str(effect_port)], cwd=str(ROOT),
                                 env=env, stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + 45
        while True:
            if child.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("本机效能服务启动失败，详见client_effectiveness_service.log。")
            try:
                if fetch(config["effectiveness_service_url"] + "/health", 2).get("status") == "ok":
                    break
            except OSError:
                pass
            time.sleep(.2)
        from app.store import Store
        from app.product_releases import ProductReleaseService
        from app.model_service_client import DegradedServiceRuntime
        runtime = DegradedServiceRuntime(None, manifest["product_code"], manifest["product_name"])
        store = Store(workspace / "data" / "protocol_demo.db", workspace / "data" / "unused.csv", runtime)
        releases = ProductReleaseService(store, runtime)
        if releases.get(manifest["release_id"])["status"] != "active":
            releases.activate(manifest["release_id"])
        from app.server import create_server
        server = create_server(workspace, "127.0.0.1", app_port)
        url = "http://127.0.0.1:%d/" % app_port
        (output / "client_running.json").write_text(json.dumps({"pid": os.getpid(), "url": url, "price_url": price_url,
                                                               "effectiveness_url": config["effectiveness_service_url"]}), encoding="utf-8")
        print("Win7验收页面：" + url + "\n价格由Win11 TabPFN服务实时计算。Ctrl+C停止本机服务。", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.server_close()
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait(timeout=5)
        log.close()
        state = output / "client_running.json"
        if state.exists():
            state.unlink()


if __name__ == "__main__":
    main()
