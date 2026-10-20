# -*- coding: utf-8 -*-
"""Strict V3 settings; one total budget shared by NSGA-II and MOTPE."""
import copy
import json
from pathlib import Path
import numpy as np
from .product_price_optuna_v2 import DEFAULT as V2_DEFAULT,load_config as v2_config

DEFAULT=copy.deepcopy(V2_DEFAULT)
DEFAULT['schema_version']='price-optuna-v3-pipeline-1.0'
DEFAULT['search']['n_trials']=300
DEFAULT['v3']={'nsga_fraction':.6,'num_anchors':3,'local_fraction':.25,'global_fraction':.2,'weight_only_fraction':.25,
               'anchor_refresh_interval':20,'startup_trials':10,'ei_candidates':24,'cache_entries':256,
               'warm_start':{'auto_history':True,'history_file':None,'config_file':None,'reuse_metrics':True,'max_history_trials':1000,'max_warm_proposals':5}}


def load_config(path=None,overrides=None):
    cfg=copy.deepcopy(DEFAULT)
    def merge(target,source,prefix=''):
        if not isinstance(source,dict):raise ValueError('配置须为对象：'+prefix)
        for key,value in source.items():
            if key not in target:raise ValueError('未知Optuna V3配置：'+prefix+key)
            if isinstance(target[key],dict) and key not in ('scores','field_types'):merge(target[key],value,prefix+key+'.')
            else:target[key]=copy.deepcopy(value)
    if path:merge(cfg,json.loads(Path(path).read_text(encoding='utf-8-sig')))
    if overrides:merge(cfg,overrides)
    if cfg['schema_version']!=DEFAULT['schema_version']:raise ValueError('Optuna V3配置版本无效。')
    base={k:v for k,v in cfg.items() if k!='v3'};base['schema_version']=V2_DEFAULT['schema_version'];base=v2_config(overrides=base)
    cfg.update({k:v for k,v in base.items() if k!='schema_version'})
    v=cfg['v3']
    for key in ('nsga_fraction','local_fraction','global_fraction','weight_only_fraction'):
        value=v[key]
        lower_ok=value>=0 if key in ('global_fraction','weight_only_fraction') and isinstance(value,(int,float)) else isinstance(value,(int,float)) and value>0
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not np.isfinite(value) or not lower_ok or value>1:raise ValueError('V3比例设置无效：'+key)
    for key in ('num_anchors','anchor_refresh_interval','startup_trials','ei_candidates','cache_entries'):
        if not isinstance(v[key],int) or isinstance(v[key],bool) or v[key]<(0 if key in ('startup_trials','cache_entries') else 1):raise ValueError('V3次数设置无效：'+key)
    w=v['warm_start']
    if not isinstance(w['reuse_metrics'],bool) or not isinstance(w['auto_history'],bool):raise ValueError('历史开关须为true/false。')
    for key in ('max_history_trials','max_warm_proposals'):
        if not isinstance(w[key],int) or isinstance(w[key],bool) or w[key]<1:raise ValueError('历史记录上限须为正整数。')
    for key in ('history_file','config_file'):
        if w[key] is not None and (not isinstance(w[key],str) or not w[key].strip()):raise ValueError('历史文件须为路径或null。')
    return cfg


def search_settings(cfg):
    result=copy.deepcopy(cfg['v3']);total=cfg['search']['n_trials']
    result['nsga_trials']=max(1,int(round(total*result['nsga_fraction'])))
    result['tpe_trials']=total-result['nsga_trials']
    return result


def train_price_optuna_v3(*args,**kwargs):
    from .product_price_optuna_v2 import train_price_optuna_v2
    return train_price_optuna_v2(*args,engine='optuna_v3',**kwargs)
