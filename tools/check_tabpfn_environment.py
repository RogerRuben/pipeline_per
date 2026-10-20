# -*- coding: utf-8 -*-
"""Verify real imports and model execution in the packaged interpreter."""
import argparse
import importlib
import importlib.metadata
import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def isolate_candidate(site):
    site = Path(site).resolve()
    if ROOT not in site.parents:
        raise ValueError("候选环境目录必须在当前解压包内。")
    sys.path[:] = [entry for entry in sys.path if not any(marker in str(entry).lower().replace("\\", "/") for marker in ("/lib/site-packages", "/lib/s_"))]
    sys.path.insert(0, str(site))


def validate_training_runtime():
    import sklearn
    from packaging.version import Version
    from sklearn.preprocessing import OneHotEncoder
    from tabpfn import TabPFNRegressor
    if sys.version_info < (3, 9):
        raise RuntimeError("TabPFN2.0.9要求Python3.9及以上，请使用包内解释器。")
    if not Version("1.2") <= Version(sklearn.__version__) < Version("1.7"):
        raise RuntimeError("TabPFN2.0.9支持sklearn1.2—1.6，当前为%s；本开发机包不会要求sklearn0.24。" % sklearn.__version__)
    if "sparse_output" not in OneHotEncoder().get_params() or not callable(getattr(TabPFNRegressor, "predict", None)):
        raise RuntimeError("当前环境缺少训练所需API，请运行当前包的安装脚本。")


def verify(profile="win11", candidate_site=None, assets=None, deep=False, bundle=None):
    if candidate_site:
        isolate_candidate(candidate_site)
    sys.path.insert(0, str(ROOT))
    assets = Path(assets or ROOT / "assets").resolve()
    if not assets.is_dir():
        assets = ROOT / "runtime" / ("tabpfn_offline_assets" if profile == "win11" else "tabpfn_win7_assets")
    expected_py = (3, 11) if profile == "win11" else (3, 8)
    if sys.version_info[:2] != expected_py:
        raise RuntimeError("请使用当前包的Python%d.%d解释器，当前实际为%s：%s" % (expected_py + (sys.version.split()[0], sys.executable)))
    modules = {"numpy": "numpy", "scipy": "scipy", "pandas": "pandas", "scikit-learn": "sklearn", "joblib": "joblib", "threadpoolctl": "threadpoolctl"}
    if profile == "win11":
        modules.update({"torch": "torch", "tabpfn": "tabpfn", "tqdm": "tqdm", "optuna": "optuna", "einops": "einops", "huggingface-hub": "huggingface_hub"})
    imported, failures = {}, []
    for package, name in modules.items():
        try:
            module = importlib.import_module(name)
            imported[name] = {"version": importlib.metadata.version(package), "path": str(getattr(module, "__file__", ""))}
        except Exception as exc:
            failures.append(name + ": " + type(exc).__name__ + ": " + str(exc))
    if failures:
        raise RuntimeError("环境依赖未就绪，安装未完成：\n" + "\n".join(failures))
    lock = assets / "requirements.lock.txt"
    if lock.is_file():
        for line in lock.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            package, version = line.split()[0].split("==", 1)
            if importlib.metadata.version(package).lower() != version.lower():
                raise RuntimeError("安装锁定版本不一致：%s，预期%s，实际%s" % (package, version, importlib.metadata.version(package)))
    report = {"status": "PASS", "profile": profile, "python": sys.version.split()[0], "executable": sys.executable, "modules": imported,
              "deep_verification": bool(deep), "legacy_sklearn_024_module_imported": "tools.automl.sklearn_0_24.automl_search" in sys.modules}
    if profile == "win11":
        validate_training_runtime()
        if report["legacy_sklearn_024_module_imported"]:
            raise RuntimeError("现代试用路径误导入旧sklearn0.24训练模块。")
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        checkpoint = assets / "model" / "tabpfn-v2-regressor.ckpt"
        if not checkpoint.is_file():
            raise RuntimeError("离线回归权重缺失：" + str(checkpoint))
        os.environ["TABPFN_PRICE_CHECKPOINT"] = str(checkpoint)
        if deep:
            import numpy as np
            from services.tabpfn_price_service.estimators import TabPFNContextRegressor
            test = TabPFNContextRegressor(n_estimators=1, threads=1, checkpoint=str(checkpoint)).fit(
                np.asarray([[0., 0.], [1., 0.], [0., 1.], [1., 1.]]), [10., 11., 12., 13.])
            value = float(test.predict([[.5, .5]])[0])
            if not math.isfinite(value) or value <= 0:
                raise RuntimeError("TabPFN实际预测检查失败。")
            report["actual_tabpfn_prediction"] = value
            path = Path(bundle) if bundle else ROOT / "prototype" / "price" / "price_native_bundle.pkl"
            if path.is_file():
                from services.tabpfn_price_service.app import TabPFNPriceService
                service = TabPFNPriceService(path)
                response = service.handle_post("/api/v1/predict", service.example_request())
                if not response.get("success"):
                    raise RuntimeError("已保存价格bundle无法预测。")
                report["saved_bundle_prediction"] = response["prediction"]["predicted_price_wan"]
            else:
                if (ROOT / "delivery_manifest.json").is_file():
                    raise RuntimeError("交付包中的已保存试用模型缺失，安装不能判为成功：" + str(path))
                report["saved_bundle_status"] = "not_supplied_in_source_checkout"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=["win11", "win7_client"], default="win11")
    parser.add_argument("--candidate-site")
    parser.add_argument("--assets")
    parser.add_argument("--deep", action="store_true")
    parser.add_argument("--bundle")
    parser.add_argument("--report", default=str(ROOT / "logs" / "environment_check.json"))
    args = parser.parse_args()
    path = Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = verify(args.profile, args.candidate_site, args.assets, args.deep, args.bundle)
    except Exception as exc:
        path.write_text(json.dumps({"status": "FAIL", "message": str(exc), "executable": sys.executable}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(str(exc), flush=True)
        return 1
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("环境实测通过：" + result["executable"], flush=True)
    if args.profile == "win11":
        print("tqdm " + result["modules"]["tqdm"]["version"] + "；sklearn " + result["modules"]["sklearn"]["version"] + "；检查记录：" + str(path), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
