# -*- coding: utf-8 -*-
"""Check 5000 real OOF trials and predictions from a saved R4 model."""
import argparse
import hashlib
import importlib.util
import json
import sys
import time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))


def main():
    p=argparse.ArgumentParser(); p.add_argument("--output",required=True); p.add_argument("--baseline"); p.add_argument("--saved-model")
    a=p.parse_args(); out=Path(a.output); out.mkdir(parents=True,exist_ok=True)
    if a.baseline:
        name="services.price_service.r4_estimators"; path=Path(a.baseline)/"services/price_service/r4_estimators.py"
        spec=importlib.util.spec_from_file_location(name,str(path)); m=importlib.util.module_from_spec(spec); sys.modules[name]=m; spec.loader.exec_module(m)
    import numpy as np
    from threadpoolctl import threadpool_limits
    from services.price_service.native_bundle import load_bundle,predict
    if a.saved_model:
        b=load_bundle(a.saved_model)
        # The models in this original R4 demo have the same numeric feature order.
        models=b["models"]
        predictions={}
        for name,model in models.items():
            estimator=model.get("estimator") if isinstance(model,dict) else model
            if estimator is None: continue
            X=np.zeros((3,len(b["feature_order"]))); predictions[name]=np.asarray(estimator.predict(X)).tolist()
        assert predictions,"No fitted estimators were inspected"
        result={"model_sha256":hashlib.sha256(Path(a.saved_model).read_bytes()).hexdigest(),"predictions":predictions}
    else:
        from tools.benchmark_price_r41 import fixture
        from tools.product_price_automl_r4 import load_config,InnerSearch
        X,y,specs,scores,data,csv=fixture("neutral")
        config=load_config(overrides={"models":["ridge"],"search":{"anchor_trials":1,"warm_start":False},"pipeline":{"feature_views":["all"]},
                     "seed_search":{"max_trials":5000,"early_stop":False},"resources":{"total_seconds":180.}})
        start=time.monotonic()
        with threadpool_limits(limits=1):
            selection=InnerSearch(X[:15],y[:15],np.arange(15),specs,scores,config,start+180.,out/"search",print).run()
        result={"elapsed_seconds":time.monotonic()-start,**selection["seed_report"]}
        assert result["completed"]==5000 and result["fit_calls"]>=25000,result
    (out/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=="__main__": main()
