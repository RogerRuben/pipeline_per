# -*- coding: utf-8 -*-
"""Portable Optuna observations with an explicit data/protocol identity (no pickle)."""
import json
from pathlib import Path
import numpy as np
import optuna
from optuna.distributions import distribution_to_json,json_to_distribution

FORMAT='price-joint-search-history-1.0'


def save_history(study,path,identity,selected_number=None):
    records=[]
    pareto={t.number for t in study.best_trials}
    for trial in study.trials:
        if trial.state!=optuna.trial.TrialState.COMPLETE or not trial.user_attrs.get('valid') or not np.isfinite(trial.values).all():continue
        records.append({'number':trial.number,'selected':trial.number==selected_number,'pareto':trial.number in pareto,'params':trial.params,'values':list(trial.values),'user_attrs':trial.user_attrs,
                        'distributions':{k:distribution_to_json(v) for k,v in trial.distributions.items()}})
    records.sort(key=lambda r:(not r['selected'],not r['pareto'],-r['number']))
    Path(path).write_text(json.dumps({'format':FORMAT,'identity':identity,'trials':records},ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')


def read_history(path,identity,reuse_metrics=True,maximum=1000,seed_low=1,seed_high=10000):
    if not path:return [],[],{'policy':'none','imported_trials':0,'reevaluation_proposals':0}
    content=json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if content.get('format')!=FORMAT:raise ValueError('历史试验文件格式不受支持。')
    same=content.get('identity')==identity;trials=[];proposals=[];seen=set()
    from .price_automl_core import model_params_from_trial
    # Validate the complete public search space, including categorical distributions.
    template=optuna.create_study().ask();model_params_from_trial(template,seed_low,seed_high)
    distributions=template.distributions
    for item in content.get('trials',[]):
        params=item.get('params',{});key=json.dumps(params,sort_keys=True)
        if key in seen or set(params)!=set(distributions):continue
        if any(not d._contains(d.to_internal_repr(params[name])) for name,d in distributions.items()):continue
        seen.add(key)
        if same and reuse_metrics and item.get('user_attrs',{}).get('valid') and len(item.get('values',[]))==2 and np.isfinite(item['values']).all():
            # Use original evaluated distributions, including anchor-local numeric bounds.
            saved={k:json_to_distribution(v) for k,v in item['distributions'].items()}
            for k,distribution in saved.items():optuna.distributions.check_distribution_compatibility(distributions[k],distribution)
            attrs=dict(item['user_attrs']);attrs.update(phase='history',history_source=str(Path(path).name))
            trials.append(optuna.trial.create_trial(params=params,distributions=saved,values=item['values'],user_attrs=attrs))
        else:proposals.append(dict(params))
        if len(trials)+len(proposals)>=maximum:break
    return trials,proposals,{'policy':'reuse_matching_observations' if same and reuse_metrics else 'parameters_only_reevaluate',
                            'identity_matches':same,'imported_trials':len(trials),'reevaluation_proposals':len(proposals)}
