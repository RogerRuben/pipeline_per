# -*- coding: utf-8 -*-
import copy,itertools,json
import numpy as np
from scipy.optimize import minimize
from ..optuna_v2 import price_automl_core as core
from ..optuna_v4.metrics import metrics,violation

def make_folds(frame,rows,cfg):
    r=cfg['v7']['review'];groups=np.unique(frame.values,axis=0,return_inverse=True)[1];unique=np.unique(groups)
    if len(unique)<2:raise ValueError('独立配置不足，不能进行共同开发复核。')
    count=len(unique) if len(frame)<r['tiny_threshold'] else min(r['folds'],len(unique),len(frame)//2)
    rng=np.random.RandomState(r['seed']);order=rng.permutation(unique).tolist();order.sort(key=lambda g:-int(np.sum(groups==g)))
    bins=[[] for _ in range(count)]
    for g in order:
        sizes=[sum(int(np.sum(groups==x)) for x in b) for b in bins];choices=np.flatnonzero(np.asarray(sizes)==min(sizes))
        bins[int(rng.choice(choices))].append(g)
    folds=[]
    for j,b in enumerate(bins):
        valid=np.flatnonzero(np.isin(groups,b));train=np.flatnonzero(~np.isin(groups,b))
        if len(train)<2:raise ValueError('共同开发划分后训练不足2条。')
        folds.append({'id':j,'train':train,'validation':valid})
    return folds,{'scope':'common_development_not_independent_test','uses_prices_for_partitioning':False,
        'folds':[{'id':f['id'],'train_rows':np.asarray(rows)[f['train']].tolist(),'validation_rows':np.asarray(rows)[f['validation']].tolist()} for f in folds],
        'coverage_per_record':[sum(int(i in f['validation']) for f in folds) for i in range(len(frame))]}

def recipe_key(recipe):
    p={k:v for k,v in recipe['params'].items() if k!='split_seed'}
    return json.dumps([p,recipe.get('expert_strength',1.),recipe.get('model_seeds',[0]),recipe.get('family_weights')],sort_keys=True)

def candidate_pool(baseline,frame,y,rows,cfg,history):
    selected=dict(baseline['selected_parameters']);result=[{'id':'baseline','params':selected,'expert_strength':1.,'model_seeds':[0]}]
    seen={recipe_key(result[0])};masks=set();lookup={int(x):i for i,x in enumerate(rows)}
    def rank(item):
        rv,m=item['values'];q=cfg['targets'];miss=max(0.,q['r2_min']-rv)+max(0.,m*100/q['mape_percent_max']-1.)
        return (miss>0,miss,m,-rv)
    for item in sorted(history.get('trials',[]),key=rank):
        p=item['params'];r={'id':'v3_trial_%s'%item['number'],'params':p,'expert_strength':1.,'model_seeds':[0]};key=recipe_key(r)
        if key in seen:continue
        split=core.split_data(frame,y,rows,int(p['split_seed']),cfg['validation']['test_size'],cfg['validation']['price_bins'])
        mask=tuple(sorted(int(x) for x in split[-1]))
        if mask in masks:continue
        result.append(r);seen.add(key);masks.add(mask)
        if len(result)>=cfg['v7']['review']['candidate_pool']:break
    for strength in cfg['v7']['expert_strengths']:
        r={'id':'expert_strength_%s'%strength,'params':selected,'expert_strength':float(strength),'model_seeds':[0]}
        if recipe_key(r) not in seen:result.insert(1 if strength!=1 else len(result),r);seen.add(recipe_key(r))
    return result

def shrink_weights(P,y,prior,settings):
    prior=np.asarray(prior,float);active=np.flatnonzero(prior>0)
    if len(active)<2 or len(y)<6:return prior.copy()
    A=np.asarray(P,float)[:,active]/np.asarray(y,float)[:,None];p=prior[active];penalty=settings['ridge_penalty']
    def loss(w):return float(np.mean((A.dot(w)-1.)**2)+penalty*np.sum((w-p)**2))
    fit=minimize(loss,p,method='SLSQP',bounds=[(0.,1.)]*len(active),constraints=[{'type':'eq','fun':lambda w:w.sum()-1.}],options={'maxiter':80,'ftol':1e-10})
    if not fit.success or not np.isfinite(fit.x).all():return prior.copy()
    answer=prior.copy();fraction=settings['max_prior_movement'];answer[active]=(1.-fraction)*p+fraction*np.maximum(fit.x,0.)/max(fit.x.sum(),1e-12)
    return answer/answer.sum()

def summarize(parts,anchor,y,weights):
    actual=[];pred=[];ta=[];tp=[];per_fold=[]
    for p in parts:
        a=p['validation_components'].dot(weights);b=p['train_components'].dot(weights)
        actual.extend(y[p['validation']]);pred.extend(a);ta.extend(y[p['train']]);tp.extend(b)
        per_fold.append({'id':p['id'],'train':metrics(y[p['train']],b),'validation':metrics(y[p['validation']],a)})
    av=anchor['validation_components'].dot(weights);at=anchor['train_components'].dot(weights)
    return {'validation':metrics(actual,pred),'train':metrics(ta,tp),'folds':per_fold,
        'anchor_validation':metrics(y[anchor['validation']],av),'anchor_train':metrics(y[anchor['train']],at),
        'fold_mape_std':float(np.std([p['validation']['mape_percent'] for p in per_fold]))}

def guard(score,baseline,cfg):
    r=cfg['v7']['review'];a=score['anchor_validation'];b=baseline['anchor_validation'];v=score['validation'];ref=baseline['validation'];q=cfg['targets']
    return (a['mape_percent']<=b['mape_percent']+r['anchor_mape_tolerance_pp']+1e-10 and
        a['r2'] is not None and a['r2']>=b['r2']-r['anchor_r2_tolerance'] and
        v['mape_percent']<=ref['mape_percent']+1e-10 and (ref['r2'] is None or v['r2'] is not None and v['r2']>=ref['r2']-r['cv_r2_tolerance']))

def score_key(score,cfg):
    r=cfg['v7']['review'];q=cfg['targets'];a=score['anchor_validation'];t=score.get('deployed_train',score['train'])
    miss=violation(a,q['mape_percent_max'],q['r2_min'])+violation(t,q['mape_percent_max'],q['r2_min'])
    blend=r['anchor_weight'];risk=(1.-blend)*score['validation']['mape_percent']+blend*a['mape_percent']+r['tail_weight']*score['validation']['p90_ape_percent']+.1*score['fold_mape_std']
    return ((miss>0,miss,a['mape_percent'],risk) if cfg['v7']['mode']=='goal_first' else (miss>0,miss,risk,a['mape_percent']))

def mix_results(items,weights,y):
    first=items[0];parts=[]
    for i,f in enumerate(first['parts']):
        parts.append(dict(f,validation_components=np.column_stack([p['parts'][i]['validation_components'].dot(p['weights']) for p in items]),
            train_components=np.column_stack([p['parts'][i]['train_components'].dot(p['weights']) for p in items])))
    anchor=dict(first['anchor'],validation_components=np.column_stack([p['anchor']['validation_components'].dot(p['weights']) for p in items]),
        train_components=np.column_stack([p['anchor']['train_components'].dot(p['weights']) for p in items]))
    return summarize(parts,anchor,y,np.asarray(weights,float))
