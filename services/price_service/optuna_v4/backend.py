# -*- coding: utf-8 -*-
"""Canonical split evaluation with per-member train AND validation prediction caches."""
import hashlib
import json
from collections import OrderedDict
import numpy as np
import pandas as pd
from sklearn.preprocessing import MinMaxScaler
from ..optuna_v2 import price_automl_core as core
from ..optuna_v2.typed_features import TypedBackend
from .metrics import summarize
from contextlib import nullcontext


def effective_model(name,cfg,expert_map,y):
    params=dict(cfg)
    if name=='expert_tree' and 'tree_min_gain_fraction' in params:
        logs=np.log(np.asarray(y,float));params['tree_min_gain_split']=float(params['tree_min_gain_fraction']*np.sum((logs-logs.mean())**2))
    model=core.instantiate_model(name,params,expert_map)
    if name in ('extratrees','gbdt') and 'random_state' in model.get_params(deep=False):model.set_params(random_state=int(cfg.get('model_seed',0)))
    return model


class Backend(TypedBackend):
    def __init__(self,frame,y,rows,expert_map,category_fields=(),category_labels=None,cache_entries=512):
        super().__init__(category_fields,category_labels)
        self.frame,self.y,self.rows,self.expert_map=frame,np.asarray(y,float),np.asarray(rows,int),dict(expert_map)
        self.limit=cache_entries;self.cache=OrderedDict();self.prepared=OrderedDict()
        self.identity=hashlib.sha256(frame.values.tobytes()+self.y.tobytes()+self.rows.tobytes()+json.dumps([list(frame.columns),expert_map],sort_keys=True).encode('utf-8')).hexdigest()
        self.stats={'model_fits':0,'prediction_cache_hits':0,'prediction_cache_misses':0,'peak_cache_entries':0,'fold_evaluations':0}
        self.trace=None
    def prepare(self,fold):
        key=(tuple(fold['train']),tuple(fold['validation']))
        if key in self.prepared:result=self.prepared.pop(key);self.prepared[key]=result;return result
        raw_train=self.frame.iloc[fold['train']];raw_validation=self.frame.iloc[fold['validation']]
        encoder=self.encode(raw_train);train=encoder.frame(raw_train);validation=encoder.frame(raw_validation)
        scaler=MinMaxScaler().fit(train)
        train=pd.DataFrame(scaler.transform(train),columns=train.columns)
        validation=pd.DataFrame(scaler.transform(validation),columns=validation.columns)
        experts={name:self.expert_map.get(owner,1.) for name,owner in encoder.owners.items()}
        result=(train,validation,experts);self.prepared[key]=result
        while len(self.prepared)>64:self.prepared.popitem(last=False)
        return result
    def evaluate(self,cfg,folds,settings):
        weights=core.normalized_weights(cfg);evaluations=[]
        for fold in folds:
            self.stats['fold_evaluations']+=1;train,validation,experts=self.prepare(fold)
            key_base=json.dumps([self.identity,self.rows[fold['train']].tolist(),self.rows[fold['validation']].tolist()],sort_keys=True)
            train_components=np.zeros((len(train),len(core.MODEL_NAMES)));validation_components=np.zeros((len(validation),len(core.MODEL_NAMES)))
            for j,name in enumerate(core.MODEL_NAMES):
                if weights[j]<=0:continue
                model=effective_model(name,cfg,experts,self.y[fold['train']])
                key=hashlib.sha256((key_base+name+json.dumps(model.get_params(deep=False),sort_keys=True)).encode('utf-8')).hexdigest()
                if key in self.cache:
                    self.stats['prediction_cache_hits']+=1;prediction=self.cache.pop(key);self.cache[key]=prediction
                    if self.trace is not None:self.trace.emit('cv_prediction_cache_hit',family=name,fold=int(fold['id']))
                else:
                    self.stats['prediction_cache_misses']+=1;self.stats['model_fits']+=1
                    context=self.trace.span('cv_member_fit',family=name,fold=int(fold['id']),parameters=model.get_params(deep=False),train_rows=self.rows[fold['train']].tolist(),validation_rows=self.rows[fold['validation']].tolist()) if self.trace is not None else nullcontext()
                    with context:model.fit(core._weighted_array(train,name,experts),np.log(self.y[fold['train']]))
                    a=np.asarray(model.predict(core._weighted_array(train,name,experts)),float)
                    b=np.asarray(model.predict(core._weighted_array(validation,name,experts)),float)
                    prediction=np.exp(np.clip(np.concatenate([a,b]),-700,700))
                    if not np.isfinite(prediction).all():raise ValueError('V4成员%s产生非有限预测。'%name)
                    if self.limit:
                        self.cache[key]=prediction
                        while len(self.cache)>self.limit:self.cache.popitem(last=False)
                        self.stats['peak_cache_entries']=max(self.stats['peak_cache_entries'],len(self.cache))
                train_components[:,j]=prediction[:len(train)];validation_components[:,j]=prediction[len(train):]
            result=dict(fold,train_prediction=train_components.dot(weights),validation_prediction=validation_components.dot(weights))
            if self.trace is not None:
                from .metrics import metrics
                self.trace.emit('cv_fold_scores',fold=int(fold['id']),parameters=cfg,train=metrics(self.y[fold['train']],result['train_prediction']),validation=metrics(self.y[fold['validation']],result['validation_prediction']))
            evaluations.append(result)
        return {'parameters':dict(cfg),'folds':evaluations,'summary':summarize(evaluations,self.y,settings)}
    def fit(self,frame,y,cfg,expert_map,active_weights=None):
        weights=core.normalized_weights(cfg) if active_weights is None else np.asarray(active_weights,float)
        encoder=self.encode(frame);encoded=encoder.frame(frame);experts={name:expert_map.get(owner,1.) for name,owner in encoder.owners.items()}
        scaler=MinMaxScaler().fit(encoded);scaled=pd.DataFrame(scaler.transform(encoded),columns=encoded.columns,index=encoded.index);models={}
        for j,name in enumerate(core.MODEL_NAMES):
            if weights[j]<=0:continue
            model=effective_model(name,cfg,experts,y);model.fit(core._weighted_array(scaled,name,experts),np.log(np.asarray(y,float)));models[name]=model
        bundle={'scaler':scaler,'models':models,'weights':weights,'feature_names':list(encoded.columns),'expert_map':experts,'params':dict(cfg),'log_target':True,'model_names':core.MODEL_NAMES}
        if self.category_fields:bundle.update(input_encoder=encoder,input_feature_names=list(frame.columns))
        return bundle


def mix_evaluations(evaluations,weights,y,cfg):
    weights=np.asarray(weights,float);weights=weights/weights.sum();folds=[]
    for i,reference in enumerate(evaluations[0]['folds']):
        for evaluated in evaluations[1:]:
            other=evaluated['folds'][i]
            if not np.array_equal(reference['train'],other['train']) or not np.array_equal(reference['validation'],other['validation']):raise ValueError('融合候选的划分不一致，禁止拼接不同验证集。')
        folds.append(dict(reference,train_prediction=sum(w*e['folds'][i]['train_prediction'] for w,e in zip(weights,evaluations)),
                          validation_prediction=sum(w*e['folds'][i]['validation_prediction'] for w,e in zip(weights,evaluations))))
    result={'folds':folds,'summary':summarize(folds,y,cfg)}
    if all('deployment_train_prediction' in e for e in evaluations):
        from .metrics import metrics,violation
        prediction=sum(w*e['deployment_train_prediction'] for w,e in zip(weights,evaluations))
        score=metrics(y,prediction);g=cfg['v4']['train_gate']
        result.update(deployment_train_prediction=prediction)
        result['summary'].update(deployed_train=score,deployed_train_violation=violation(score,g['mape_percent_max'],g['r2_min']))
    return result
