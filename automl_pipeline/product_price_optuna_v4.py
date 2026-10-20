# -*- coding: utf-8 -*-
"""V4 settings and bilingual product-table adapter entry point."""
import copy
import json
from pathlib import Path
import numpy as np
from .product_price_optuna_v2 import DEFAULT as V2_DEFAULT,load_config as v2_config

DEFAULT=copy.deepcopy(V2_DEFAULT)
DEFAULT['schema_version']='price-optuna-v4-pipeline-1.0'
DEFAULT['search']['n_trials']=300
DEFAULT['v4']={
 'search':{'mode':'hybrid','sampler':'nsga_tpe','nsga_fraction':.6,'global_fraction':.4,'coarse_splits':4,
           'budget_mode':'total','trials_per_split':100,'global_trials':100,'warm_candidates':3,
           'model_seeds':[0,42,137],'model_seed_mode':'range','model_seed_low':0,'model_seed_high':10000,'cache_entries':512,'adaptive_parameters':True},
 'splits':{'coverage':'auto','validation_rows':None,'max_splits':64,'random_splits':8,'enumeration_limit':100000,
           'seed':20261016,'stratify':False,'group_identical_inputs':True,'group_field':None},
 'train_gate':{'mode':'hard','scope':'pooled','mape_percent_max':10.,'r2_min':.9,'min_pass_fraction':1.,'soft_penalty':1.},
 'review':{'max_candidates':12},
 'ensemble':{'enabled':True,'max_configurations':3,'weight_trials':32,'non_degradation':True},
 'evaluation':{'mode':'development','holdout_ids':[],'holdout_fraction':.2,'holdout_seed':20261016,'require_declared_types':True},
 'publish':{'policy':'allow_draft'}}


def load_config(path=None,overrides=None):
    cfg=copy.deepcopy(DEFAULT)
    def merge(target,source,prefix=''):
        if not isinstance(source,dict):raise ValueError('配置须为对象：'+prefix)
        for key,value in source.items():
            if key not in target:raise ValueError('未知V4配置：'+prefix+key)
            if isinstance(target[key],dict) and key not in ('scores','field_types'):merge(target[key],value,prefix+key+'.')
            else:target[key]=copy.deepcopy(value)
    if path:merge(cfg,json.loads(Path(path).read_text(encoding='utf-8-sig')))
    if overrides:merge(cfg,overrides)
    if cfg['schema_version']!=DEFAULT['schema_version']:raise ValueError('V4配置版本无效。')
    base={k:v for k,v in cfg.items() if k!='v4'};base['schema_version']=V2_DEFAULT['schema_version'];base=v2_config(overrides=base)
    cfg.update({k:v for k,v in base.items() if k!='schema_version'})
    v=cfg['v4'];s=v['search'];p=v['splits'];g=v['train_gate'];e=v['evaluation']
    choices=[(s['mode'],('random','global','per_split','hybrid'),'搜索模式'),(s['sampler'],('nsga','tpe','nsga_tpe'),'优化器'),
             (s['model_seed_mode'],('range','list'),'模型种子模式'),
             (s['budget_mode'],('total','per_split'),'预算模式'),(p['coverage'],('auto','exhaustive','sampled'),'划分覆盖'),
             (g['mode'],('off','soft','hard'),'训练门槛'),(g['scope'],('pooled','every_split','fraction'),'训练门槛范围'),
             (e['mode'],('development','locked_holdout'),'评估模式'),(v['publish']['policy'],('allow_draft','require_targets'),'导出策略')]
    for value,allowed,label in choices:
        if value not in allowed:raise ValueError(label+'无效：'+str(value))
    for obj,keys in ((s,('coarse_splits','trials_per_split','global_trials','warm_candidates')),(p,('random_splits','enumeration_limit')),
                     (v['review'],('max_candidates',)),(v['ensemble'],('max_configurations','weight_trials'))):
        for key in keys:
            if not isinstance(obj[key],int) or isinstance(obj[key],bool) or obj[key]<1:raise ValueError('V4次数须为正整数：'+key)
    for obj,key in ((s,'cache_entries'),(p,'max_splits')):
        if not isinstance(obj[key],int) or isinstance(obj[key],bool) or obj[key]<0:raise ValueError(key+'须为非负整数。')
    if p['validation_rows'] is not None and (not isinstance(p['validation_rows'],int) or isinstance(p['validation_rows'],bool) or p['validation_rows']<2):raise ValueError('validation_rows须为至少2的整数或null。')
    for obj,key in ((s,'nsga_fraction'),(s,'global_fraction'),(g,'min_pass_fraction'),(e,'holdout_fraction')):
        if isinstance(obj[key],bool) or not isinstance(obj[key],(int,float)) or not np.isfinite(obj[key]) or not 0<obj[key]<=(1. if key=='min_pass_fraction' else .999999):raise ValueError('比例设置无效：'+key)
    if not np.isfinite(g['mape_percent_max']) or g['mape_percent_max']<=0:raise ValueError('训练MAPE目标须为正数。')
    if g['r2_min'] is not None and (not np.isfinite(g['r2_min']) or g['r2_min']>1):raise ValueError('训练R方目标须不大于1或null。')
    if not np.isfinite(g['soft_penalty']) or g['soft_penalty']<0:raise ValueError('软门槛强度须为非负数。')
    for flag in (s['adaptive_parameters'],p['stratify'],p['group_identical_inputs'],v['ensemble']['enabled'],v['ensemble']['non_degradation'],e['require_declared_types']):
        if not isinstance(flag,bool):raise ValueError('V4开关须为true/false。')
    if not isinstance(e['holdout_ids'],list) or any(not isinstance(x,str) or not x for x in e['holdout_ids']) or len(set(e['holdout_ids']))!=len(e['holdout_ids']):raise ValueError('holdout_ids须为不重复的成品编号列表。')
    if p['group_field'] is not None and (not isinstance(p['group_field'],str) or not p['group_field'].strip()):raise ValueError('group_field须为英文属性代号或null。')
    if not isinstance(s['model_seeds'],list):raise ValueError('model_seeds须为非空不重复列表。')
    for value in s['model_seeds']+[s['model_seed_low'],s['model_seed_high'],p['seed'],e['holdout_seed']]:
        if not isinstance(value,int) or isinstance(value,bool) or not 0<=value<2**32:raise ValueError('模型及划分种子须为非负32位整数。')
    if not s['model_seeds'] or len(set(s['model_seeds']))!=len(s['model_seeds']):raise ValueError('model_seeds须为非空不重复列表。')
    if s['model_seed_high']<s['model_seed_low']:raise ValueError('模型种子上界不得小于下界。')
    return cfg


def train_price_optuna_v4(*args,**kwargs):
    from .product_optuna_v4_adapter import train
    return train(*args,**kwargs)
