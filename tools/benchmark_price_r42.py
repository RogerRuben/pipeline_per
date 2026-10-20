# -*- coding: utf-8 -*-
"""Software stress cases, never a substitute for the unavailable business table."""
import argparse
import copy
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import numpy as np
from tools.product_r42_profiles import profile
from tools.product_price_automl_r4 import train_price_r4,score
from services.price_service.native_bundle import load_bundle,predict


def main():
    p=argparse.ArgumentParser();p.add_argument("--output",required=True);p.add_argument("--case",choices=["quadratic","quadratic_noisy","linear","noise"],default="quadratic");p.add_argument("--baseline",action="store_true");a=p.parse_args()
    out=Path(a.output);out.mkdir(parents=True,exist_ok=True)
    rng=np.random.RandomState(608);X=rng.uniform(-1,1,(272,36));X[:16,0]=np.linspace(-1,1,16)
    if a.case in ("quadratic","quadratic_noisy"):
        y=80+30*X[:,0]**2
        if a.case=="quadratic_noisy":y=y+rng.normal(0,1.5,272)
    elif a.case=="linear":y=80+24*X[:,0]-18*X[:,1]+12*X[:,2]+rng.normal(0,2.,272)
    else:y=rng.uniform(50.,150.,272)
    specs=[{"parameter_id":"x%d"%i,"label":"价格属性%d"%i,"value_type":"number","unit":"","required":False,"inference":{"observed_count":16}} for i in range(36)]
    data={"products":[{"product_code":"R42_STRESS","product_name":"微样本压力对照"}],"parameters":specs,
          "agreements":[{"agreement_id":"S%02d"%i,"product_code":"R42_STRESS","historical_price_wan":float(y[i]),"params":{q["parameter_id"]:float(X[i,j]) for j,q in enumerate(specs)}} for i in range(16)]}
    c=profile("quick")
    c["search"].update(anchor_trials=32,max_restarts=1,restart_trials=16)
    c["seed_search"].update(max_trials=16)
    c["validation"].update(inner_folds=8,inner_repeats=2,outer_strategy="group_kfold",outer_folds=8,outer_repeats=1)
    c["resources"].update(total_seconds=1200.,per_search_seconds=100.)
    if a.baseline:
        # Run the preserved R4.1 source in its original module namespace.
        import importlib.util
        directory=ROOT/"outputs/r42_optimization_20261015/baseline_source"
        for name,relative in [("services.price_service.r4_estimators","services/price_service/r4_estimators.py"),("tools.product_price_automl_r4","tools/product_price_automl_r4.py")]:
            s=importlib.util.spec_from_file_location(name,str(directory/relative));m=importlib.util.module_from_spec(s);sys.modules[name]=m;s.loader.exec_module(m)
        from tools.product_price_automl_r4 import train_price_r4 as baseline_train,load_config
        c=load_config(overrides={"seed_search":{"max_trials":16},"resources":{"total_seconds":1200.}});trainer=baseline_train
    else:trainer=train_price_r4
    (out/"effective_input_config.json").write_text(json.dumps(c,ensure_ascii=False,indent=2),encoding="utf-8")
    (out/"fixed_training_records.json").write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
    bundle,report=trainer(data,out/"training",overrides=c)
    b=load_bundle(out/"training/price_native_bundle.pkl")
    pred=[predict(b,{q["parameter_id"]:float(row[j]) for j,q in enumerate(specs)})["predicted_price_wan"] for row in X[16:]]
    result={"case":a.case,"baseline_r41":a.baseline,"report":report,"external_virtual_256":score(y[16:],pred),
            "note":"16x36固定软件压力样本，外部256条未参与训练；不是业务精度承诺。两版本CV政策不同，不能将其开发分数当成同一测试集。"}
    (out/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"case":a.case,"baseline":a.baseline,"audit":report["nested_cv_metrics"],"external":result["external_virtual_256"],"seconds":report["elapsed_seconds"],"members":report["configuration_count"]},ensure_ascii=False))
if __name__=="__main__":main()
