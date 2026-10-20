# -*- coding: utf-8 -*-
"""Native service member: input is already MinMax-transformed exactly once."""
import numpy as np
import pandas as pd
from .price_automl_core import _weighted_array,expert_weight_vector


class NativeOptunaMember(object):
    def __init__(self,name,model,feature_names,expert_map,field_labels,scaler,owners=None,categories=None):
        self.name,self.model=name,model
        self.feature_names=list(feature_names);self.expert_map=dict(expert_map);self.field_labels=dict(field_labels)
        self.scale=np.asarray(scaler.scale_).copy();self.offset=np.asarray(scaler.min_).copy()
        self.owners=owners or {key:key for key in self.feature_names};self.categories=categories or {}
    def _frame(self,prepared):return pd.DataFrame(np.asarray(prepared,float),columns=self.feature_names)
    def predict(self,prepared):
        frame=self._frame(prepared);X=_weighted_array(frame,self.name,self.expert_map)
        return np.exp(np.clip(np.asarray(self.model.predict(X),float),-700,700))
    def explanation(self,prepared):
        frame=self._frame(prepared);X=_weighted_array(frame,self.name,self.expert_map)
        member={'kind':self.name,'target_transform':'log','preprocessing':'MinMax then per-model expert scaling'}
        if hasattr(self.model,'coef_'):
            coef=np.asarray(self.model.coef_).reshape(-1);values=np.asarray(X)[0]
            member['attribute_terms']=[{'field_id':key,'label':self.field_labels[key],'term':float(coef[j]*values[j]),'space':'log_price'} for j,key in enumerate(self.feature_names)]
            member['intercept']=float(np.asarray(self.model.intercept_).reshape(-1)[0])
            lo,inc={'lasso':(.5,2.5),'ridge':(.3,4.7),'svr':(.5,2.)}.get(self.name,(1.,0.))
            multiplier=expert_weight_vector(self.feature_names,self.expert_map,lo,inc)
            member['original_unit_coefficients']=[{'field_id':key,'label':self.field_labels[key],'coefficient':float(coef[j]*multiplier[j]*self.scale[j]),'space':'log_price_per_unit'} for j,key in enumerate(self.feature_names)]
        if self.name=='expert_tree':
            row=frame.iloc[0];node=self.model.root_;path=[]
            while not node.is_leaf and node.feature is not None:
                key=node.feature;j=self.feature_names.index(key);left=row[key]<=node.threshold
                path.append({'field_id':key,'label':self.field_labels[key],'branch':'不大于' if left else '大于',
                             'threshold':float((node.threshold-self.offset[j])/self.scale[j]),'expert_score':float(self.expert_map.get(key,1.))})
                node=node.left if left else node.right
            member.update(tree_path=path,leaf_samples=node.sample_num,leaf_price_range=[node.min_price,node.max_price])
        if hasattr(self.model,'feature_importances_'):
            member['feature_importance']=[{'field_id':key,'label':self.field_labels[key],'importance':float(value)} for key,value in zip(self.feature_names,self.model.feature_importances_)]
        # Older saved V2 wrappers predate the optional ownership metadata.
        owners=getattr(self,'owners',{});categories=getattr(self,'categories',{})
        for section in ('attribute_terms','original_unit_coefficients','feature_importance','tree_path'):
            for item in member.get(section,[]):
                key=item['field_id'];item['field_id']=owners.get(key,key)
                if key in categories:
                    item.update(component_id=key,encoding='one_hot',category=categories[key])
                    if section=='original_unit_coefficients':item['space']='log_price_per_indicator'
        return {'seed_instances':1,'calibration':1.,'members':[member]}
