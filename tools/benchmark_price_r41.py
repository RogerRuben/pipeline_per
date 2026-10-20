# -*- coding: utf-8 -*-
"""Predeclared 16x36 diagnostics; the external 256 rows never enter selection."""
import argparse
import copy
import importlib.util
import json
import sys
import time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def fixture(case="mixed_scores"):
    import numpy as np
    from tools.product_pipeline import fixture_csv
    rng = np.random.RandomState(17036)
    X = rng.uniform(-1., 1., (272, 36))
    X[:, 3] = .9 * X[:, 0] + .1 * rng.normal(size=len(X))
    X[:, 4] = .9 * X[:, 1] + .1 * rng.normal(size=len(X))
    X[:, 5] = 1.
    y = 80 + 24*X[:, 0] - 18*X[:, 1] + 12*X[:, 2] + 6*X[:, 0]*X[:, 1] + rng.normal(0, 2., len(X))
    if case == "neutral": scores = [3.] * 36
    elif case == "wrong_scores": scores = [1.]*6 + [5.]*4 + [2.]*26
    else: scores = [5., 4., 3., 4., 3., 1.] + [1.]*30
    specs = [{"parameter_id": "x%02d"%j, "label": "价格属性%02d"%j, "value_type": "number",
              "unit": "", "required": False, "inference": {"observed_count":16}} for j in range(36)]
    data = {"products":[{"product_code":"BENCH16","product_name":"16条36属性诊断样本"}],
            "parameters":copy.deepcopy(specs), "agreements":[]}
    for i in range(16):
        data["agreements"].append({"agreement_id":"B%02d"%i,"product_code":"BENCH16",
                                  "historical_price_wan":float(y[i]),
                                  "params":{p["parameter_id"]:float(X[i,j]) for j,p in enumerate(specs)}})
    table = {"labels":["成品编号","成品名称","价格（万元）"]+[p["label"] for p in specs],
             "field_ids":["product_id","product_name","price_wan"]+[p["parameter_id"] for p in specs],
             "rows":[["B%02d"%i,"虚拟成品%02d"%i,float(y[i])]+X[i].tolist() for i in range(16)]}
    return X, y, specs, scores, data, fixture_csv(table)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output",required=True)
    parser.add_argument("--case",default="mixed_scores",choices=["mixed_scores","neutral","wrong_scores"])
    parser.add_argument("--baseline")
    parser.add_argument("--mode",default="inner",choices=["inner","full","models"])
    parser.add_argument("--seconds",type=float,default=1800.)
    parser.add_argument("--seeds",type=int)
    args = parser.parse_args(); out = Path(args.output); out.mkdir(parents=True,exist_ok=True)
    if args.baseline:
        for name,relative in [("services.price_service.r4_estimators","services/price_service/r4_estimators.py"),
                              ("tools.product_price_automl_r4","tools/product_price_automl_r4.py")]:
            spec=importlib.util.spec_from_file_location(name,str(Path(args.baseline)/relative))
            module=importlib.util.module_from_spec(spec); sys.modules[name]=module; spec.loader.exec_module(module)
    import numpy as np
    from threadpoolctl import threadpool_limits
    from tools.product_price_automl_r4 import load_config,InnerSearch,score,train_price_r4
    from services.price_service.r4_estimators import R4PriceRegressor
    from services.price_service.native_bundle import load_bundle,predict
    X,y,specs,scores,data,csv=fixture(args.case)
    (out/"virtual_16x36.csv").write_bytes(csv)
    overrides={"expert":{"scores":{p["parameter_id"]:s for p,s in zip(specs,scores)}},"resources":{"total_seconds":args.seconds}}
    if args.seeds: overrides["seed_search"]={"max_trials":args.seeds}
    config=load_config(overrides=overrides)
    (out/"config.json").write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding="utf-8")
    start=time.monotonic()
    with threadpool_limits(limits=1):
        if args.mode=="models":
            timings={}
            for name in config["models"]:
                candidate={"model":name,"parameters":{},"expert_strength":1.,"target_transform":"identity","view":"all","sample_weight_strength":.25}
                t=time.monotonic()
                try:
                    p=[]
                    for i in range(15):
                        mask=np.arange(15)!=i
                        m=R4PriceRegressor(specs,scores,candidate,811).fit(X[:15][mask],y[:15][mask])
                        p.append(m.predict(X[i:i+1])[0])
                    timings[name]={"seconds_15_fits":time.monotonic()-t,"metrics":score(y[:15],p)}
                except Exception as exc: timings[name]={"seconds":time.monotonic()-t,"error":str(exc)}
                print(json.dumps({name:timings[name]},ensure_ascii=False),flush=True)
            result={"per_model":timings}
        elif args.mode=="inner":
            selection=InnerSearch(X[:15],y[:15],np.arange(15),specs,scores,config,start+args.seconds,out/"search",print).run()
            fitted=[]
            for row in selection["rows"]:
                fitted.append([R4PriceRegressor(specs,scores,row["config"],seed).fit(X[:16],y[:16]) for seed in row["seeds"]])
            pred=np.column_stack([np.mean([m.predict(X[16:]) for m in group],axis=0)*selection["calibration"] for group in fitted]).dot(selection["weights"])
            result={"development":score(y[:15],selection["oof"]),"external_256":score(y[16:],pred),
                    "seed_report":selection["seed_report"],"chosen":[r["config"] for r in selection["rows"]],"weights":selection["weights"].tolist()}
        else:
            bundle,report=train_price_r4(data,out/"training",overrides=overrides)
            native=load_bundle(out/"training/price_native_bundle.pkl")
            pred=[predict(native,{p["parameter_id"]:float(row[j]) for j,p in enumerate(specs)})["predicted_price_wan"] for row in X[16:]]
            result={"report":report,"external_256":score(y[16:],pred)}
    result["elapsed_seconds"]=time.monotonic()-start; result["case"]=args.case
    (out/"results.json").write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)


if __name__=="__main__": main()
