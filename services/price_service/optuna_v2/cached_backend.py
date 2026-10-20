# -*- coding: utf-8 -*-
"""Bounded prediction cache, keyed by ordered split, training data and model settings."""
import hashlib
import json
from collections import OrderedDict
import numpy as np
import pandas as pd
from sklearn.preprocessing import MinMaxScaler
from . import price_automl_core as core
from .typed_features import TypedBackend


class CachedBackend(TypedBackend):
    def __init__(self,category_fields=(),category_labels=None,cache_entries=256):
        super().__init__(category_fields,category_labels)
        self.limit=int(cache_entries);self.searching=False;self.cache=OrderedDict();self.splits=OrderedDict()
        self.stats={'model_fits':0,'prediction_cache_hits':0,'prediction_cache_misses':0,'cached_entries_peak':0,'split_cache_hits':0,'cache_fallbacks':0}
        self.fast_path_disabled=False;self.last_diagnostic={}
    def evaluate(self,cfg,X,y,rows,expert_map,test_size,price_bins):
        self.last_diagnostic={}
        if not self.searching or not self.limit or self.fast_path_disabled:
            return self._direct_evaluate(cfg,X,y,rows,expert_map,test_size,price_bins)
        try:return self._cached_evaluate(cfg,X,y,rows,expert_map,test_size,price_bins)
        except KeyError as exc:
            # A cache/column lookup error must not take a valid-data run down.
            # Refit this exact candidate through the original V2 backend once;
            # disable the fast path for the rest of this run if it succeeds.
            self.stats['cache_fallbacks']+=1;self.fast_path_disabled=True;self.cache.clear();self.splits.clear()
            self.last_diagnostic={'kind':'cache_key_error','error':repr(exc),'recovery':'same_candidate_original_v2_backend'}
            return self._direct_evaluate(cfg,X,y,rows,expert_map,test_size,price_bins)
    def _direct_evaluate(self,cfg,X,y,rows,expert_map,test_size,price_bins):
        if self.searching:self.stats['model_fits']+=int(np.count_nonzero(core.normalized_weights(cfg)))
        result=super().evaluate(cfg,X,y,rows,expert_map,test_size,price_bins)
        scores,_,detail=result
        self._check_metrics(scores,detail[3],detail[6],detail[7],detail[5],cfg)
        return result
    def _check_metrics(self,scores,actual,prediction,components,validation_rows,cfg):
        bad=[name for name in ('r2','mape','mae','rmse') if not np.isfinite(scores[name])]
        if not bad:return
        weights=core.normalized_weights(cfg)
        magnitudes={name:float(np.max(np.abs(components[name].values)))*float(weights[j]) for j,name in enumerate(core.MODEL_NAMES)}
        self.last_diagnostic.update(kind='nonfinite_metric',metrics=bad,validation_rows=[int(x) for x in validation_rows],
            validation_price_min=float(np.min(actual)),validation_price_max=float(np.max(actual)),
            prediction_max=float(np.max(np.abs(prediction))),dominant_models=sorted(magnitudes,key=magnitudes.get,reverse=True)[:3],
            recovery='reject_candidate_not_input_table')
        raise ValueError('候选模型数值不稳定：%s不是有限值；验证%d条，预测最大幅值%s。跳过该候选，不判定为表格错误。'%(','.join(bad),len(actual),str(self.last_diagnostic['prediction_max'])))
    def _cached_evaluate(self,cfg,X,y,rows,expert_map,test_size,price_bins):
        digest=hashlib.sha256(np.ascontiguousarray(X.values,dtype=float).tobytes()+np.ascontiguousarray(y,dtype=float).tobytes()+np.ascontiguousarray(rows).tobytes()).hexdigest()
        identity=json.dumps([digest,list(X.columns),self.category_fields,expert_map,test_size,price_bins],sort_keys=True)
        split_key=(identity,int(cfg['split_seed']))
        if split_key in self.splits:
            self.stats['split_cache_hits']+=1;prepared=self.splits.pop(split_key);self.splits[split_key]=prepared
        else:
            Xtr,ytr,Xte,yte,tr_rows,te_rows=core.split_data(X,y,rows,cfg['split_seed'],test_size,price_bins)
            encoder=self.encode(Xtr);train=encoder.frame(Xtr);test=encoder.frame(Xte)
            scaler=MinMaxScaler().fit(train)
            train=pd.DataFrame(scaler.transform(train),columns=train.columns)
            test=pd.DataFrame(scaler.transform(test),columns=test.columns)
            expanded={name:expert_map.get(owner,1.) for name,owner in encoder.owners.items()}
            prepared=(Xtr,ytr,Xte,yte,tr_rows,te_rows,train,test,expanded)
            self.splits[split_key]=prepared
            while len(self.splits)>64:self.splits.popitem(last=False)
        Xtr,ytr,Xte,yte,tr_rows,te_rows,train,test,expanded=prepared
        # Train/test ORDER is material to subsampling and deterministic tie-breaking.
        split_identity=json.dumps([identity,tr_rows.tolist(),te_rows.tolist()],sort_keys=True)
        weights=core.normalized_weights(cfg);components=pd.DataFrame(index=np.arange(len(yte)))
        for j,name in enumerate(core.MODEL_NAMES):
            prediction=np.zeros(len(yte))
            if weights[j]>0:
                model=core.instantiate_model(name,cfg,expanded)
                key=hashlib.sha256((split_identity+name+json.dumps(model.get_params(deep=False),sort_keys=True)).encode('utf-8')).hexdigest()
                if key in self.cache:
                    self.stats['prediction_cache_hits']+=1;prediction=self.cache.pop(key);self.cache[key]=prediction
                else:
                    self.stats['prediction_cache_misses']+=1;self.stats['model_fits']+=1
                    model.fit(core._weighted_array(train,name,expanded),np.log(ytr))
                    prediction=np.exp(np.clip(np.asarray(model.predict(core._weighted_array(test,name,expanded)),float),-700,700))
                    if not np.isfinite(prediction).all():raise ValueError('模型预测包含非有限值。')
                    if self.limit:
                        self.cache[key]=prediction
                        while len(self.cache)>self.limit:self.cache.popitem(last=False)
                        self.stats['cached_entries_peak']=max(self.stats['cached_entries_peak'],len(self.cache))
            # Inactive models are neither fitted nor cached as zero predictions.
            components[name]=prediction
        pred=components.to_numpy().dot(weights)
        scores=core.regression_metrics(yte,pred)
        self._check_metrics(scores,yte,pred,components,te_rows,cfg)
        return scores,None,(Xtr,ytr,Xte,yte,tr_rows,te_rows,pred,components)
