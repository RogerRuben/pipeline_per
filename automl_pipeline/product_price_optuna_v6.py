# -*- coding: utf-8 -*-
"""Independent, target-first AutoML with sealed assessment and bounded resources."""
import copy
import json
from pathlib import Path
import numpy as np
from .product_price_optuna_v3 import DEFAULT as BASE,load_config as validate_base

DEFAULT={k:copy.deepcopy(v) for k,v in BASE.items() if k!='v3'}
DEFAULT['schema_version']='price-optuna-v6-pipeline-1.0'
DEFAULT['deployment']['model_scope']='full_data'
DEFAULT['search'].update(n_trials=60,timeout_seconds=900)
DEFAULT['search']['seed_low']=0
DEFAULT['v6']={
 'mode':'goal_first',
 'families':['ridge','lasso','svr','expert_tree','extratrees','gbdt'],
 'evaluation':{'mode':'nested_cv','outer_splits':4,'outer_repeats':1,'inner_splits':3,'inner_repeats':1,
               'seed':20261027,'tiny_threshold':8,'group_field':None,'holdout_ids':[],
               'holdout_fraction':.2,'require_declared_types':True},
 'train_gate':{'mode':'hard','mape_percent_max':10.,'r2_min':.9,'soft_penalty':1.},
 'budget':{'max_member_fits':2400,'tiny_trials':12,'cache_entries':128},
 'screening':{'enabled':True,'folds':2,'startup_candidates':8,'keep_quantile':.65,'exploration_fraction':.2},
 'ensemble':{'enabled':True,'weight_policy':'auto','max_members':3,'tiny_max_members':2,'shrinkage':.5,'max_weight':.85,
             'max_combinations':60,'pool_per_family':2,'fixed_weights_below':6},
 'features':{'subset_sizes':[0,4,8,12]},
 'selection':{'min_trials':8,'continue_after_targets':False,'finalists':3,'tail_weight':.05},
 'persistence':{'enabled':True,'resume':True,'directory':None,'checkpoint_interval':5},
 'logging':{'heartbeat_seconds':10.},
 'publish':{'policy':'allow_draft'}}


def load_config(path=None,overrides=None):
    cfg=copy.deepcopy(DEFAULT)
    def merge(dst,src,prefix=''):
        if not isinstance(src,dict):raise ValueError('V6配置须为对象：'+prefix)
        for key,value in src.items():
            if key not in dst:raise ValueError('未知V6配置：'+prefix+key)
            if isinstance(dst[key],dict) and key not in ('scores','field_types'):merge(dst[key],value,prefix+key+'.')
            else:dst[key]=copy.deepcopy(value)
    if path:merge(cfg,json.loads(Path(path).read_text(encoding='utf-8-sig')))
    if overrides:merge(cfg,overrides)
    if cfg['schema_version']!=DEFAULT['schema_version']:raise ValueError('V6配置版本无效。')
    base={k:copy.deepcopy(v) for k,v in cfg.items() if k!='v6'};base['schema_version']=BASE['schema_version'];base['v3']=copy.deepcopy(BASE['v3'])
    validated=validate_base(overrides=base)
    cfg.update({k:v for k,v in validated.items() if k not in ('schema_version','v3')})
    v=cfg['v6'];e=v['evaluation']
    for value,allowed,label in [(v['mode'],('goal_first','balanced','robust'),'模式'),(e['mode'],('nested_cv','locked_holdout'),'验收方式'),
        (v['train_gate']['mode'],('off','soft','hard'),'训练门槛'),(v['publish']['policy'],('allow_draft','require_targets'),'发布方式'),
        (v['ensemble']['weight_policy'],('auto','equal'),'融合份额方案')]:
        if value not in allowed:raise ValueError('V6'+label+'无效。')
    if cfg['deployment']['model_scope']!='full_data':raise ValueError('V6导出本轮允许开发记录的全量重拟合；不从验收轮中挑最好快照。')
    if cfg['input']['remove_outliers']:raise ValueError('V6不自动删除成品记录。')
    if not isinstance(v['families'],list) or not v['families'] or len(set(v['families']))!=len(v['families']) or set(v['families'])-set(DEFAULT['v6']['families']):raise ValueError('V6模型族列表无效。')
    for obj,key,minimum in [(e,'outer_splits',2),(e,'inner_splits',2),(e,'outer_repeats',1),(e,'inner_repeats',1),(e,'tiny_threshold',4),
        (v['budget'],'max_member_fits',1),(v['budget'],'tiny_trials',2),(v['budget'],'cache_entries',1),
        (v['screening'],'folds',1),(v['screening'],'startup_candidates',1),(v['ensemble'],'max_members',1),
        (v['ensemble'],'tiny_max_members',1),(v['ensemble'],'max_combinations',1),(v['ensemble'],'pool_per_family',1),
        (v['ensemble'],'fixed_weights_below',3),(v['selection'],'min_trials',1),(v['selection'],'finalists',1),
        (v['persistence'],'checkpoint_interval',1)]:
        if not isinstance(obj[key],int) or isinstance(obj[key],bool) or obj[key]<minimum:raise ValueError('V6整数设置无效：'+key)
    if min(cfg['search']['n_trials'],v['budget']['tiny_trials'])<len(v['families'])+1:raise ValueError('V6试验预算至少应覆盖中位数及每个启用模型族一次。')
    for flag in (v['screening']['enabled'],v['ensemble']['enabled'],e['require_declared_types'],v['persistence']['enabled'],v['persistence']['resume'],v['selection']['continue_after_targets']):
        if not isinstance(flag,bool):raise ValueError('V6开关须为true/false。')
    for obj,key in [(v['screening'],'keep_quantile'),(v['screening'],'exploration_fraction'),(v['ensemble'],'shrinkage'),
                    (v['ensemble'],'max_weight'),(v['selection'],'tail_weight'),(v['logging'],'heartbeat_seconds'),
                    (v['train_gate'],'mape_percent_max'),(v['train_gate'],'soft_penalty'),(e,'holdout_fraction')]:
        value=obj[key]
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not np.isfinite(value) or value<0:raise ValueError('V6数值设置无效：'+key)
    if not 0<v['screening']['keep_quantile']<=1 or not 0<=v['screening']['exploration_fraction']<=1:raise ValueError('V6初筛比例无效。')
    if v['ensemble']['shrinkage']<=0 or not .5<=v['ensemble']['max_weight']<=1:raise ValueError('V6融合须有正收缩强度，最大份额在0.5—1之间。')
    if max(v['ensemble']['max_members'],v['ensemble']['tiny_max_members'])>3:raise ValueError('小样本V6融合最多3个成员，避免份额自由度失控。')
    if not 0<e['holdout_fraction']<1 or v['logging']['heartbeat_seconds']<=0 or v['train_gate']['mape_percent_max']<=0:raise ValueError('V6比例、心跳或目标设置无效。')
    if v['train_gate']['r2_min'] is not None and (isinstance(v['train_gate']['r2_min'],bool) or not isinstance(v['train_gate']['r2_min'],(int,float)) or not np.isfinite(v['train_gate']['r2_min']) or v['train_gate']['r2_min']>1):raise ValueError('V6训练R²无效。')
    if not isinstance(e['seed'],int) or isinstance(e['seed'],bool) or not 0<=e['seed']<2**32-10000:raise ValueError('V6评估种子无效。')
    if e['group_field'] is not None and (not isinstance(e['group_field'],str) or not e['group_field']):raise ValueError('V6分组属性代号无效。')
    if not isinstance(e['holdout_ids'],list) or any(not isinstance(x,str) or not x for x in e['holdout_ids']) or len(set(e['holdout_ids']))!=len(e['holdout_ids']):raise ValueError('V6留出编号无效。')
    if v['persistence']['directory'] is not None and (not isinstance(v['persistence']['directory'],str) or not v['persistence']['directory'].strip()):raise ValueError('V6检查点目录无效。')
    if not isinstance(v['features']['subset_sizes'],list) or not v['features']['subset_sizes'] or any(not isinstance(k,int) or isinstance(k,bool) or k<0 for k in v['features']['subset_sizes']):raise ValueError('V6属性数量候选无效。')
    return cfg


def train_price_optuna_v6(*args,**kwargs):
    from .product_optuna_v6_adapter import train
    return train(*args,**kwargs)
