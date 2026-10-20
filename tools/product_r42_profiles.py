# -*- coding: utf-8 -*-
"""Explicit R4.2 settings. These never replace or loosen the quality targets."""
import argparse
import copy
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from tools.product_price_automl_r4 import load_config


def profile(name="data_only"):
    c=load_config(overrides={
      "models":["ridge","relative_ridge","lasso","elasticnet","mape_lasso","relative_huber","pls","pca_ridge","poly_ridge","svr","kernel_ridge","gpr","extra_trees","gbdt"],
      "expert":{"enabled":name=="expert_compare"},
      "pipeline":{"feature_views":["all","screened"],"select_k":[1,2,3,5,8,12],"screening":"nonlinear","select_fraction":1.},
      "search":{"anchor_trials":128,"promote":5,"restart_trials":32,"max_restarts":40,"wide_kernels":True,"optimize_gp":True,
                "ridge_alphas":[1e-8,1e-6,1e-4,.01,1.,100.,10000.,1000000.],
                "parameter_ranges":{"ridge":{"alpha":[1e-8,1e6,True]},"relative_ridge":{"alpha":[1e-8,1e6,True]},
                 "lasso":{"alpha":[1e-7,1.,True]},"elasticnet":{"alpha":[1e-7,1.,True],"l1_ratio":[.01,.99]},
                 "mape_lasso":{"alpha":[1e-6,.3,True]},"relative_huber":{"alpha":[1e-6,1.,True],"delta":[.01,1.,True]},
                 "svr":{"C":[1e-3,1e6,True],"epsilon":[1e-5,.5,True],"gamma":[1e-7,100.,True]},
                 "kernel_ridge":{"alpha":[1e-9,1000.,True],"gamma":[1e-7,100.,True]},
                 "gpr":{"length_scale":[.01,1000.,True],"noise":[1e-8,1.,True]},
                 "pls":{"n_components":[1,12]},"pca_ridge":{"n_components":[1,12],"alpha":[1e-8,1e6,True]},
                 "poly_ridge":{"degree":[2,3],"alpha":[1e-8,1e6,True]},
                 "extra_trees":{"n_estimators":[32,256],"max_depth":[1,12],"min_samples_leaf":[1,4],"max_features":[.1,1.]},
                 "gbdt":{"n_estimators":[24,256],"max_depth":[1,8],"min_samples_leaf":[1,4],"learning_rate":[.005,.5,True],"subsample":[.5,1.]}}},
      "seed_search":{"max_trials":5000,"stop_when_unmet_stagnant":False,"sample_weight_strength":.2},
      "validation":{"inner_strategy":"price_balanced","inner_folds":8,"inner_repeats":2,
                    "outer_strategy":"group_kfold","outer_folds":8,"outer_repeats":2},
      "ensemble":{"min_members":2,"max_members":6,"minimum_weight":.03,"maximum_weight":.95,"candidate_pool":32,"jackknife":False},
      "resources":{"total_seconds":3600.,"per_search_seconds":150.}})
    if name=="loo_audit": c["validation"].update(outer_strategy="loo",outer_repeats=1)
    elif name=="quick":
        c["search"].update(anchor_trials=48,restart_trials=24,max_restarts=8)
        c["validation"].update(inner_folds=5,inner_repeats=1,outer_folds=5,outer_repeats=1,outer_strategy="price_balanced")
        c["resources"].update(total_seconds=900.,per_search_seconds=60.)
    elif name not in ("data_only","expert_compare"): raise ValueError("未知R4.2配置："+name)
    return load_config(overrides=c)


def main():
    p=argparse.ArgumentParser(); p.add_argument("--output",required=True); args=p.parse_args(); directory=Path(args.output); directory.mkdir(parents=True,exist_ok=True)
    for name in ("data_only","expert_compare","loo_audit","quick"):
        (directory/("price_r42_"+name+".json")).write_text(json.dumps(profile(name),ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("已生成R4.2四套训练配置；精度门槛保持原值。")
if __name__=="__main__": main()
