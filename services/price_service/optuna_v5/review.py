# -*- coding: utf-8 -*-
"""Optional cross-partition audit; the default mode never reranks by audit scores."""
import copy
import hashlib
import json
import numpy as np
from ..optuna_v4.splits import make_splits
from ..optuna_v4.backend import Backend
from .fusion import summary,rank,solve
from .engine import matrices,bank_summary


def review(result,frame,y,rows,expert,mappings,labels,cfg,progress):
    v=cfg['v5'];selected=copy.deepcopy(result['selected']);state=result['state']
    if v['mode']=='goal_first':
        active=np.flatnonzero(selected['weights']>0)
        selected['entries']=[selected['entries'][i] for i in active];selected['weights']=selected['weights'][active]
    if not v['audit']['enabled'] and v['mode']=='goal_first':return selected,{'status':'disabled','used_for_selection':False}
    if cfg['search']['timeout_seconds'] is not None and result['elapsed_seconds']>=cfg['search']['timeout_seconds']:
        return selected,{'status':'unavailable','reason':'时间预算已用尽，保留基线/优胜模型；未追加复核','used_for_selection':False}
    if len(selected['entries'])>v['audit']['max_members']:return selected,{'status':'unavailable','reason':'成员数超过复核预算，未截断权重冒充完整复核','used_for_selection':False}
    from automl_pipeline.product_price_optuna_v4 import load_config
    v4=load_config(overrides={'validation':cfg['validation'],'targets':cfg['targets'],
        'v4':{'splits':{'max_splits':v['audit']['max_splits'],'validation_rows':v['audit']['validation_rows'],'seed':v['audit']['seed']},
              'train_gate':v['train_gate']}})
    groups=np.unique(frame.values,axis=0,return_inverse=True)[1]
    try:folds,plan=make_splits(y,groups,v4)
    except ValueError as exc:return selected,{'status':'unavailable','reason':str(exc),'used_for_selection':False}
    key=hashlib.sha256(json.dumps({'parameters':[e['parameters'] for e in selected['entries']],
        'folds':[(f['train'].tolist(),f['validation'].tolist()) for f in folds]},sort_keys=True).encode('utf-8')).hexdigest()
    cached=state.get('audit_cache')
    if cached and cached['key']==key:
        parts=cached['parts'];progress('V5复用同参数同划分的复核预测，不新增fit。')
    else:
        estimated=len(selected['entries'])*len(folds);limit=v['budget']['max_member_fits']
        if limit is not None and result['used_fits']+estimated+v['enhancement']['pool_size']>=limit:
            return selected,{'status':'unavailable','reason':'总fit预算已保留导出额度，未完整复核','used_for_selection':False}
        backend=Backend(frame,y,rows,expert,tuple(mappings),labels,512);parts=[]
        try:
            for j,entry in enumerate(selected['entries']):
                parts.append(backend.evaluate(entry['parameters'],folds,v4))
                progress('V5开发集复核%d/%d个成员，%d个固定划分；当前fit=%d。'%(j+1,len(selected['entries']),len(folds),backend.stats['model_fits']))
        except (ValueError,FloatingPointError,OverflowError,RuntimeError) as exc:
            state['audit_fits']+=backend.stats['model_fits']
            return selected,{'status':'failed','reason':repr(exc),'used_for_selection':False}
        state['audit_fits']+=backend.stats['model_fits'];state['audit_cache']={'key':key,'parts':parts}
    ya=np.concatenate([y[f['train']] for f in folds]);yb=np.concatenate([y[f['validation']] for f in folds])
    A=np.column_stack([np.concatenate([f['train_prediction'] for f in p['folds']]) for p in parts])
    B=np.column_stack([np.concatenate([f['validation_prediction'] for f in p['folds']]) for p in parts])
    old_weights=selected['weights'].copy();old=summary(ya,yb,A.dot(old_weights),B.dot(old_weights),cfg)
    current=old;used=False
    if v['mode'] in ('balanced','robust'):
        weights,candidate,solver=solve(A,B,ya,yb,old_weights,cfg,False);state['solver_calls']+=solver['solves']
        T,V=matrices(selected);primary=summary(y[selected['train']],y[selected['validation']],T.dot(weights),V.dot(weights),cfg)
        old_primary=selected['summary']
        def balanced(a,b):return (not a['qualified'],a['validation_violation']+v['audit']['balanced_weight']*b['validation_violation'],
                                 a['validation']['mape_percent']+v['audit']['balanced_weight']*b['validation']['mape_percent'])
        if (v['mode']=='robust' and rank(candidate,cfg)<rank(old,cfg)) or (v['mode']=='balanced' and balanced(primary,candidate)<balanced(old_primary,old)):
            selected['weights']=weights;selected['summary']=primary;current=candidate;used=True
    return selected,{'status':'completed','used_for_selection':v['mode']!='goal_first','weights_changed_by_audit':used,
        'scope':'searched_cross_partition_development' if v['mode']!='goal_first' else 'development_audit_of_selected_parameters_report_only',
        'summary':current,'before_audit_selection':old,'plan':plan,
        'aggregation':'pooled individual predictions, repeated samples retained; no average fold R2',
        'folds':[{'id':f['id'],'train_rows':rows[f['train']].tolist(),'validation_rows':rows[f['validation']].tolist()} for f in folds]}
