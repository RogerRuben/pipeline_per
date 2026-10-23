# -*- coding: utf-8 -*-
"""Shared two-type feature contract; keep the supplied numeric V2 core untouched."""
import copy
import numpy as np
import pandas as pd
from . import price_automl_core as core

TYPE_ALIASES = {'number':'number','numeric':'number','数值':'number',
                'enum':'enum','categorical':'enum','configuration':'enum','配置类型':'enum','配置':'enum','枚举':'enum',
                'boolean':'enum','布尔':'enum','text':'enum','文本':'enum','ip_grade':'number'}


def canonical_type(value):
    token=str(value).strip().lower()
    if token not in TYPE_ALIASES:
        raise ValueError('属性类型只能是数值(number)或配置类型(enum)：'+str(value))
    return TYPE_ALIASES[token]


def apply_types(data, declared):
    from tools.product_r4_input import apply_field_types
    known={p['parameter_id'] for p in data['parameters']}
    if set(declared)-known:raise ValueError('属性类型引用未知英文代号：'+','.join(sorted(set(declared)-known)))
    types={p['parameter_id']:canonical_type(declared[p['parameter_id']]) if p['parameter_id'] in declared
           else ('enum' if p['value_type'] in ('enum','text') else 'number') for p in data['parameters']}
    return apply_field_types(data,types)


def restore_configurations(data,table,declared,missing_tokens=()):
    """Restore declared category tokens before automatic numeric/IP inference can erase them."""
    categorical={key for key,value in declared.items() if canonical_type(value)=='enum'}
    if not categorical:return data
    from app.historical_onboarding import _is_missing,parse_missing_tokens
    if len(table['rows'])!=len(data['agreements']):raise ValueError('原始表记录与成品记录数不同，无法安全恢复配置取值。')
    positions={key:j for j,key in enumerate(table['field_ids'])};tokens=parse_missing_tokens(missing_tokens)
    for record,source in zip(data['agreements'],table['rows']):
        for key in categorical:
            if key not in positions:continue
            j=positions[key];value=source[j] if j<len(source) else None
            if _is_missing(value,tokens):record['params'].pop(key,None)
            else:record['params'][key]=format(value,'.15g') if isinstance(value,(int,float)) and not isinstance(value,bool) else str(value).strip()
    return data


class FeatureEncoder(object):
    """Fit category vocabulary on training rows only; numeric magnitudes stay numeric."""
    def __init__(self,category_fields=(),category_labels=None):
        self.category_fields=set(category_fields);self.category_labels=copy.deepcopy(category_labels or {})
    def fit(self,frame):
        self.input_names=list(frame.columns);self.categories={};self.feature_names=[];self.owners={};self.labels={}
        for position,key in enumerate(self.input_names):
            if key not in self.category_fields:
                self.feature_names.append(key);self.owners[key]=key;continue
            values=np.unique(frame[key].values.astype(float));self.categories[key]=values
            for j,value in enumerate(values):
                name='__price_category_%d_%d'%(position,j)
                while name in self.input_names:name='_'+name
                self.feature_names.append(name);self.owners[name]=key
                self.labels[name]=self.category_labels.get(key,{}).get(float(value),str(value))
        return self
    def transform(self,values):
        if isinstance(values,pd.DataFrame):array=values.loc[:,self.input_names].to_numpy(dtype=float)
        else:array=np.asarray(values,dtype=float)
        if array.ndim!=2 or array.shape[1]!=len(self.input_names) or not np.isfinite(array).all():
            raise ValueError('特征编码收到缺失、非有限值或列数不匹配的输入。')
        result=[]
        for j,key in enumerate(self.input_names):
            if key in self.categories:result.extend((array[:,j]==value).astype(float) for value in self.categories[key])
            else:result.append(array[:,j])
        return np.column_stack(result)
    def frame(self,values):
        return pd.DataFrame(self.transform(values),columns=self.feature_names,index=values.index if isinstance(values,pd.DataFrame) else None)


class EncodedMinMax(object):
    """Saved service preprocessor: raw category codes -> one-hot -> fitted MinMax."""
    def __init__(self,encoder,scaler):self.encoder,self.scaler=encoder,scaler
    def transform(self,values):return self.scaler.transform(self.encoder.transform(values))


class TypedBackend(object):
    def __init__(self,category_fields=(),category_labels=None):
        self.category_fields=tuple(category_fields);self.category_labels=category_labels or {}
        self.last_evaluation=None
    def encode(self,frame):return FeatureEncoder(self.category_fields,self.category_labels).fit(frame)
    def fit(self,frame,y,cfg,expert_map,active_weights=None):
        if not self.category_fields:return core.fit_ensemble(frame,y,cfg,expert_map,active_weights)
        encoder=self.encode(frame);encoded=encoder.frame(frame)
        expanded={name:expert_map.get(owner,1.) for name,owner in encoder.owners.items()}
        bundle=core.fit_ensemble(encoded,y,cfg,expanded,active_weights)
        bundle.update(input_encoder=encoder,input_feature_names=list(frame.columns))
        return bundle
    @staticmethod
    def predict(bundle,frame):
        encoder=bundle.get('input_encoder')
        return core.predict_bundle(bundle,encoder.frame(frame) if encoder else frame)
    def evaluate(self,cfg,X,y,rows,expert_map,test_size,price_bins):
        self.last_evaluation=None
        Xtr,ytr,Xte,yte,tr_rows,te_rows=core.split_data(X,y,rows,cfg['split_seed'],test_size,price_bins)
        bundle=self.fit(Xtr,ytr,cfg,expert_map);pred,components=self.predict(bundle,Xte)
        detail=(Xtr,ytr,Xte,yte,tr_rows,te_rows,pred,components)
        train_prediction,train_components=self.predict(bundle,Xtr)
        from .training_observation import capture
        capture(self,cfg,detail,train_prediction,train_components,bundle)
        return core.regression_metrics(yte,pred),bundle,detail


def native_preprocessor(bundle):
    encoder=bundle.get('input_encoder')
    return EncodedMinMax(encoder,bundle['scaler']) if encoder else bundle['scaler']
