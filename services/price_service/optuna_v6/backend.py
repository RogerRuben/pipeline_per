# -*- coding: utf-8 -*-
import hashlib,json,time,warnings
from collections import OrderedDict
from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator,RegressorMixin
from sklearn.preprocessing import MinMaxScaler
from ..optuna_v2 import price_automl_core as core
from ..optuna_v2.typed_features import TypedBackend
from ..optuna_v4.backend import effective_model
from ..optuna_v5.trace import fit_scope
from ..optuna_v4.metrics import metrics


class BudgetStop(RuntimeError):pass


class FitLedger(object):
    def __init__(self,path,limit,trace=None,timeout=None,elapsed_before=0.):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.limit=limit;self.trace=trace;self.timeout=timeout;self.started=time.monotonic();self.elapsed_before=elapsed_before
        self.starts=[];self.completed=0;self.cache_hits=0
        if self.path.is_file():
            for line in self.path.read_text(encoding='utf-8').splitlines():
                if not line.strip():continue
                item=json.loads(line)
                self.elapsed_before=max(self.elapsed_before,float(item.get('elapsed_seconds',0.)))
                if item['event']=='fit_start':self.starts.append(item)
                elif item['event']=='fit_end':self.completed+=int(item['success'])
    def elapsed(self):return self.elapsed_before+time.monotonic()-self.started
    def used(self,stage=None):return len(self.starts) if stage is None else sum(e['stage']==stage for e in self.starts)
    def room(self,stage,cap,reserve=0):return min(self.limit-self.used(),cap-self.used(stage))>reserve
    def expired(self):return self.timeout is not None and self.elapsed()>=self.timeout
    def append(self,item):
        item=dict(item,elapsed_seconds=self.elapsed())
        with self.path.open('a',encoding='utf-8') as handle:handle.write(json.dumps(item,ensure_ascii=False,allow_nan=False)+'\n');handle.flush()
    def start(self,stage,cap,spec,rows):
        if not self.room(stage,cap):raise BudgetStop('实际成员fit预算已用尽，停止并保存断点。')
        item={'event':'fit_start','fit_id':len(self.starts)+1,'stage':stage,'spec':spec,'train_rows':list(map(int,rows))}
        self.starts.append(item);self.append(item)
        return item['fit_id']
    def end(self,fit_id,success,error=None):
        self.completed+=int(success);self.append({'event':'fit_end','fit_id':fit_id,'success':success,'error':error})


class SelectedModel(BaseEstimator,RegressorMixin):
    """Training-only filtering with full-width coefficients for native explanations."""
    def __init__(self,model,feature_k=0):self.model=model;self.feature_k=feature_k
    def fit(self,X,y):
        self.columns_=list(X.columns) if isinstance(X,pd.DataFrame) else list(range(np.asarray(X).shape[1]))
        a=np.asarray(X,float);self.support_=np.arange(a.shape[1])
        if self.feature_k and len(a)>=4 and self.feature_k<a.shape[1]:
            centered=a-a.mean(axis=0);target=np.asarray(y)-np.mean(y)
            denom=np.sqrt(np.sum(centered**2,axis=0)*max(float(np.sum(target**2)),1e-30))
            corr=np.divide(np.abs(centered.T.dot(target)),denom,out=np.zeros(a.shape[1]),where=denom>1e-15)
            self.support_=np.sort(np.argsort(-corr,kind='mergesort')[:self.feature_k])
        self.model.fit(self.subset(X),y);return self
    def subset(self,X):
        return X.iloc[:,self.support_] if isinstance(X,pd.DataFrame) else np.asarray(X)[:,self.support_]
    def predict(self,X):return self.model.predict(self.subset(X))
    @property
    def coef_(self):
        coef=np.asarray(self.model.coef_).reshape(-1);result=np.zeros(len(self.columns_));result[self.support_]=coef;return result
    @property
    def intercept_(self):return self.model.intercept_
    @property
    def feature_importances_(self):
        result=np.zeros(len(self.columns_));result[self.support_]=self.model.feature_importances_;return result
    @property
    def root_(self):return self.model.root_


class MedianModel(BaseEstimator,RegressorMixin):
    def fit(self,X,y):
        self.coef_=np.zeros(np.asarray(X).shape[1]);self.intercept_=float(np.log(np.median(np.exp(y))));return self
    def predict(self,X):return np.full(len(X),self.intercept_)


def native_family(spec):return 'ridge' if spec['family']=='median' else spec['family']


class Backend(TypedBackend):
    def __init__(self,frame,y,rows,expert,mappings,labels,ledger,limit=128,trace=None):
        super().__init__(tuple(mappings),labels);self.frame=frame;self.y=np.asarray(y,float);self.rows=np.asarray(rows,int)
        self.expert=expert;self.ledger=ledger;self.limit=limit;self.trace=trace;self.cache=OrderedDict()
    def fit_bundle(self,train,spec,stage,cap,allowed):
        train=np.asarray(train,int)
        if len(train)<2 or not set(train)<=set(allowed):raise ValueError('禁止在本轮允许训练记录以外拟合。')
        raw=self.frame.iloc[train];target=self.y[train]
        key=hashlib.sha256(raw.values.tobytes()+target.tobytes()+self.rows[train].tobytes()+json.dumps([list(raw.columns),spec,self.expert,self.category_fields],sort_keys=True).encode('utf-8')).hexdigest()
        if key in self.cache:
            self.ledger.cache_hits+=1;bundle=self.cache.pop(key);self.cache[key]=bundle
            if self.trace:self.trace.emit('v6_cache_hit',stage=stage,spec=spec,train_rows=self.rows[train])
            return bundle
        fit_id=self.ledger.start(stage,cap,spec,self.rows[train]);family=native_family(spec)
        try:
            with fit_scope(self.trace,'v6_member_fit',scope=stage,fit_id=fit_id,spec=spec,train_rows=self.rows[train]):
                encoder=self.encode(raw);encoded=encoder.frame(raw);scaler=MinMaxScaler().fit(encoded)
                prepared=pd.DataFrame(scaler.transform(encoded),columns=encoded.columns,index=encoded.index)
                experts={k:self.expert.get(owner,1.) for k,owner in encoder.owners.items()}
                base=MedianModel() if spec['family']=='median' else effective_model(family,spec['params'],experts,target)
                model=SelectedModel(base,spec.get('feature_k',0))
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter('always');model.fit(core._weighted_array(prepared,family,experts),np.log(target))
                if self.trace and caught:self.trace.emit('v6_fit_warnings',stage=stage,fit_id=fit_id,warnings=[str(w.message) for w in caught])
                if self.trace:self.trace.emit('v6_features_fitted',stage=stage,fit_id=fit_id,train_rows=self.rows[train],requested_k=spec.get('feature_k',0),
                    selected_fields=[str(encoded.columns[i]) for i in model.support_],selection_uses_test_labels=False)
                weights=np.asarray([float(n==family) for n in core.MODEL_NAMES])
                bundle={'scaler':scaler,'models':{family:model},'weights':weights,'feature_names':list(encoded.columns),
                        'expert_map':experts,'params':dict(spec['params'],**{'weight_'+n:float(n==family) for n in core.MODEL_NAMES}),
                        'log_target':True,'model_names':core.MODEL_NAMES,'v6_spec':spec}
                if self.category_fields:bundle.update(input_encoder=encoder,input_feature_names=list(raw.columns))
            self.ledger.end(fit_id,True)
        except BaseException as exc:self.ledger.end(fit_id,False,repr(exc));raise
        self.cache[key]=bundle
        while len(self.cache)>self.limit:self.cache.popitem(last=False)
        return bundle
    def evaluate(self,spec,folds,stage,cap,allowed):
        results=[]
        for f in folds:
            bundle=self.fit_bundle(f['train'],spec,stage,cap,allowed)
            a=TypedBackend.predict(bundle,self.frame.iloc[f['train']])[0];b=TypedBackend.predict(bundle,self.frame.iloc[f['validation']])[0]
            sa=metrics(self.y[f['train']],a);sb=metrics(self.y[f['validation']],b)
            if self.trace:self.trace.emit('v6_fold_scores',stage=stage,fold=f['id'],spec=spec,train_rows=self.rows[f['train']],validation_rows=self.rows[f['validation']],train=sa,validation=sb)
            results.append(dict(f,train_prediction=a,validation_prediction=b))
        return results
