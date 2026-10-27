# -*- coding: utf-8 -*-
"""V3-preserving V5 configuration and native-pipeline entry point."""
import copy
import json
from pathlib import Path
import numpy as np
from .product_price_optuna_v3 import DEFAULT as V3_DEFAULT,load_config as v3_config

DEFAULT=copy.deepcopy(V3_DEFAULT);DEFAULT['schema_version']='price-optuna-v5-pipeline-1.0'
DEFAULT['deployment']['model_scope']='full_data'
DEFAULT['v3']['warm_start']['auto_history']=False
DEFAULT['v5']={
 'mode':'goal_first',
 'protocol':'fixed_cv','weight_policy':'baseline_fixed',
 'cv':{'n_splits':4,'repeats':1,'seed':20261027,'min_mape_improvement_pp':.001,
       'r2_tolerance':1e-8,'max_fold_regression_pp':2.,'max_tail_regression_pp':2.},
 'logging':{'heartbeat_seconds':10.},
 'baseline':{'reference_dir':None,'auto_reference':True,'finish':'fixed_budget'},
 'train_gate':{'mode':'hard','mape_percent_max':10.,'r2_min':.9,'soft_penalty':1.},
 'budget':{'max_member_fits':None},
 'enhancement':{'max_new_fits':60,'families':['ridge','lasso','svr','expert_tree','extratrees','gbdt'],'pool_size':12,
                'patience':30,'split_anchors':1,'model_seed_low':0,'model_seed_high':10000,'continue_after_targets':False},
 'fusion':{'enabled':True,'non_degradation':True,'maxiter':200},
 'audit':{'enabled':True,'max_splits':4,'max_members':16,'validation_rows':None,'seed':20261023,'balanced_weight':.25},
 'evaluation':{'mode':'development','holdout_ids':[],'holdout_fraction':.2,'holdout_seed':20261016,'require_declared_types':True},
 'persistence':{'enabled':True,'resume':True,'directory':None,'checkpoint_interval':10},
 'publish':{'policy':'allow_draft'}}


def load_config(path=None,overrides=None):
    cfg=copy.deepcopy(DEFAULT)
    def merge(dst,src,prefix=''):
        if not isinstance(src,dict):raise ValueError('V5配置须为对象：'+prefix)
        for key,value in src.items():
            if key not in dst:raise ValueError('未知V5配置：'+prefix+key)
            if isinstance(dst[key],dict) and key not in ('scores','field_types'):merge(dst[key],value,prefix+key+'.')
            else:dst[key]=copy.deepcopy(value)
    if path:
        raw=json.loads(Path(path).read_text(encoding='utf-8-sig'))
        # Saved V5.0 artefacts keep their old identity. APPLY_V5_1 explicitly
        # migrates the editable training configuration to the new protocol.
        if 'v5' in raw and 'protocol' not in raw['v5']:
            raw=copy.deepcopy(raw);raw['v5']['protocol']='legacy_split'
            raw['v5'].setdefault('enhancement',{}).setdefault('max_new_fits',0)
        merge(cfg,raw)
    if overrides:merge(cfg,overrides)
    explicit_scope=(overrides or {}).get('deployment',{}).get('model_scope')
    if cfg['v5']['protocol']=='legacy_split' and not explicit_scope and not path:cfg['deployment']['model_scope']='selected_split'
    if cfg['schema_version']!=DEFAULT['schema_version']:raise ValueError('V5配置版本无效。')
    base={k:copy.deepcopy(v) for k,v in cfg.items() if k!='v5'};base['schema_version']=V3_DEFAULT['schema_version'];base=v3_config(overrides=base)
    cfg.update({k:v for k,v in base.items() if k!='schema_version'});v=cfg['v5'];g=v['train_gate'];e=v['evaluation']
    for value,allowed,label in [(v['mode'],('goal_first','balanced','robust'),'模式'),(g['mode'],('off','soft','hard'),'训练门槛'),
        (v['protocol'],('fixed_cv','legacy_split'),'搜索协议'),(v['weight_policy'],('baseline_fixed','uniform_fixed'),'固定权重方案'),
        (v['baseline']['finish'],('fixed_budget','targets_met'),'基线结束策略'),(e['mode'],('development','locked_holdout'),'评估方式'),
        (v['publish']['policy'],('allow_draft','require_targets'),'发布策略')]:
        if value not in allowed:raise ValueError('V5'+label+'无效：'+str(value))
    for obj,key,minimum in [(v['enhancement'],'max_new_fits',0),(v['enhancement'],'pool_size',6),(v['enhancement'],'patience',1),
        (v['enhancement'],'split_anchors',1),(v['fusion'],'maxiter',1),(v['audit'],'max_splits',1),(v['audit'],'max_members',1),
        (v['persistence'],'checkpoint_interval',1)]:
        if not isinstance(obj[key],int) or isinstance(obj[key],bool) or obj[key]<minimum:raise ValueError('V5整数设置无效：'+key)
    for key,minimum in [('n_splits',2),('repeats',1)]:
        value=v['cv'][key]
        if not isinstance(value,int) or isinstance(value,bool) or not minimum<=value<=100:raise ValueError('V5固定CV整数设置无效：'+key)
    if not isinstance(v['cv']['seed'],int) or isinstance(v['cv']['seed'],bool) or not 0<=v['cv']['seed']<2**32-v['cv']['repeats']:raise ValueError('V5固定CV种子无效。')
    for obj,key in [(v['cv'],k) for k in ('min_mape_improvement_pp','r2_tolerance','max_fold_regression_pp','max_tail_regression_pp')]+[(v['logging'],'heartbeat_seconds')]:
        value=obj[key]
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not np.isfinite(value) or value<0:raise ValueError('V5固定CV/日志数值设置无效：'+key)
    if v['logging']['heartbeat_seconds']<=0:raise ValueError('日志心跳间隔须大于0。')
    if v['protocol']=='fixed_cv' and v['enhancement']['split_anchors']!=1:raise ValueError('固定CV禁止搜索容易的划分；split_anchors须为1。')
    limit=v['budget']['max_member_fits']
    if limit is not None and (not isinstance(limit,int) or isinstance(limit,bool) or limit<1):raise ValueError('max_member_fits须为正整数或null。')
    if limit is not None and limit<6*cfg['search']['n_trials']+12:raise ValueError('总fit预算需至少为6*n_trials+12，保证原V3基线不会因预算上限被截断。请降低基线轮数或提高总fit预算。')
    for flag in [v['baseline']['auto_reference'],v['fusion']['enabled'],v['fusion']['non_degradation'],v['audit']['enabled'],
                 v['enhancement']['continue_after_targets'],v['persistence']['enabled'],v['persistence']['resume'],e['require_declared_types']]:
        if not isinstance(flag,bool):raise ValueError('V5开关须为true/false。')
    families=v['enhancement']['families']
    if not isinstance(families,list) or not families or len(set(families))!=len(families) or set(families)-{'ridge','lasso','svr','expert_tree','extratrees','gbdt'}:raise ValueError('V5模型族列表无效。')
    for name in ('model_seed_low','model_seed_high'):
        value=v['enhancement'][name]
        if not isinstance(value,int) or isinstance(value,bool) or not 0<=value<2**32:raise ValueError('V5模型种子范围无效。')
    if v['enhancement']['model_seed_high']<v['enhancement']['model_seed_low']:raise ValueError('V5模型种子上界低于下界。')
    for obj,key in ((g,'mape_percent_max'),(g,'soft_penalty'),(v['audit'],'balanced_weight'),(e,'holdout_fraction')):
        value=obj[key]
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not np.isfinite(value) or value<0:raise ValueError('V5数值设置无效：'+key)
    if g['mape_percent_max']<=0 or not 0<e['holdout_fraction']<1:raise ValueError('V5目标/比例设置无效。')
    if g['r2_min'] is not None and (not isinstance(g['r2_min'],(int,float)) or isinstance(g['r2_min'],bool) or not np.isfinite(g['r2_min']) or g['r2_min']>1):raise ValueError('V5训练R方设置无效。')
    if v['audit']['validation_rows'] is not None and (not isinstance(v['audit']['validation_rows'],int) or isinstance(v['audit']['validation_rows'],bool) or v['audit']['validation_rows']<2):raise ValueError('V5验证条数至少2或null。')
    if not isinstance(e['holdout_ids'],list) or any(not isinstance(x,str) or not x for x in e['holdout_ids']) or len(set(e['holdout_ids']))!=len(e['holdout_ids']):raise ValueError('V5外层编号列表无效。')
    for key in ('seed',):
        value=v['audit'][key]
        if not isinstance(value,int) or isinstance(value,bool) or not 0<=value<2**32:raise ValueError('V5复核种子无效。')
    if not isinstance(e['holdout_seed'],int) or isinstance(e['holdout_seed'],bool) or not 0<=e['holdout_seed']<2**32:raise ValueError('V5外层种子无效。')
    for value in (v['baseline']['reference_dir'],v['persistence']['directory']):
        if value is not None and (not isinstance(value,str) or not value.strip()):raise ValueError('V5目录设置须为路径或null。')
    if cfg['input']['remove_outliers']:raise ValueError('V5不自动删除成品记录，请先确认处理好的输入表。')
    return cfg


def baseline_config(cfg):
    value={k:copy.deepcopy(v) for k,v in cfg.items() if k!='v5'};value['schema_version']=V3_DEFAULT['schema_version']
    return v3_config(overrides=value)


def train_price_optuna_v5(*args,**kwargs):
    from .product_optuna_v5_adapter import train
    return train(*args,**kwargs)
