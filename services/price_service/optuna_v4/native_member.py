# -*- coding: utf-8 -*-
"""Each configuration owns its fitted encoder, scaler and six-model mixture."""
import numpy as np
import pandas as pd
from ..optuna_v2.typed_features import TypedBackend
from ..optuna_v2.native_member import NativeOptunaMember
from ..optuna_v2 import price_automl_core as core

LABELS={'lasso':'稀疏回归','ridge':'岭回归','expert_tree':'专家分裂树','extratrees':'极端随机树','svr':'支持向量回归','gbdt':'梯度提升树'}


class NativeConfigurationMember(object):
    def __init__(self,bundle,columns,labels):
        self.bundle=bundle;self.columns=list(columns);self.labels=dict(labels)
    def frame(self,values):return pd.DataFrame(np.asarray(values,float),columns=self.columns)
    def predict(self,values):return TypedBackend.predict(self.bundle,self.frame(values))[0]
    def explanation(self,values):
        bundle=self.bundle;frame=self.frame(values);encoder=bundle.get('input_encoder')
        encoded=encoder.frame(frame) if encoder else frame;prepared=bundle['scaler'].transform(encoded)
        owners=encoder.owners if encoder else {key:key for key in self.columns}
        labels={key:self.labels[owner]+('＝'+encoder.labels[key] if encoder and key in encoder.labels else '') for key,owner in owners.items()}
        members=[]
        for j,name in enumerate(core.MODEL_NAMES):
            if bundle['weights'][j]<=0:continue
            wrapper=NativeOptunaMember(name,bundle['models'][name],bundle['feature_names'],bundle['expert_map'],labels,bundle['scaler'],owners,encoder.labels if encoder else {})
            member=wrapper.explanation(prepared)['members'][0]
            member.update(weight=float(bundle['weights'][j]),label=LABELS[name],predicted_price_wan=float(wrapper.predict(prepared)[0]),model_seed=bundle['params'].get('model_seed',0))
            members.append(member)
        return {'seed_instances':len(members),'calibration':1.,'members':members,'configuration_parameters':bundle['params']}


def predict_saved(bundle,frame):
    components=[TypedBackend.predict(model,frame)[0] for model in bundle['configurations']]
    return np.column_stack(components).dot(np.asarray(bundle['weights'],float))
