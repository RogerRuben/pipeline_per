# -*- coding: utf-8 -*-
"""Serve the existing price JSON contract from the isolated modern environment."""
from __future__ import print_function

import argparse
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from services.price_service.app import PriceService
from services.common.http_service import run_service
from services.tabpfn_price_service.estimators import checkpoint_path


class TabPFNPriceService(PriceService):
    service_version = "tabpfn-1.0.0"

    def __init__(self, model_path):
        if sys.version_info < (3, 10):
            raise RuntimeError("TabPFN服务须在Win11开发机的独立Python3.11环境运行。")
        self.prediction_lock = threading.RLock()
        super(TabPFNPriceService, self).__init__(model_path=model_path)
        if self.bundle.get("model_version") != "tabpfn-price-prototype-1.0":
            raise ValueError("此启动器只接受本版本导出的TabPFN试用包。")

    def handle_post(self, path, payload):
        with self.prediction_lock:
            return super(TabPFNPriceService, self).handle_post(path, payload)

    def health(self):
        result = super(TabPFNPriceService, self).health()
        ledger=self.bundle.get("automl_explanation",{}).get("member_ledger",[])
        used=any(row.get("family")=="tabpfn" and row.get("weight",0)>0 for row in ledger)
        result.update(method="价格AutoML与属性评分集成", tabpfn_used_in_ensemble=used,
                      execution_requirement="Python3.11/sklearn1.5.2", device="cpu")
        return result


def main():
    parser = argparse.ArgumentParser(description="Win11 TabPFN价格服务，兼容Win7现有HTTP调用")
    parser.add_argument("--model", required=True)
    parser.add_argument("--checkpoint")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18901)
    args = parser.parse_args()
    os.environ["TABPFN_PRICE_CHECKPOINT"] = str(checkpoint_path(args.checkpoint))
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    service = TabPFNPriceService(args.model)
    service.handle_post("/api/v1/predict", service.example_request())  # Warm before advertising readiness.
    run_service(service, args.host, args.port)


if __name__ == "__main__":
    main()
