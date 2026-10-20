# -*- coding: utf-8 -*-
from __future__ import print_function

import argparse
import json
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from app.server import create_server


def available_port(host, preferred, span=10):
    for port in range(preferred, preferred + max(int(span), 0) + 1):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((host, port))
            return port
        except OSError:
            pass
        finally:
            sock.close()
    if span:
        raise RuntimeError("端口%d-%d均被占用" % (preferred, preferred + span))
    raise RuntimeError("端口%d被占用" % preferred)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("IPDEMO_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("IPDEMO_PORT", "17891")))
    parser.add_argument("--port-span", type=int, default=int(os.environ.get("IPDEMO_PORT_SPAN", "10")))
    parser.add_argument("--no-browser", action="store_true", default=os.environ.get("IPDEMO_OPEN_BROWSER", "1") in ("0", "false", "False"))
    args = parser.parse_args()
    port = available_port(args.host, args.port, args.port_span)
    server = create_server(ROOT, args.host, port)
    application = server.RequestHandlerClass.app
    service_mode = application.model_execution_mode in ("service", "services", "http", "remote")
    runtime = ROOT / "runtime"
    runtime.mkdir(exist_ok=True)
    (runtime / "last_port.txt").write_text(str(port), encoding="ascii")
    browser_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    info = {
        "url": "http://%s:%d/" % (browser_host, port),
        "listen": "%s:%d" % (args.host, port),
        "port": port,
        "pid": os.getpid(),
        "mode": "independent_http_services" if service_mode else "single_process_in_process_models",
        "local_fallback_enabled": bool(application.model_config.get("local_fallback")),
        "public_bind": args.host not in ("127.0.0.1", "localhost", "::1"),
        "demo_read_only": str(os.environ.get("IPDEMO_DEMO_READ_ONLY", "0")).strip().lower() in ("1", "true", "yes", "on"),
        "auth_enabled": str(os.environ.get("IPDEMO_AUTH_ENABLED", "0")).strip().lower() in ("1", "true", "yes", "on"),
    }
    (runtime / "running.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    print("=" * 72)
    print("Industrial Protocol Demo V19.6.14 - Clean HTTP Model Pipeline")
    print("Listen: %s" % info["listen"])
    print("Local URL: %s" % info["url"])
    print("Model mode: %s" % info["mode"])
    print("Local fallback: %s" % ("enabled" if info["local_fallback_enabled"] else "disabled"))
    if info["public_bind"]:
        print("PUBLIC BIND ENABLED: use a firewall and reverse proxy authentication.")
    if info["demo_read_only"]:
        print("CLOUDFLARE LOGIN DEMO: all pages are visible after authentication; persistent server writes are disabled.")
    if info["auth_enabled"]:
        print("LOGIN REQUIRED: %s" % os.environ.get("IPDEMO_AUTH_USERNAME", "ab123"))
    print("Press Ctrl+C to stop.")
    print("=" * 72)
    if not args.no_browser:
        def open_later():
            time.sleep(1.0)
            webbrowser.open(info["url"])
        threading.Thread(target=open_later, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping...")
    finally:
        server.server_close()
        try:
            (runtime / "running.json").unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()
