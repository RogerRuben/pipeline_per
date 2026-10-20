# -*- coding: utf-8 -*-
import itertools
import math
import numpy as np
from ..optuna_v2.price_automl_core import make_price_bins


def holdout_indices(n,ids,groups,cfg):
    e=cfg['v4']['evaluation']
    if e['mode']=='development':return np.arange(n,dtype=int),np.asarray([],dtype=int)
    if e['holdout_ids']:
        unknown=set(e['holdout_ids'])-set(ids)
        if unknown:raise ValueError('外层留出编号不存在：'+','.join(sorted(unknown)))
        test=np.asarray([i for i,key in enumerate(ids) if key in set(e['holdout_ids'])],int)
    else:
        count=max(2,int(math.ceil(n*e['holdout_fraction'])))
        test=np.sort(np.random.RandomState(e['holdout_seed']).choice(n,count,replace=False))
    if cfg['v4']['splits']['group_identical_inputs'] or cfg['v4']['splits']['group_field']:
        selected=set(groups[i] for i in test);test=np.asarray([i for i,g in enumerate(groups) if g in selected],int)
    train=np.asarray([i for i in range(n) if i not in set(test)],int)
    if len(test)<2 or len(train)<cfg['input']['min_samples']:
        raise ValueError('固定外层留出至少2条，开发数据至少%d条；当前剩%d条。请增加记录或切换development。'%(cfg['input']['min_samples'],len(train)))
    return train,test


def make_splits(y,groups,cfg):
    n=len(y);p=cfg['v4']['splits'];k=p['validation_rows'] or max(2,min(n-2,int(math.ceil(n*cfg['validation']['test_size']))))
    if k>n-2:raise ValueError('验证条数过多，训练至少保留2条。')
    total=math.comb(n,k);bins=make_price_bins(y,k/float(n),cfg['validation']['price_bins']) if p['stratify'] else None
    enforce_groups=p['group_identical_inputs'] or p['group_field'] is not None
    group_sets={g:set(np.flatnonzero(np.asarray(groups)==g)) for g in set(groups)} if enforce_groups else {}
    def valid(combo):
        selected=set(combo)
        if any(selected&members and not members<=selected for members in group_sets.values()):return False
        if bins is not None:
            if set(bins[list(combo)])!=set(bins) or set(bins[[i for i in range(n) if i not in selected]])!=set(bins):return False
        return True
    cap=p['max_splits'];coverage=p['coverage'];rng=np.random.RandomState(p['seed'])
    if coverage=='exhaustive' and total>p['enumeration_limit']:raise ValueError('组合数%d超过enumeration_limit，请提高上限或使用auto/sampled。'%total)
    if total<=p['enumeration_limit']:
        eligible=[tuple(c) for c in itertools.combinations(range(n),k) if valid(c)];eligible_total=len(eligible)
        if coverage=='exhaustive' or (coverage=='auto' and (not cap or len(eligible)<=cap)):selected=eligible
        else:
            limit=min(len(eligible),cap or p['random_splits']);selected=[eligible[i] for i in np.sort(rng.choice(len(eligible),limit,replace=False))] if limit else []
    else:
        eligible_total=None;limit=cap or p['random_splits'];seen=set()
        for _ in range(max(1000,limit*500)):
            combo=tuple(sorted(int(x) for x in rng.choice(n,k,replace=False)))
            if valid(combo):seen.add(combo)
            if len(seen)>=limit:break
        selected=sorted(seen)
    if not selected:raise ValueError('没有满足条数、同成品分组及分层约束的划分；请检查重复输入、group_field或划分设置。')
    result=[]
    for j,combo in enumerate(selected):
        result.append({'id':j,'train':np.asarray([i for i in range(n) if i not in set(combo)],int),'validation':np.asarray(combo,int)})
    report={'records':n,'validation_rows':k,'training_rows':n-k,'theoretical_combinations':total,'eligible_combinations':eligible_total,
            'evaluated_splits':len(result),'exhaustive':eligible_total is not None and len(result)==eligible_total,
            'coverage_policy':coverage,'canonical_training_order':True,'group_isolation':bool(enforce_groups),'price_stratification':p['stratify']}
    return result,report


def coverage_subset(splits,limit):
    if limit>=len(splits):return list(splits)
    pending=list(splits);selected=[];covered=set()
    while pending and len(selected)<limit:
        best=max(pending,key=lambda f:(len(set(f['validation'])-covered),-f['id']))
        selected.append(best);covered.update(best['validation']);pending=[f for f in pending if f['id']!=best['id']]
    return selected
