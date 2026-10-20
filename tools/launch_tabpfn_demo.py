# -*- coding: utf-8 -*-
"""Open the saved three-service demo using the packaged modern interpreter."""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description="启动已保存的试用模型，不触发重新训练")
    parser.add_argument("output", nargs="?", default=str(ROOT / "prototype"))
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--app-port", type=int, default=18991)
    parser.add_argument("--price-port", type=int, default=18901)
    parser.add_argument("--effect-port", type=int, default=18902)
    args = parser.parse_args()
    from tools.check_tabpfn_environment import verify
    assets = ROOT / "assets"
    if not assets.exists():
        assets = ROOT / "runtime" / "tabpfn_offline_assets"
    verify("win11", assets=assets)
    print("实际使用包内解释器：" + sys.executable, flush=True)
    from tools.product_pipeline import serve
    serve(args.output, activate_draft=True, no_browser=args.no_browser, price_port=args.price_port,
          effect_port=args.effect_port, app_port=args.app_port)


if __name__ == "__main__":
    main()
