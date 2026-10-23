# -*- coding: utf-8 -*-
"""Small constrained weight problems on fixed train/development predictions."""
import warnings
import numpy as np
from scipy.optimize import linprog,minimize
from ..optuna_v4.metrics import metrics,violation


def summary(ytr,yva,ptr,pva,cfg):
    a=metrics(ytr,ptr);b=metrics(yva,pva);g=cfg['v5']['train_gate'];q=cfg['targets']
    t=violation(a,g['mape_percent_max'],g['r2_min']);v=violation(b,q['mape_percent_max'],q['r2_min'])
    return {'train':a,'validation':b,'train_violation':t,'validation_violation':v,'qualified':v==0 and (g['mode']=='off' or t==0)}


def rank(s,cfg):
    g=cfg['v5']['train_gate'];t=s['train_violation'] if g['mode']!='off' else 0.;v=s['validation_violation']
    miss=t+v if g['mode']=='hard' else v+g['soft_penalty']*t if g['mode']=='soft' else v
    return (not s['qualified'],miss,s['validation']['mape_percent'],1.-(s['validation']['r2'] if s['validation']['r2'] is not None else -1e12))


def dominates(a,b,tol=1e-8):
    x=a['validation'];y=b['validation']
    return x['mape_percent']<=y['mape_percent']+tol and x['r2'] is not None and y['r2'] is not None and x['r2']>=y['r2']-tol


def solve(train,validation,ytr,yva,initial,cfg,strict=True):
    T=np.asarray(train,float);V=np.asarray(validation,float);ytr=np.asarray(ytr,float);yva=np.asarray(yva,float)
    if T.ndim!=2 or V.ndim!=2 or T.shape[1]!=V.shape[1] or T.shape[0]!=len(ytr) or V.shape[0]!=len(yva) or not np.isfinite(T).all() or not np.isfinite(V).all():raise ValueError('融合矩阵必须有限，且训练/验证成员严格对齐。')
    m=T.shape[1];w=np.asarray(initial,float).copy()
    if len(w)!=m or np.any(w<0) or not np.isfinite(w).all() or w.sum()<=0:raise ValueError('融合初值无效。')
    w/=w.sum();reference=summary(ytr,yva,T.dot(w),V.dot(w),cfg);best=w.copy();best_score=reference
    if m==1 or not cfg['v5']['fusion']['enabled']:return best,best_score,{'solves':0,'fallback':'original_weights'}
    P=np.vstack([T,V]);y=np.r_[ytr,yva];n=len(y);A=P/y[:,None];gate=cfg['v5']['train_gate'];q=cfg['targets'];maxiter=cfg['v5']['fusion']['maxiter']
    c=np.r_[np.zeros(m),np.full(len(ytr),1e-8/len(ytr)),np.full(len(yva),100./len(yva))]
    Aub=np.r_[np.c_[A,-np.eye(n)],np.c_[-A,-np.eye(n)]];bub=np.r_[np.ones(n),-np.ones(n)]
    limits=[]
    if gate['mode']=='hard':limits.append((np.arange(len(ytr)),gate['mape_percent_max']))
    val_limit=q['mape_percent_max']
    if strict and reference['qualified']:val_limit=min(val_limit,reference['validation']['mape_percent'])
    limits.append((np.arange(len(ytr),n),val_limit))
    for indices,limit in limits:
        row=np.zeros(m+n);row[m+indices]=1./len(indices);Aub=np.vstack([Aub,row]);bub=np.r_[bub,limit/100.]
    eq=np.r_[np.ones(m),np.zeros(n)][None,:];bounds=[(0.,1.)]*m+[(0.,None)]*n
    candidates=[w.copy()];notes=[]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            lp=linprog(c,A_ub=Aub,b_ub=bub,A_eq=eq,b_eq=[1.],bounds=bounds,method='revised simplex',options={'maxiter':maxiter,'tol':1e-9})
        if lp.success and np.isfinite(lp.x).all():candidates.append(np.maximum(lp.x[:m],0.))
        else:notes.append('LP:'+str(lp.message))
    except (ValueError,RuntimeError,FloatingPointError,OverflowError) as exc:notes.append('LP:'+repr(exc))
    quadratic=[]
    for matrix,target,limit in [(T,ytr,gate['r2_min'] if gate['mode']=='hard' else None),(V,yva,q['r2_min'])]:
        if strict and reference['qualified'] and matrix is V:limit=max(limit,reference['validation']['r2'])
        if limit is None:continue
        scale=max(float(np.max(np.abs(target))),np.finfo(float).tiny);B=matrix/scale;z=target/scale;sst=float(np.sum((z-z.mean())**2))
        if sst==0:notes.append('R2 undefined; keep reference unless genuinely feasible');continue
        upper=(1.-limit)*sst
        quadratic.append({'type':'ineq','fun':lambda x,B=B,z=z,u=upper:float(u-np.sum((B.dot(x[:m])-z)**2)),
                          'jac':lambda x,B=B,z=z:np.r_[-2.*B.T.dot(B.dot(x[:m])-z),np.zeros(n)]})
    constraints=[{'type':'eq','fun':lambda x:float(x[:m].sum()-1.),'jac':lambda x:np.r_[np.ones(m),np.zeros(n)]},
                 {'type':'ineq','fun':lambda x:bub-Aub.dot(x),'jac':lambda x:-Aub}]+quadratic
    starts=list(candidates)
    for seed in starts[:2]:
        seed=seed/seed.sum();x0=np.r_[seed,np.abs(1.-A.dot(seed))+1e-10]
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore');res=minimize(lambda x:float(c.dot(x)),x0,jac=lambda x:c,method='SLSQP',bounds=bounds,constraints=constraints,options={'maxiter':maxiter,'ftol':1e-10})
            if res.success and np.isfinite(res.x).all():candidates.append(np.maximum(res.x[:m],0.))
            else:notes.append('SLSQP:'+str(res.message))
        except (ValueError,RuntimeError,FloatingPointError,OverflowError) as exc:notes.append('SLSQP:'+repr(exc))
    candidates.extend(np.eye(m))
    for candidate in candidates:
        if not np.isfinite(candidate).all() or candidate.sum()<=0:continue
        candidate=candidate/candidate.sum()
        try:current=summary(ytr,yva,T.dot(candidate),V.dot(candidate),cfg)
        except (ValueError,FloatingPointError,OverflowError):continue
        if strict and reference['qualified'] and (not current['qualified'] or not dominates(current,reference)):continue
        if rank(current,cfg)<rank(best_score,cfg):best=candidate.copy();best_score=current
    best[best<1e-12]=0.;best/=best.sum();best_score=summary(ytr,yva,T.dot(best),V.dot(best),cfg)
    if strict and reference['qualified'] and (not best_score['qualified'] or not dominates(best_score,reference)):
        return w,reference,{'solves':len(starts[:2])+1,'fallback':'baseline_after_numeric_recheck','notes':notes}
    return best,best_score,{'solves':len(starts[:2])+1,'fallback':None,'notes':notes}
