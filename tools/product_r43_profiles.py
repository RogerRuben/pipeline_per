# -*- coding: utf-8 -*-
"""Small-sample R4.3 policies for the existing Python 3.8 runtime."""
from tools.product_r42_profiles import profile as previous_profile
from tools.product_price_automl_r4 import load_config


def profile(name="data_only"):
    c=previous_profile(name)
    c['models']+=['bayesian_ridge','ard']
    c['pipeline'].update(stability_resamples=16,stability_fraction=.8,stability_correlation=.98)
    c['ensemble'].update(robust=True,minimum_weight=0.,maximum_weight=1.,stability_penalty=.15,
                         max_mape_delta_pp=.2,max_r2_drop=.005,minimum_gain=1e-6)
    c['diagnostics']['sample_influence']=True
    c['trust_region']['enabled']=True
    c['search'].update(anchor_trials=96,restart_trials=24,family_first=True,warm_start_fraction=.35)
    c['validation'].update(inner_folds=4,inner_repeats=2,outer_folds=4,outer_repeats=2)
    c['resources'].update(total_seconds=1000.,per_search_seconds=90.)
    if name=='quick':
        c['pipeline']['stability_resamples']=8
        c['search'].update(anchor_trials=32,max_restarts=2,restart_trials=16)
        c['validation'].update(inner_folds=4,inner_repeats=1,outer_folds=4,outer_repeats=1)
        c['resources'].update(total_seconds=180.,per_search_seconds=30.)
    elif name=='loo_audit':
        c['validation'].update(outer_strategy='loo',outer_repeats=1)
        c['resources']['total_seconds']=1800.
    return load_config(overrides=c)
