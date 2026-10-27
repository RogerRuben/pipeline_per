# -*- coding: utf-8 -*-
"""Reproduce old meta-overfit and verify the preserved 300-trial V3 study."""
import hashlib,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import numpy as np
import pandas as pd
from services.price_service.optuna_v5.persistence import load_state
from services.price_service.optuna_v5.fusion import solve
from automl_pipeline.product_price_optuna_v5 import load_config


def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))


def same(a,b):
    if isinstance(a,str) and isinstance(b,str):return a==b
    try:return bool(np.isclose(float(a),float(b),rtol=1e-12,atol=1e-12))
    except (ValueError,TypeError):return a==b


def main():
    evidence=ROOT/'outputs/v51_20261027';old=ROOT/'outputs/v5_20261023/demo_v5_goal_first/price'
    baseline_dirs=list((evidence/'demo_checkpoints').glob('baseline_*'))
    if len(baseline_dirs)!=1:raise AssertionError('Expected one completed V3 baseline.')
    path=baseline_dirs[0]/'baseline';meta=read(path.with_suffix('.json'));stored=load_state(path,meta['identity'])
    old_trials=pd.read_csv(old/'baseline/all_trials.csv');trials=stored['study'].trials
    assert len(trials)==len(old_trials)==300
    for trial,(_,row) in zip(trials,old_trials.iterrows()):
        assert trial.number==row['trial']
        for key,value in trial.params.items():assert same(value,row[key]),(trial.number,key,value,row[key])
        if trial.user_attrs.get('valid'):
            assert same(trial.values[0],row['r2']) and same(trial.values[1],row['mape'])
    old_report=read(old/'training_report.json');current=read(evidence/'demo_v51_goal_first/price/training_report.json')
    assert old_report['baseline_selected_parameters']==current['baseline_selected_parameters']
    assert stored['stats']['model_fits']==old_report['fit_statistics']['baseline_search']
    initial=read(evidence/'demo_reports_before_fit_accounting.json');final=read(evidence/'demo_reports.json')
    for before,after in zip(initial,final):
        assert before['development_metrics']==after['development_metrics']
        assert before['weights']==after['weights'] and before['member_ledger']==after['member_ledger']
    y=np.array([10.,20.,30.,40.]);P=np.c_[y*.9,y*1.1]
    weights,score,_=solve(P,P,y,y,[.7,.3],load_config())
    result={'status':'PASS','v3_trials':300,'all_original_parameters_and_valid_scores_equal':True,
            'selected_parameters_equal':True,'v3_search_fits':stored['stats']['model_fits'],
            'counting_fix_changes_no_predictions':True,'core_sha256':hashlib.sha256((ROOT/'services/price_service/optuna_v2/price_automl_core.py').read_bytes()).hexdigest(),
            'old_weight_interpolation_reproduced':{'individual_mape_percent':[10.,10.],
                'original_fixed_weight_mape_percent':float(np.mean(np.abs(P.dot([.7,.3])-y)/y)*100.),
                'fitted_weights':weights.tolist(),'fitted_on_scored_labels_mape_percent':score['validation']['mape_percent'],
                'fitted_on_scored_labels_r2':score['validation']['r2']},
            'fixed_cv_reference':current['reference_cv_metrics']['validation'],'fixed_cv_selected':current['development_metrics'],
            'business_accuracy_certified':False}
    (evidence/'parity_and_root_cause.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
