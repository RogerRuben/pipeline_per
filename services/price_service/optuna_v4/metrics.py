# -*- coding: utf-8 -*-
import math
import numpy as np


def metrics(actual,predicted):
    y=np.asarray(actual,float).reshape(-1);p=np.asarray(predicted,float).reshape(-1)
    if len(y)!=len(p) or not len(y) or not np.isfinite(y).all() or not np.isfinite(p).all() or np.any(y<=0):raise ValueError('指标输入须对齐、有限且价格为正。')
    with np.errstate(over='ignore',invalid='ignore',divide='ignore'):
        error=np.abs(y-p);ape=error/y;scale=max(float(error.max()),np.finfo(float).tiny)
        mae=scale*float(np.mean(error/scale));rmse=scale*float(np.sqrt(np.mean((error/scale)**2)))
        mape=float(np.mean(ape)*100.)
        if len(y)<2 or np.ptp(y)==0:r2=None
        else:
            target_scale=max(float(np.max(np.abs(y))),np.finfo(float).tiny)
            yn=y/target_scale;denominator=float(np.sum((yn-yn.mean())**2))
            if not denominator:raise ValueError('价格方差过小，R方数值不可评估。')
            if not np.any(error):r2=1.
            else:
                log_ratio=2*(math.log(scale)-math.log(target_scale))+math.log(float(np.sum((error/scale)**2)))-math.log(denominator)
                if log_ratio>math.log(np.finfo(float).max):raise ValueError('候选预测外推过大，R方超出有限范围。')
                r2=float(1.-math.exp(log_ratio))
    if not all(np.isfinite(x) for x in (mape,mae,rmse,float(ape.max()*100.),float(np.quantile(ape,.9)*100.))) or (r2 is not None and not np.isfinite(r2)):raise ValueError('候选数值不稳定，无法取得有限指标。')
    return {'mape_percent':mape,'r2':r2,'mae_wan':mae,'rmse_wan':rmse,'rows':len(y),'r2_defined':r2 is not None,
            'max_ape_percent':float(ape.max()*100.),'p90_ape_percent':float(np.quantile(ape,.9)*100.)}


def violation(score,mape_limit,r2_limit):
    a=max(0.,math.log(max(score['mape_percent'],1e-300))-math.log(mape_limit))
    if r2_limit is None:b=0.
    elif score['r2'] is None:b=1.
    else:b=math.log1p(max(0.,r2_limit-score['r2'])/max(1.,abs(r2_limit)))
    return float(a+b)


def summarize(folds,y,cfg):
    tr_y=np.concatenate([y[f['train']] for f in folds]);tr_p=np.concatenate([f['train_prediction'] for f in folds])
    va_y=np.concatenate([y[f['validation']] for f in folds]);va_p=np.concatenate([f['validation_prediction'] for f in folds])
    train=metrics(tr_y,tr_p);validation=metrics(va_y,va_p);g=cfg['v4']['train_gate']
    per_train=[metrics(y[f['train']],f['train_prediction']) for f in folds]
    per_validation=[metrics(y[f['validation']],f['validation_prediction']) for f in folds]
    misses=[violation(m,g['mape_percent_max'],g['r2_min']) for m in per_train]
    pass_fraction=float(np.mean([v==0 for v in misses]))
    if g['scope']=='pooled':train_violation=violation(train,g['mape_percent_max'],g['r2_min'])
    elif g['scope']=='every_split':train_violation=max(misses)
    else:train_violation=max(0.,g['min_pass_fraction']-pass_fraction)
    q=cfg['targets'];validation_violation=violation(validation,q['mape_percent_max'],q['r2_min'])
    return {'train':train,'validation':validation,'train_violation':train_violation,'validation_violation':validation_violation,
        'train_pass_fraction':pass_fraction,'validation_pass_fraction':float(np.mean([violation(m,q['mape_percent_max'],q['r2_min'])==0 for m in per_validation])),
        'validation_fold_mape_p90':float(np.quantile([m['mape_percent'] for m in per_validation],.9)),
        'undefined_train_r2_folds':sum(m['r2'] is None for m in per_train),'undefined_validation_r2_folds':sum(m['r2'] is None for m in per_validation),
        'fold_metrics':[{'split_id':f['id'],'train':a,'validation':b} for f,a,b in zip(folds,per_train,per_validation)],
        'aggregation':'pooled_individual_heldout_predictions_with_repeated_rows; no mean-of-fold-R2; development scoring'}


def ranking(summary,cfg):
    gate=cfg['v4']['train_gate'];t=max(summary['train_violation'],summary.get('deployed_train_violation',0.)) if gate['mode']!='off' else 0.
    v=summary['validation_violation'];r=summary['validation']['r2']
    if gate['mode']=='hard':prefix=(t>0,t,t+v>0,t+v)
    elif gate['mode']=='soft':prefix=(t+v>0,v+gate['soft_penalty']*t)
    else:prefix=(v>0,v)
    return prefix+(summary['validation_fold_mape_p90'],summary['validation']['mape_percent'],float('inf') if r is None else -r)
