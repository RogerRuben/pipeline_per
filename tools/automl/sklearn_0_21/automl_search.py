# -*- coding: utf-8 -*-
"""Robust multi-objective AutoML for small-sample regression.

Objectives:
- minimize robust MAPE (%)
- maximize robust R^2

Every trial is evaluated across multiple shuffled KFold seeds. After tuning,
a representative seed (closest to median multi-seed behavior) is selected for
reproducibility. It is NOT the luckiest seed.
"""
from __future__ import print_function

import argparse
import json
import math
import sys
import warnings
from pathlib import Path

import joblib
import numpy as np
import optuna
import pandas as pd
import sklearn
from sklearn.ensemble import ExtraTreesRegressor, GradientBoostingRegressor, RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import BayesianRidge, ElasticNet, Lasso, Ridge
from sklearn.metrics import r2_score
from sklearn.model_selection import KFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVR

EXPECTED_SKLEARN = '0.21'
if not sklearn.__version__.startswith(EXPECTED_SKLEARN):
    warnings.warn('Prepared for sklearn %s.x, current=%s' % (EXPECTED_SKLEARN, sklearn.__version__))

def mape_percent(y_true, y_pred, eps=1e-8):
    # sklearn 0.21 has no mean_absolute_percentage_error.
    # Return percent units: 8.25 means 8.25%.
    yt=np.asarray(y_true,dtype=float); yp=np.asarray(y_pred,dtype=float)
    den=np.maximum(np.abs(yt),float(eps))
    return float(np.mean(np.abs((yt-yp)/den))*100.0)

SUPPORTED_MODELS = [
    'ridge', 'lasso', 'elasticnet', 'bayesian_ridge',
    'svr', 'random_forest', 'extra_trees', 'gbdt'
]


def safe_r2(y_true, y_pred):
    v = r2_score(y_true, y_pred)
    return float(v) if np.isfinite(v) else -1e12


def suggest_config(trial, model_names):
    name = trial.suggest_categorical('model', model_names)
    c = {'model': name}
    if name == 'ridge':
        c['alpha'] = trial.suggest_float('ridge_alpha', 1e-5, 1e4, log=True)
    elif name == 'lasso':
        c['alpha'] = trial.suggest_float('lasso_alpha', 1e-6, 10.0, log=True)
    elif name == 'elasticnet':
        c['alpha'] = trial.suggest_float('enet_alpha', 1e-6, 10.0, log=True)
        c['l1_ratio'] = trial.suggest_float('enet_l1_ratio', 0.02, 0.98)
    elif name == 'bayesian_ridge':
        c['alpha_1'] = trial.suggest_float('br_alpha_1', 1e-8, 1e-3, log=True)
        c['alpha_2'] = trial.suggest_float('br_alpha_2', 1e-8, 1e-3, log=True)
        c['lambda_1'] = trial.suggest_float('br_lambda_1', 1e-8, 1e-3, log=True)
        c['lambda_2'] = trial.suggest_float('br_lambda_2', 1e-8, 1e-3, log=True)
    elif name == 'svr':
        c['C'] = trial.suggest_float('svr_C', 1e-2, 1e4, log=True)
        c['epsilon'] = trial.suggest_float('svr_epsilon', 1e-4, 1.0, log=True)
        c['gamma'] = trial.suggest_float('svr_gamma', 1e-5, 10.0, log=True)
    elif name in ('random_forest', 'extra_trees'):
        p = 'rf' if name == 'random_forest' else 'et'
        c['n_estimators'] = trial.suggest_int(p+'_n_estimators', 80, 600)
        c['max_depth'] = trial.suggest_int(p+'_max_depth', 1, 8)
        c['min_samples_leaf'] = trial.suggest_int(p+'_min_samples_leaf', 1, 8)
        c['min_samples_split'] = trial.suggest_int(p+'_min_samples_split', 2, 12)
        c['max_features'] = trial.suggest_float(p+'_max_features', 0.30, 1.00)
    elif name == 'gbdt':
        c['n_estimators'] = trial.suggest_int('gbdt_n_estimators', 30, 500)
        c['learning_rate'] = trial.suggest_float('gbdt_learning_rate', 0.005, 0.20, log=True)
        c['max_depth'] = trial.suggest_int('gbdt_max_depth', 1, 4)
        c['min_samples_leaf'] = trial.suggest_int('gbdt_min_samples_leaf', 1, 8)
        c['min_samples_split'] = trial.suggest_int('gbdt_min_samples_split', 2, 12)
        c['subsample'] = trial.suggest_float('gbdt_subsample', 0.55, 1.0)
        c['max_features'] = trial.suggest_float('gbdt_max_features', 0.30, 1.00)
    return c


def build_estimator(c, seed):
    n = c['model']
    if n == 'ridge':
        return Ridge(alpha=float(c['alpha']))
    if n == 'lasso':
        return Lasso(alpha=float(c['alpha']), max_iter=20000)
    if n == 'elasticnet':
        return ElasticNet(alpha=float(c['alpha']), l1_ratio=float(c['l1_ratio']), max_iter=20000)
    if n == 'bayesian_ridge':
        return BayesianRidge(alpha_1=float(c['alpha_1']), alpha_2=float(c['alpha_2']),
                             lambda_1=float(c['lambda_1']), lambda_2=float(c['lambda_2']))
    if n == 'svr':
        return SVR(kernel='rbf', C=float(c['C']), epsilon=float(c['epsilon']), gamma=float(c['gamma']))
    if n == 'random_forest':
        return RandomForestRegressor(n_estimators=int(c['n_estimators']), max_depth=int(c['max_depth']),
            min_samples_leaf=int(c['min_samples_leaf']), min_samples_split=int(c['min_samples_split']),
            max_features=float(c['max_features']), random_state=int(seed), n_jobs=1)
    if n == 'extra_trees':
        return ExtraTreesRegressor(n_estimators=int(c['n_estimators']), max_depth=int(c['max_depth']),
            min_samples_leaf=int(c['min_samples_leaf']), min_samples_split=int(c['min_samples_split']),
            max_features=float(c['max_features']), random_state=int(seed), n_jobs=1)
    if n == 'gbdt':
        return GradientBoostingRegressor(n_estimators=int(c['n_estimators']),
            learning_rate=float(c['learning_rate']), max_depth=int(c['max_depth']),
            min_samples_leaf=int(c['min_samples_leaf']), min_samples_split=int(c['min_samples_split']),
            subsample=float(c['subsample']), max_features=float(c['max_features']),
            loss='huber', random_state=int(seed))
    raise ValueError('Unknown model: %s' % n)


def build_pipeline(c, seed):
    scale_models = {'ridge','lasso','elasticnet','bayesian_ridge','svr'}
    steps = [('imputer', SimpleImputer(strategy='median'))]
    if c['model'] in scale_models:
        steps.append(('scaler', StandardScaler()))
    steps.append(('model', build_estimator(c, seed)))
    return Pipeline(steps)


def evaluate_config(X, y, c, seeds, folds, log_target):
    per_seed, all_m, all_r = [], [], []
    for seed in seeds:
        kf = KFold(n_splits=folds, shuffle=True, random_state=int(seed))
        sm, sr = [], []
        for tr, va in kf.split(X):
            pipe = build_pipeline(c, seed)
            Xtr, Xva = X.iloc[tr], X.iloc[va]
            ytr, yva = y.iloc[tr], y.iloc[va]
            if log_target:
                if np.any(np.asarray(ytr) < 0):
                    raise ValueError('--log-target requires non-negative target.')
                pipe.fit(Xtr, np.log1p(np.asarray(ytr, dtype=float)))
                pred = np.expm1(pipe.predict(Xva))
            else:
                pipe.fit(Xtr, ytr)
                pred = pipe.predict(Xva)
            m, r = mape_percent(yva, pred), safe_r2(yva, pred)
            sm.append(m); sr.append(r); all_m.append(m); all_r.append(r)
        per_seed.append({'seed': int(seed), 'mean_mape': float(np.mean(sm)),
                         'std_mape': float(np.std(sm)), 'mean_r2': float(np.mean(sr)),
                         'std_r2': float(np.std(sr))})
    return {
        'mean_mape': float(np.mean(all_m)), 'std_mape': float(np.std(all_m)),
        'median_mape': float(np.median(all_m)), 'p90_mape': float(np.percentile(all_m,90)),
        'worst_mape': float(np.max(all_m)), 'mean_r2': float(np.mean(all_r)),
        'std_r2': float(np.std(all_r)), 'median_r2': float(np.median(all_r)),
        'p10_r2': float(np.percentile(all_r,10)), 'worst_r2': float(np.min(all_r)),
        'per_seed': per_seed
    }


def representative_seed(rows):
    d = pd.DataFrame(rows).copy()
    mm, mr = float(d.mean_mape.median()), float(d.mean_r2.median())
    sm = max(float(d.mean_mape.std()), 1e-12)
    sr = max(float(d.mean_r2.std()), 1e-12)
    d['representative_distance'] = np.sqrt(((d.mean_mape-mm)/sm)**2 + ((d.mean_r2-mr)/sr)**2)
    d = d.sort_values(['representative_distance','seed']).reset_index(drop=True)
    return int(d.loc[0,'seed']), d


def objective_factory(X, y, model_names, seeds, folds, log_target, lam):
    def objective(trial):
        c = suggest_config(trial, model_names)
        m = evaluate_config(X,y,c,seeds,folds,log_target)
        trial.set_user_attr('config', c)
        for k,v in m.items():
            if k != 'per_seed': trial.set_user_attr(k,v)
        return m['mean_mape'] + lam*m['std_mape'], m['mean_r2'] - lam*m['std_r2']
    return objective


def select_trial(study, target_mape=None, target_r2=None, selection='balanced'):
    pool = list(study.best_trials)
    q = []
    for t in pool:
        ok = (target_mape is None or t.user_attrs['mean_mape'] <= target_mape) and \
             (target_r2 is None or t.user_attrs['mean_r2'] >= target_r2)
        if ok: q.append(t)
    use = q if q else pool
    if selection == 'mape': return min(use,key=lambda t:t.user_attrs['mean_mape']), bool(q)
    if selection == 'r2': return max(use,key=lambda t:t.user_attrs['mean_r2']), bool(q)
    ms=np.array([t.user_attrs['mean_mape'] for t in use]); rs=np.array([t.user_attrs['mean_r2'] for t in use])
    md=max(float(ms.max()-ms.min()),1e-12); rd=max(float(rs.max()-rs.min()),1e-12)
    def dist(t):
        mb=(t.user_attrs['mean_mape']-float(ms.min()))/md
        rb=(float(rs.max())-t.user_attrs['mean_r2'])/rd
        return math.sqrt(mb*mb+rb*rb)
    return min(use,key=dist), bool(q)


def csv_list(s): return [x.strip() for x in str(s).split(',') if x.strip()]
def int_list(s): return [int(x) for x in csv_list(s)]


def load_data(path,target,drop_cols):
    df=pd.read_csv(path)
    if target not in df.columns: raise ValueError('Target not found: %s' % target)
    drops=[c for c in drop_cols if c in df.columns]
    y=pd.to_numeric(df[target],errors='raise')
    X=df.drop(columns=[target]+drops)
    bad=[c for c in X.columns if not pd.api.types.is_numeric_dtype(X[c])]
    if bad: raise ValueError('Encode/drop non-numeric features first: %s' % bad)
    return X,y


def predict_bundle(bundle, X):
    missing=[c for c in bundle['feature_names'] if c not in X.columns]
    if missing: raise ValueError('Missing features: %s' % missing)
    p=bundle['pipeline'].predict(X.loc[:,bundle['feature_names']])
    return np.expm1(p) if bundle['log_target'] else np.asarray(p,dtype=float)


def train(args):
    out=Path(args.output); out.mkdir(parents=True,exist_ok=True)
    seeds=int_list(args.cv_seeds); models=csv_list(args.models) or SUPPORTED_MODELS
    unknown=[m for m in models if m not in SUPPORTED_MODELS]
    if unknown: raise ValueError('Unsupported models: %s' % unknown)
    X,y=load_data(args.csv,args.target,csv_list(args.drop_cols))
    study=optuna.create_study(directions=['minimize','maximize'],
        sampler=optuna.samplers.TPESampler(seed=args.search_seed))
    study.optimize(objective_factory(X,y,models,seeds,args.folds,args.log_target,args.stability_lambda),
                   n_trials=args.trials,n_jobs=args.optuna_jobs,show_progress_bar=False)

    rows=[]
    for t in study.trials:
        r={'trial':t.number,'state':str(t.state)}
        if t.values is not None: r.update({'robust_mape':t.values[0],'robust_r2':t.values[1]})
        if 'config' in t.user_attrs:
            r['model']=t.user_attrs['config']['model']; r['config_json']=json.dumps(t.user_attrs['config'],ensure_ascii=False)
        for k,v in t.user_attrs.items():
            if k!='config': r[k]=v
        rows.append(r)
    pd.DataFrame(rows).to_csv(out/'all_trials.csv',index=False)

    prows=[]
    for t in study.best_trials:
        prows.append({'trial':t.number,'robust_mape':t.values[0],'robust_r2':t.values[1],
          'model':t.user_attrs['config']['model'],'mean_mape':t.user_attrs['mean_mape'],
          'std_mape':t.user_attrs['std_mape'],'mean_r2':t.user_attrs['mean_r2'],
          'std_r2':t.user_attrs['std_r2'],'p90_mape':t.user_attrs['p90_mape'],
          'p10_r2':t.user_attrs['p10_r2'],'config_json':json.dumps(t.user_attrs['config'],ensure_ascii=False)})
    pd.DataFrame(prows).to_csv(out/'pareto_trials.csv',index=False)

    chosen,met=select_trial(study,args.target_mape,args.target_r2,args.selection)
    c=dict(chosen.user_attrs['config'])
    metrics=evaluate_config(X,y,c,seeds,args.folds,args.log_target)
    seed,seed_df=representative_seed(metrics['per_seed']); seed_df.to_csv(out/'seed_report.csv',index=False)
    pipe=build_pipeline(c,seed)
    pipe.fit(X,np.log1p(np.asarray(y,dtype=float)) if args.log_target else y)
    bundle={'pipeline':pipe,'log_target':bool(args.log_target),'feature_names':list(X.columns),
            'config':c,'representative_seed':seed,'sklearn_version':sklearn.__version__,
            'optuna_version':optuna.__version__}
    joblib.dump(bundle,out/'final_model.joblib')
    summary={'selected_trial':chosen.number,'selected_config':c,'thresholds_satisfied':met,
             'target_mape':args.target_mape,'target_r2':args.target_r2,'representative_seed':seed,
             'cv_seeds':seeds,'folds':args.folds,'log_target':bool(args.log_target),
             'stability_lambda':args.stability_lambda,
             'metrics':{k:v for k,v in metrics.items() if k!='per_seed'},
             'environment':{'python':sys.version,'sklearn':sklearn.__version__,'optuna':optuna.__version__,
                            'numpy':np.__version__,'pandas':pd.__version__}}
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Selected:',c['model'],' seed=',seed)
    print('Mean MAPE(%)=',round(metrics['mean_mape'],4),' Mean R2=',round(metrics['mean_r2'],4))
    if (args.target_mape is not None or args.target_r2 is not None) and not met:
        print('WARNING: no Pareto candidate met all requested thresholds simultaneously.')


def predict(args):
    b=joblib.load(args.predict_model); df=pd.read_csv(args.predict_csv)
    df['prediction']=predict_bundle(b,df)
    p=Path(args.output)
    if p.suffix.lower()!='.csv': p.mkdir(parents=True,exist_ok=True); p=p/'predictions.csv'
    else: p.parent.mkdir(parents=True,exist_ok=True)
    df.to_csv(p,index=False); print('Saved:',p)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--csv'); ap.add_argument('--target'); ap.add_argument('--drop-cols',default='')
    ap.add_argument('--models',default=','.join(SUPPORTED_MODELS)); ap.add_argument('--trials',type=int,default=300)
    ap.add_argument('--folds',type=int,default=5); ap.add_argument('--cv-seeds',default='42,137,256,512,1024')
    ap.add_argument('--search-seed',type=int,default=2026); ap.add_argument('--stability-lambda',type=float,default=0.5)
    ap.add_argument('--log-target',action='store_true'); ap.add_argument('--target-mape',type=float,default=None)
    ap.add_argument('--target-r2',type=float,default=None); ap.add_argument('--selection',choices=['balanced','mape','r2'],default='balanced')
    ap.add_argument('--optuna-jobs',type=int,default=1); ap.add_argument('--output',default='automl_outputs')
    ap.add_argument('--predict-model'); ap.add_argument('--predict-csv')
    a=ap.parse_args()
    if a.predict_model:
        if not a.predict_csv: ap.error('--predict-csv required with --predict-model')
        predict(a)
    else:
        if not a.csv or not a.target: ap.error('--csv and --target are required')
        train(a)
if __name__=='__main__': main()
