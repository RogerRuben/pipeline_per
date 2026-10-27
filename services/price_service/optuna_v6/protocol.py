# -*- coding: utf-8 -*-
import hashlib
import json
import numpy as np
from ..optuna_v4.metrics import metrics,violation


def groups_for(frame,cfg):
    field=cfg['v6']['evaluation']['group_field']
    if field:
        if field not in frame.columns:raise ValueError('分组属性不存在：'+field)
        # Also isolate identical predictors, even if a supplied group is malformed.
        raw=frame[field].values
    else:raw=np.asarray(frame.values)
    return np.unique(raw,axis=0,return_inverse=True)[1]


def folds_for(indices,groups,cfg,outer=False,scope=0):
    indices=np.asarray(indices,int);e=cfg['v6']['evaluation'];local=groups[indices]
    unique=np.unique(local);n=len(indices)
    if len(unique)<2:raise ValueError('独立成品组不足，不能隔离验收。')
    tiny=n<e['tiny_threshold']
    count=len(unique) if tiny else min(e['outer_splits'] if outer else e['inner_splits'],len(unique),n//2)
    repeats=e['outer_repeats'] if outer else e['inner_repeats']
    result=[]
    for repeat in range(repeats):
        rng=np.random.RandomState((e['seed']+scope*97+repeat)%(2**32))
        ordered=rng.permutation(unique).tolist()
        ordered.sort(key=lambda g:-int(np.sum(local==g)))
        bins=[[] for _ in range(count)]
        for g in ordered:
            sizes=[sum(int(np.sum(local==x)) for x in b) for b in bins]
            choices=np.flatnonzero(np.asarray(sizes)==min(sizes));bins[int(rng.choice(choices))].append(g)
        for b in bins:
            validation=indices[np.isin(local,b)];train=np.setdiff1d(indices,validation)
            if len(train)<2 or not len(validation):raise ValueError('分组隔离后训练不足2条，请增加独立记录或采用预设模型试用。')
            result.append({'id':len(result),'repeat':repeat,'train':np.sort(train),'validation':np.sort(validation)})
    coverage={int(i):sum(int(i in f['validation']) for f in result) for i in indices}
    if set(coverage.values())!={repeats}:raise AssertionError('每条记录每次重复必须恰好验收一次。')
    return result


def serialize(folds,rows):
    return [{'id':f['id'],'repeat':f.get('repeat',0),'train_rows':rows[f['train']].tolist(),'validation_rows':rows[f['validation']].tolist()} for f in folds]


def make_plan(frame,rows,ids,cfg):
    groups=groups_for(frame,cfg);e=cfg['v6']['evaluation'];all_rows=np.arange(len(frame))
    if e['mode']=='locked_holdout':
        if e['holdout_ids']:
            unknown=set(e['holdout_ids'])-set(ids)
            if unknown:raise ValueError('外层留出编号不存在：'+','.join(sorted(unknown)))
            selected=[i for i,key in enumerate(ids) if key in e['holdout_ids']]
        else:
            selected=np.random.RandomState(e['seed']).choice(len(frame),max(1,int(np.ceil(len(frame)*e['holdout_fraction']))),replace=False)
        held_groups=set(groups[selected]);test=np.flatnonzero(np.isin(groups,list(held_groups)));dev=np.setdiff1d(all_rows,test)
        if len(dev)<3:raise ValueError('固定留出后至少需要3条开发记录进行小样本内部练习。')
        outer=[{'id':0,'repeat':0,'train':dev,'validation':test}]
    else:
        dev=all_rows;outer=folds_for(dev,groups,cfg,True)
    inner=[]
    for f in outer:
        try:inner.append(folds_for(f['train'],groups,cfg,False,f['id']+1))
        except ValueError:inner.append([]) # predeclared fixed model branch, never split a group
    final=folds_for(dev,groups,cfg,False,900)
    public={'outer':serialize(outer,rows),'inner':[serialize(f,rows) for f in inner],
            'final_inner':serialize(final,rows),'group_field':e['group_field'],'group_isolation':True,
            'tiny_single_record_validation':len(dev)<e['tiny_threshold'],'outer_mode':e['mode'],
            'uses_prices_for_partitioning':False,'development_rows':rows[dev].tolist()}
    public['sha256']=hashlib.sha256(json.dumps(public,sort_keys=True).encode('utf-8')).hexdigest()
    return {'outer':outer,'inner':inner,'final':final,'dev':dev,'groups':groups,'public':public}


def assessment(train,validation,cfg):
    t=metrics(*train);v=metrics(*validation);g=cfg['v6']['train_gate'];q=cfg['targets']
    tm=0. if g['mode']=='off' else violation(t,g['mape_percent_max'],g['r2_min'])
    vm=violation(v,q['mape_percent_max'],q['r2_min'])
    return {'train':t,'validation':v,'train_violation':tm,'validation_violation':vm,'qualified':tm==0 and vm==0}


def rank(score,cfg):
    g=cfg['v6']['train_gate'];miss=score['validation_violation']+score['train_violation']*(g['soft_penalty'] if g['mode']=='soft' else 1.)
    v=score['validation'];tail=cfg['v6']['selection']['tail_weight']
    risk=v['max_ape_percent'] if cfg['v6']['mode']=='robust' else v['p90_ape_percent']
    return (not score['qualified'],miss,v['mape_percent']+tail*risk,1.-v['r2'] if v['r2'] is not None else 1e9)


def loss(score,cfg):
    g=cfg['v6']['train_gate'];v=score['validation']
    miss=score['validation_violation']+score['train_violation']*(g['soft_penalty'] if g['mode']=='soft' else 1.)
    return float((0. if score['qualified'] else 1000.)+10.*miss+np.log1p(v['mape_percent'])+.1*np.log1p(max(0.,1.-v['r2'] if v['r2'] is not None else 1.)))
