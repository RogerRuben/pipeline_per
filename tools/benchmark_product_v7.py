# -*- coding: utf-8 -*-
"""Ablations on harder virtual worlds; external labels never reach training."""
import argparse,copy,hashlib,json,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import app
app.__path__.insert(0,str(ROOT/'app'))
import numpy as np
from product_optuna_v6_test import data_fixture
from automl_pipeline.product_price_optuna_v7 import train_price_optuna_v7,load_config
from services.price_service.native_bundle import predict_batch,load_bundle
from services.price_service.optuna_v4.metrics import metrics

def world(n=16,p=36,seed=731,noise=.06,correlation=.9,cover_categories=False):
    data=data_fixture(n,p);rng=np.random.RandomState(seed);z=rng.normal(size=(n,3));raw=rng.normal(size=(n,p))
    for i,row in enumerate(data['agreements']):
        for j in range(p):
            if j==0:value=2.+z[i,0]
            elif j==1:value=(2.+correlation*z[i,0]+np.sqrt(1-correlation**2)*raw[i,j])*80.
            elif j==2:value=int(np.clip(round(3.+z[i,1]),1,5))
            elif j==3:value=('甲' if i%2 else '乙') if cover_categories else ('甲' if raw[i,j]>0 else '乙')
            elif j==4:value=('001' if i%3 else '002') if cover_categories else ('001' if raw[i,j]>0 else '002')
            elif j==5:value=(2.+z[i,2])*7200.
            else:value=(2.+raw[i,j])*[1.,80.,7200.][j%3]
            row['params']['x%d'%j]=value
        row['historical_price_wan']=float(45.*np.exp(.22*z[i,0]+.08*(row['params']['x2']-3)+.06*z[i,2]+(.04 if row['params']['x3']=='甲' else 0)+rng.normal(0,noise)))
    return data

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',default=str(ROOT/'outputs/v7_20261030/release_benchmark'));parser.add_argument('--trials',type=int,default=12);parser.add_argument('--ablation',action='store_true');parser.add_argument('--seed-offset',type=int,default=0);parser.add_argument('--final-evaluation',action='store_true');a=parser.parse_args()
    out=Path(a.output);out.mkdir(parents=True,exist_ok=True);records=[]
    cases=[('independent_noise',16,36,.04,0.),('correlated_noise',16,36,.08,.98),('ten_by_fifty',10,50,.06,.8),('tiny_four',4,40,.05,.8),('tiny_seven',7,40,.08,.95)]
    training_files=list((ROOT/'services/price_service/optuna_v7').glob('*.py'))+[ROOT/'automl_pipeline/product_optuna_v7_adapter.py',ROOT/'automl_pipeline/product_price_optuna_v7.py']
    frozen={str(f.relative_to(ROOT)):hashlib.sha256(f.read_bytes()).hexdigest() for f in training_files}
    for index,(name,n,p,noise,corr) in enumerate(cases):
        data=world(n,p,1000+a.seed_offset+index,noise,corr,a.final_evaluation);external=world(160,p,9000+a.seed_offset+index,noise,corr,a.final_evaluation)
        cfg=load_config(overrides={'search':{'n_trials':a.trials,'timeout_seconds':180},
            'expert':{'scores':{'x%d'%j:(5 if j==0 else 3 if j==1 else 4 if j==5 else 1) for j in range(p)},'field_types':{'x3':'enum','x4':'enum'}},
            'v7':{'budget':{'extra_seconds':120},'persistence':{'directory':str(out/'checkpoints')}}})
        directory=out/name
        if (directory/'training_report.json').is_file():r=json.loads((directory/'training_report.json').read_text(encoding='utf-8'));bundle=load_bundle(directory/'price_native_bundle.pkl')
        else:bundle,r=train_price_optuna_v7(data,directory,overrides=cfg,progress=lambda s:None)
        base=load_bundle(directory/'baseline_v3/price_native_bundle.pkl');inputs=[r['params'] for r in external['agreements']];actual=[r['historical_price_wan'] for r in external['agreements']]
        baseline_external=metrics(actual,[x['predicted_price_wan'] for x in predict_batch(base,inputs)])
        enhanced_external=metrics(actual,[x['predicted_price_wan'] for x in predict_batch(bundle,inputs)])
        row={'case':name,'rows':n,'features':p,'noise':noise,'correlation':corr,'baseline_external':baseline_external,'v7_external':enhanced_external,
            'baseline_common_development':r['baseline_v3']['common_development'],'v7_common_development':r['common_development_metrics'],
            'enhancement_applied':r['enhancement_applied'],'selected_model':r['selected_model'],'extra_fits':r['extra_fits_this_session'],
            'extra_fit_limit':r['fit_statistics']['extra_fit_limit'],'seconds':r['elapsed_seconds'],'external_data_used_for_selection':False}
        records.append(row)
        assert frozen=={str(f.relative_to(ROOT)):hashlib.sha256(f.read_bytes()).hexdigest() for f in training_files},'Training method changed during final simulation'
        (out/'benchmark_report.json').write_text(json.dumps({'status':'MEASURED','cases':records,'all_cases_kept':True,'business_accuracy_certified':False,
            'seed_offset':a.seed_offset,'method_frozen_before_generation':a.final_evaluation,'training_code_sha256':frozen,
            'scope':'new_seed_virtual_cohort_not_real_business_validation' if a.final_evaluation else 'method_development_diagnostics'},ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(row,ensure_ascii=False),flush=True)
        if a.ablation and index<2:
            for label,changes in [('review_only',{'model_seeds':{'enabled':False},'fusion':{'enabled':False},'expert_strengths':[1.]}),
                                  ('with_seed_average',{'fusion':{'enabled':False},'expert_strengths':[1.]}),
                                  ('with_expert_soft',{'consistency':{'mode':'soft','permutations':4}})]:
                variant=copy.deepcopy(cfg)
                for section,change in changes.items():
                    if isinstance(change,dict):variant['v7'][section].update(change)
                    else:variant['v7'][section]=change
                directory=out/(name+'_'+label)
                if (directory/'training_report.json').is_file():vr=json.loads((directory/'training_report.json').read_text(encoding='utf-8'));vb=load_bundle(directory/'price_native_bundle.pkl')
                else:vb,vr=train_price_optuna_v7(data,directory,overrides=variant,progress=lambda s:None)
                record={'case':name,'ablation':label,'baseline_external':baseline_external,'v7_external':metrics(actual,[x['predicted_price_wan'] for x in predict_batch(vb,inputs)]),
                    'common_development':vr['common_development_metrics'],'extra_fits':vr['extra_fits_this_session'],'baseline_new_fits':vr['baseline_new_fits'],
                    'consistency':vr['expert_grade_consistency'],'selected_model':vr['selected_model'],'external_data_used_for_selection':False}
                records.append(record);(out/'benchmark_report.json').write_text(json.dumps({'status':'MEASURED','cases':records,'all_cases_kept':True,'business_accuracy_certified':False},ensure_ascii=False,indent=2),encoding='utf-8')
                print(json.dumps({k:v for k,v in record.items() if k!='consistency'},ensure_ascii=False),flush=True)
    # The report deliberately retains regressions on unseen virtual data.
    return 0
if __name__=='__main__':sys.exit(main())
