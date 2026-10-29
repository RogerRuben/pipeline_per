# -*- coding: utf-8 -*-
import copy,hashlib,json,pickle,time
from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.preprocessing import MinMaxScaler
from ..optuna_v2 import price_automl_core as core
from ..optuna_v2.typed_features import FeatureEncoder,TypedBackend
from ..optuna_v5.persistence import save_state,load_state,atomic_json
from ..optuna_v4.native_member import NativeConfigurationMember

class NativeV7ConfigurationMember(NativeConfigurationMember):
    def explanation(self,values):
        result=super().explanation(values)
        for member in result['members']:
            model=self.bundle['models'][member['kind']]
            member['model_seeds']=getattr(model,'model_seeds',[getattr(model,'random_state',0) or 0])
            member['seed_average_space']='original_price' if isinstance(model,LogMeanRegressor) else 'single_deterministic_or_seed_fit'
        return result

class ExtraBudgetStop(RuntimeError):pass

class LogMeanRegressor(object):
    """Mean in original price space, represented as a log-output model."""
    def __init__(self,models,seeds):self.models=list(models);self.model_seeds=list(seeds)
    def predict(self,X):
        p=np.vstack([np.clip(np.asarray(m.predict(X),float),-700,700) for m in self.models]);offset=p.max(axis=0)
        return offset+np.log(np.mean(np.exp(p-offset),axis=0))
    @property
    def feature_importances_(self):return np.mean([m.feature_importances_ for m in self.models],axis=0)

class FitLedger(object):
    def __init__(self,root,limit,seconds,trace):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True);self.path=self.root/'extra_fit_ledger.jsonl'
        self.limit=limit;self.trace=trace;self.started=time.monotonic();self.seconds=seconds
        lines=self.path.read_text(encoding='utf-8').splitlines() if self.path.is_file() else []
        self.before=sum(json.loads(s)['event']=='fit_start' for s in lines if s.strip());self.used=self.before;self.cache_hits=0
    def room(self,count=1):return self.used+count<=self.limit and time.monotonic()-self.started<self.seconds
    def charge(self,**info):
        if not self.room():raise ExtraBudgetStop('新增fit/时间预算到达。')
        self.used+=1;self.append('fit_start',**info)
    def append(self,event,**info):
        row=dict(event=event,used=self.used,**info)
        with self.path.open('a',encoding='utf-8') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n');f.flush()

class Backend(object):
    def __init__(self,frame,y,rows,expert,category_fields,category_labels,root,identity,ledger,trace):
        self.frame=frame;self.y=np.asarray(y,float);self.rows=np.asarray(rows,int);self.expert=expert
        self.category_fields=category_fields;self.category_labels=category_labels;self.root=Path(root)/'fit_cache'
        self.root.mkdir(exist_ok=True);self.identity=identity;self.ledger=ledger;self.trace=trace;self.prepared={};self.memory={}
    def fit(self,indices,recipe,scope):
        indices=np.asarray(indices,int);cfg=recipe['params'];strength=recipe.get('expert_strength',1.);seeds=recipe.get('model_seeds',[0])
        prep_key=tuple(indices.tolist())
        if prep_key not in self.prepared:
            X=self.frame.iloc[indices];encoder=FeatureEncoder(self.category_fields,self.category_labels).fit(X)
            encoded=encoder.frame(X);scaler=MinMaxScaler().fit(encoded)
            self.prepared[prep_key]=(encoder,scaler,pd.DataFrame(scaler.transform(encoded),columns=encoded.columns,index=X.index))
        encoder,scaler,train=self.prepared[prep_key]
        applied={key:1.+strength*(value-1.) for key,value in self.expert.items()}
        expanded={key:applied.get(owner,1.) for key,owner in encoder.owners.items()}
        weights=np.asarray(recipe.get('family_weights',core.normalized_weights(cfg)),float);models={}
        for j,name in enumerate(core.MODEL_NAMES):
            if weights[j]<=0:continue
            fitted=[];actual_seeds=seeds if name in ('extratrees','gbdt') else [0]
            for seed in actual_seeds:
                prototype=core.instantiate_model(name,cfg,expanded)
                if name in ('extratrees','gbdt'):prototype.set_params(random_state=int(seed))
                dependency=expanded if name in ('lasso','ridge','svr','expert_tree') else None
                description=[self.identity,indices.tolist(),name,prototype.get_params(deep=False),dependency]
                key=hashlib.sha256(json.dumps(description,sort_keys=True).encode('utf-8')).hexdigest()
                if key in self.memory:model=self.memory[key];self.ledger.cache_hits+=1
                else:
                    cached=load_state(self.root/key,self.identity)
                    if cached is not None:model=cached['model'];self.ledger.cache_hits+=1
                    else:
                        self.ledger.charge(family=name,seed=int(seed),train_rows=self.rows[indices].tolist(),scope=scope)
                        with self.trace.span('v7_member_fit',scope=scope,family=name,model_seed=int(seed),
                                parameters=prototype.get_params(deep=False),train_rows=self.rows[indices].tolist(),expert_strength=strength):
                            model=prototype
                            try:model.fit(core._weighted_array(train,name,expanded),np.log(self.y[indices]))
                            except BaseException as exc:self.ledger.append('fit_failed',family=name,error=repr(exc));raise
                        self.ledger.append('fit_end',family=name,seed=int(seed))
                        save_state(self.root/key,{'model':model},self.identity)
                    self.memory[key]=model
                fitted.append(model)
            models[name]=LogMeanRegressor(fitted,actual_seeds) if len(fitted)>1 else fitted[0]
        return {'scaler':scaler,'models':models,'weights':weights,'feature_names':list(train.columns),'expert_map':expanded,
            'params':dict(cfg,model_seed=int(seeds[0]),model_seeds=list(seeds),expert_strength=strength),
            'log_target':True,'model_names':core.MODEL_NAMES,'input_encoder':encoder,'input_feature_names':list(self.frame.columns)}
    @staticmethod
    def predict(bundle,frame):return TypedBackend.predict(bundle,frame)
