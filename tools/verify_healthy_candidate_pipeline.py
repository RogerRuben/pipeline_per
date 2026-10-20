# -*- coding: utf-8 -*-
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.product_pipeline import virtual_fixture,fixture_csv
from app.historical_onboarding import HistoricalProductOnboarding
from tools.product_price_tabpfn import train_price


def main():
    fixture=virtual_fixture()
    data=HistoricalProductOnboarding().analyze('integration.csv',fixture_csv(fixture),'NORMAL_RECOVERY_SMOKE','正常候选路径验证',['-1','\\','/'])['data']
    keys={'rated_thrust_n','speed_mm_s'}
    data['parameters']=[p for p in data['parameters'] if p['parameter_id'] in keys]
    data['agreements']=data['agreements'][:32]
    for row in data['agreements']:
        row['params']={key:value for key,value in row['params'].items() if key in keys}
        row['historical_price_wan']=6+.002*row['params']['rated_thrust_n']+.03*row['params']['speed_mm_s']
    output=ROOT/'outputs/candidate_failure_fix_20261015/normal_neural_training_file'
    bundle,report=train_price(data,output,overrides={
        'resources':{'total_seconds':300,'fit_timeout_seconds':30,'threads':1},
        'validation':{'protocol':'holdout_nested'},
        'tabpfn':{'n_estimators':1,'variants':[{'target_transform':'identity','output_type':'median'}]},
        'seed_bagging':{'milestones':[2]},'baselines':{'ridge_alphas':[.0001],'select_k':['all'],'bayesian_ridge':False},
        'quality':{'hard_gate':True}})
    assert report['targets_met'] and report['tabpfn_only']['status']=='available'
    assert (output/'tabpfn_only_bundle.pkl').is_file()
    print('PASS: actual healthy neural path, independent holdout and native export remain available',flush=True)

if __name__=='__main__':main()