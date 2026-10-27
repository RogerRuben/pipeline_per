# -*- coding: utf-8 -*-
import itertools
import numpy as np
from .protocol import assessment,rank


def project(values,cap):
    if len(values)==1:return np.ones(1)
    lo=float(np.min(values)-1.);hi=float(np.max(values)+1.)
    for _ in range(70):
        middle=(lo+hi)/2.;w=np.clip(values-middle,0.,cap)
        if w.sum()>1.:lo=middle
        else:hi=middle
    w=np.clip(values-(lo+hi)/2.,0.,cap);return w/w.sum()


def weights_on_training_oof(P,y,cfg,sample_count=None):
    """No external evaluation labels accepted; caller passes this stage's pool only."""
    e=cfg['v6']['ensemble'];m=P.shape[1];prior=np.full(m,1./m)
    if m==1 or e['weight_policy']=='equal' or (len(y) if sample_count is None else sample_count)<e['fixed_weights_below']:return prior,'preset_equal_small_sample'
    A=np.asarray(P,float)/np.asarray(y,float)[:,None];alpha=e['shrinkage'];cap=e['max_weight']
    H=A.T.dot(A)/len(y)+alpha*np.eye(m);b=A.mean(axis=0)+alpha*prior
    def solve_face(status):
        fixed=np.flatnonzero(np.asarray(status)>=0);free=np.flatnonzero(np.asarray(status)<0)
        w=np.zeros(m)
        if len(fixed):w[fixed]=np.asarray(status)[fixed]*cap
        remaining=1.-w.sum()
        if not len(free):return w if abs(remaining)<1e-8 else None
        matrix=np.zeros((len(free)+1,len(free)+1));matrix[:-1,:-1]=H[np.ix_(free,free)];matrix[-1,:-1]=1.;matrix[:-1,-1]=1.
        rhs=np.r_[b[free]-H[np.ix_(free,fixed)].dot(w[fixed]),remaining]
        try:w[free]=np.linalg.solve(matrix,rhs)[:-1]
        except np.linalg.LinAlgError:return None
        return w if np.min(w)>=-1e-8 and np.max(w)<=cap+1e-8 else None
    w=solve_face([-1]*m)
    if w is None:
        feasible=[q for q in (solve_face(s) for s in itertools.product([-1,0,1],repeat=m)) if q is not None]
        if not feasible:return prior,'preset_equal_solver_fallback'
        w=min(feasible,key=lambda q:float(q.dot(H).dot(q)-2.*b.dot(q)))
    w=np.maximum(w,0.);w/=w.sum()
    return w,'shrunk_inner_oof_training'


def score_parts(parts,weights,y,cfg):
    actual_train=[];pred_train=[];actual_valid=[];pred_valid=[];folds=[]
    for i,f in enumerate(parts[0]):
        for other in parts[1:]:
            if not np.array_equal(f['train'],other[i]['train']) or not np.array_equal(f['validation'],other[i]['validation']):raise ValueError('候选练习折不一致。')
        a=sum(w*p[i]['train_prediction'] for w,p in zip(weights,parts));b=sum(w*p[i]['validation_prediction'] for w,p in zip(weights,parts))
        actual_train.extend(y[f['train']]);pred_train.extend(a);actual_valid.extend(y[f['validation']]);pred_valid.extend(b)
        from ..optuna_v4.metrics import metrics
        folds.append({'id':int(f['id']),'train_rows':len(f['train']),'validation_rows':len(f['validation']),
                      'train':metrics(y[f['train']],a),'validation':metrics(y[f['validation']],b)})
    score=assessment((actual_train,pred_train),(actual_valid,pred_valid),cfg)
    score['folds']=folds
    q=cfg['targets'];score['validation_pass_fraction']=float(np.mean([f['validation']['mape_percent']<=q['mape_percent_max'] and
        (f['validation']['r2'] is None or f['validation']['r2']>=q['r2_min']) for f in folds]))
    if cfg['v6']['mode']=='robust':score['qualified']=score['qualified'] and score['validation_pass_fraction']==1.
    return score


def choose_combinations(pool,y,cfg,pool_rows):
    e=cfg['v6']['ensemble'];ordered=sorted(pool,key=lambda p:rank(p['score'],cfg));selected=[];counts={}
    for p in ordered:
        f=p['spec']['family'];counts.setdefault(f,0)
        if counts[f]<e['pool_per_family']:selected.append(p);counts[f]+=1
    # Singles remain eligible; a poor model need not be included in any mixture.
    proposals=[{'members':[p],'weights':np.ones(1),'score':p['score'],'weight_source':'single_member'} for p in selected]
    maximum=min(e['max_members'],e['tiny_max_members'] if len(pool_rows)<8 else e['max_members']) if e['enabled'] else 1
    for size in range(2,maximum+1):
        options=[c for c in itertools.combinations(range(len(selected)),size) if len({selected[i]['spec']['family'] for i in c})==size]
        options.sort(key=lambda c:sum(c));allowance=max(1,e['max_combinations']//max(1,maximum-1))
        for indices in options[:allowance]:
            combo=[selected[i] for i in indices]
            P=np.column_stack([np.concatenate([f['validation_prediction'] for f in p['parts']]) for p in combo])
            target=np.concatenate([y[f['validation']] for f in combo[0]['parts']])
            w,source=weights_on_training_oof(P,target,cfg,len(pool_rows))
            score=score_parts([p['parts'] for p in combo],w,y,cfg)
            proposals.append({'members':list(combo),'weights':w,'score':score,'weight_source':source})
    return sorted(proposals,key=lambda c:rank(c['score'],cfg))
