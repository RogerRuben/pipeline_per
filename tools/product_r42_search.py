# -*- coding: utf-8 -*-
"""Grouped repeated CV and bounded OOF blending for the pinned 0.24 engine."""
import numpy as np
from scipy.optimize import minimize
from sklearn.model_selection import LeaveOneGroupOut


def folds(X,y,groups,validation,level):
    groups=np.asarray(groups); unique=np.unique(groups); n=len(unique)
    strategy=validation[level+"_strategy"]
    if strategy=="auto":
        threshold=validation["loo_inner_max_groups"] if level=="inner" else validation["micro_max_groups"]
        strategy="loo" if n<=threshold else "group_kfold"
    if strategy=="loo": return list(LeaveOneGroupOut().split(X,y,groups))
    k=min(n,int(validation[level+"_folds"])); result=[]
    prices=np.asarray([np.median(y[groups==g]) for g in unique])
    for repeat in range(validation[level+"_repeats"]):
        rng=np.random.RandomState(validation["seed"]+(7001 if level=="outer" else 0)+repeat*104729)
        if strategy=="price_balanced":
            # Distribute successive price ranks over folds. Duplicate inputs
            # stay in one group. No feature processing sees validation rows.
            ordered=np.argsort(prices,kind="mergesort"); bins=[[] for _ in range(k)]
            for offset in range(0,n,k):
                block=ordered[offset:offset+k]; destinations=rng.permutation(k)
                for j,index in enumerate(block): bins[destinations[j]].append(index)
        else: bins=[list(a) for a in np.array_split(rng.permutation(n),k)]
        for indices in bins:
            test=np.flatnonzero(np.isin(groups,unique[indices])); train=np.flatnonzero(~np.isin(groups,unique[indices]))
            if len(test)==0 or len(train)<3: raise ValueError("CV划分后的训练记录不足3条。")
            result.append((train,test))
    return result


def bounded_blend(y,bank,config,_fixed=None):
    """All weights satisfy the requested minimum count, floor and ceiling."""
    y=np.asarray(y,float); bank=np.asarray(bank,float); e=config["ensemble"]
    alpha=e["alpha"]; sst=float(np.sum((y-y.mean())**2)); variance=max(sst,1e-12)
    rweight=1-alpha if sst>1e-12 else 0.
    def business(p): return float(alpha*np.mean(np.abs(p-y)/y)+rweight*np.sum((p-y)**2)/variance)
    order=sorted(range(bank.shape[1]),key=lambda i:business(bank[:,i]))
    distinct=[]
    for i in order:
        if not any(np.allclose(bank[:,i],bank[:,j],rtol=1e-9,atol=1e-10) for j in distinct): distinct.append(i)
        if len(distinct)>=e["candidate_pool"]: break
    minimum=min(e["min_members"],len(distinct))
    if minimum<1: raise ValueError("没有有效折外预测可供融合。")
    chosen=list(_fixed) if _fixed is not None else [distinct[0]]; current=bank[:,chosen[0]]
    while _fixed is None and len(chosen)<minimum:
        candidates=[]
        for i in distinct:
            if i in chosen: continue
            trial=(1-.15)*current+.15*bank[:,i]
            candidates.append((business(trial),i,trial))
        value,index,pred=min(candidates,key=lambda x:x[0])
        chosen.append(index); current=pred
    selected=bank[:,chosen]; k=len(chosen)
    if k==1: return chosen,np.ones(1),selected[:,0]
    lo=e["minimum_weight"]; hi=e["maximum_weight"]
    if k*lo>1 or k*hi<1: raise ValueError("融合权重上下限与候选数不相容。")
    reference=np.full(k,1./k); scaled=selected/y[:,None]; n=len(y)
    equality=np.r_[np.ones(k),np.zeros(n)]
    lower=np.column_stack([-scaled,np.eye(n)]); upper=np.column_stack([scaled,np.eye(n)])
    def objective(z):
        w=z[:k]; residual=selected.dot(w)-y
        return float(alpha*np.mean(z[k:])+rweight*residual.dot(residual)/variance+e["regularization"]*np.sum((w-reference)**2))
    def gradient(z): return np.r_[2*rweight*selected.T.dot(selected.dot(z[:k])-y)/variance+2*e["regularization"]*(z[:k]-reference),np.full(n,alpha/n)]
    constraints=[{"type":"eq","fun":lambda z:z[:k].sum()-1,"jac":lambda z:equality},
                 {"type":"ineq","fun":lambda z:z[k:]-(scaled.dot(z[:k])-1),"jac":lambda z:lower},
                 {"type":"ineq","fun":lambda z:z[k:]+(scaled.dot(z[:k])-1),"jac":lambda z:upper}]
    start=np.r_[reference,np.abs(scaled.dot(reference)-1)+1e-8]
    alternatives=[reference]
    bounds=[(lo,hi)]*k+[(0,None)]*n
    q=config["quality"]
    target_constraints=constraints+[
        {"type":"ineq","fun":lambda z:q["mape_percent_max"]/100-np.mean(z[k:]),"jac":lambda z:np.r_[np.zeros(k),np.full(n,-1./n)]},
        {"type":"ineq","fun":lambda z:1-q["r2_min"]-np.sum((selected.dot(z[:k])-y)**2)/variance,
         "jac":lambda z:np.r_[-2*selected.T.dot(selected.dot(z[:k])-y)/variance,np.zeros(n)]}]
    for constraints_used in (constraints,target_constraints):
        fit=minimize(objective,start,jac=gradient,method="SLSQP",constraints=constraints_used,bounds=bounds,options={"maxiter":300,"ftol":1e-9})
        if fit.success and np.all(np.isfinite(fit.x)) and abs(fit.x[:k].sum()-1)<1e-6 and np.min(fit.x[k:]-np.abs(scaled.dot(fit.x[:k])-1))>=-1e-6:
            alternatives.append(fit.x[:k]); start=fit.x
    def feasible(w):
        p=selected.dot(w)
        return np.mean(np.abs(p-y)/y)*100<=q["mape_percent_max"]+1e-7 and sst>1e-12 and 1-np.sum((p-y)**2)/variance>=q["r2_min"]-1e-7
    good=[w for w in alternatives if feasible(w)]
    weights=min(good or alternatives,key=lambda w:business(selected.dot(w)))
    # Re-optimize the current ensemble before deciding to add another member.
    # Otherwise an unoptimized pair can make a harmful third model look useful.
    if _fixed is None:
        while len(chosen)<min(e["max_members"],len(distinct)):
            current=selected.dot(weights); value=business(current); was_good=feasible(weights); improvement=None
            for index in [i for i in distinct if i not in chosen][:6]:
                ids,w,pred=bounded_blend(y,bank,config,chosen+[index])
                is_good=np.mean(np.abs(pred-y)/y)*100<=q["mape_percent_max"] and sst>1e-12 and 1-np.sum((pred-y)**2)/variance>=q["r2_min"]
                cost=business(pred)
                if was_good and not is_good: continue
                if cost<value-1e-8 and (improvement is None or cost<improvement[0]): improvement=(cost,ids,w)
            if improvement is None: break
            unused,chosen,weights=improvement; selected=bank[:,chosen]
    return chosen,weights,selected.dot(weights)
