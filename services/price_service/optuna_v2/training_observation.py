# -*- coding: utf-8 -*-
"""Training telemetry, independent of the byte-preserved V2 search objectives."""
import math
import time
import numpy as np


def score(actual,predicted):
    y=np.asarray(actual,float);p=np.asarray(predicted,float)
    if y.shape!=p.shape or not len(y) or not np.isfinite(y).all() or not np.isfinite(p).all() or np.any(y<=0):
        return {'mape_percent':None,'r2':None,'rows':len(y),'status':'nonfinite_or_unaligned'}
    with np.errstate(over='ignore',invalid='ignore',divide='ignore'):
        mape=float(np.mean(np.abs(p/y-1.))*100.)
        scale=max(float(np.max(np.abs(y))),float(np.max(np.abs(p))),np.finfo(float).tiny)
        a=y/scale;b=p/scale;den=float(np.sum((a-a.mean())**2))
        r2=None if len(y)<2 or np.ptp(y)==0 or den==0 else float(1.-np.sum((a-b)**2)/den)
    if not np.isfinite(mape) or (r2 is not None and not np.isfinite(r2)):
        return {'mape_percent':None,'r2':None,'rows':len(y),'status':'numerically_unavailable'}
    return {'mape_percent':mape,'r2':r2,'rows':len(y),'status':'ok' if r2 is not None else 'undefined_r2'}


def capture(backend,cfg,detail,train_prediction,train_components,bundle=None):
    Xtr,ytr,Xte,yte,tr_rows,te_rows,pred,components=detail
    backend.last_evaluation={'parameters':dict(cfg),'train_rows':np.asarray(tr_rows,int),'validation_rows':np.asarray(te_rows,int),
        'train_actual':np.asarray(ytr,float),'validation_actual':np.asarray(yte,float),
        'train_prediction':np.asarray(train_prediction,float),'validation_prediction':np.asarray(pred,float),
        'train_components':np.asarray(train_components,float),'validation_components':np.asarray(components,float),
        'train_metrics':score(ytr,train_prediction),'validation_metrics':score(yte,pred),'bundle':bundle}


def attach(trial,backend):
    evaluated=getattr(backend,'last_evaluation',None)
    if not trial.user_attrs.get('valid') or not evaluated:return
    m=evaluated['train_metrics']
    trial.set_user_attr('train_metrics',m)
    trial.set_user_attr('train_mape_percent',m['mape_percent']);trial.set_user_attr('train_r2',m['r2'])
    trial.set_user_attr('train_rows',m['rows']);trial.set_user_attr('validation_rows',len(evaluated['validation_rows']))


def fmt(value,digits=12):return '不可定义/未记录' if value is None else ('%.'+str(max(10,digits))+'g')%value


class ProgressBest(object):
    def __init__(self,r2_target,mape_target):self.r2=r2_target;self.mape=mape_target;self.best=None;self.key=None;self.started=time.monotonic()
    def update(self,trial):
        if trial.values is None or not trial.user_attrs.get('valid'):return
        r,m=map(float,trial.values)
        miss=max(0.,self.r2-r)/max(.1,abs(self.r2))+max(0.,m-self.mape)/max(self.mape,1e-8)
        key=(miss>0,miss,m,-r)
        if self.key is None or key<self.key:self.key=key;self.best=trial
    def describe(self,trial):
        self.update(trial);t=trial.user_attrs.get('train_metrics',{})
        current='训练MAPE %s%%，R² %s；开发MAPE %.10g%%，R² %.12g'%(fmt(t.get('mape_percent')),fmt(t.get('r2')),trial.values[1]*100.,trial.values[0])
        if self.best:
            b=self.best;bt=b.user_attrs.get('train_metrics',{})
            current+='；目标优胜#%d 训练%s%%/%s，开发%.10g%%/%.12g'%(b.number,fmt(bt.get('mape_percent')),fmt(bt.get('r2')),b.values[1]*100.,b.values[0])
        return current+'；累计%.1f秒。'%(time.monotonic()-self.started)
