# -*- coding: utf-8 -*-
"""V7 / V3+: preserve the supplied V3 search; budget the small enhancement."""
import copy,hashlib,json
from pathlib import Path
import numpy as np
from .product_price_optuna_v3 import DEFAULT as V3_DEFAULT,load_config as validate_v3
DEFAULT=copy.deepcopy(V3_DEFAULT);DEFAULT['schema_version']='price-optuna-v7-pipeline-1.0'
DEFAULT['v3']['warm_start']['auto_history']=False
DEFAULT['search']['timeout_seconds']=900
DEFAULT['v7']={
 'mode':'balanced',
 'review':{'enabled':True,'candidate_pool':10,'max_candidates':5,'folds':4,'seed':20261030,
           'tiny_threshold':8,'anchor_mape_tolerance_pp':1.,'anchor_r2_tolerance':.02,
           'cv_r2_tolerance':.02,'tail_weight':.05,'anchor_weight':.4},
 'budget':{'extra_fit_ratio':.2,'minimum_extra_fits':96,'max_extra_fits':500,'extra_seconds':180},
 'model_seeds':{'enabled':True,'count':3,'low':0,'high':10000,'max_candidates':2},
 'fusion':{'enabled':True,'drop_weak_members':True,'max_configurations':3,'max_prior_movement':.35,'ridge_penalty':1.,'max_combinations':12},
 'expert_strengths':[1.,.5,0.],
 'consistency':{'mode':'off','max_error_percent':10.,'min_coverage':1.,'permutations':8,'seed':20261030,
                'tie_tolerance':1e-6,'soft_penalty_pp':1.,'mapping':'expert_grade_counts_rank_bins'},
 'persistence':{'enabled':True,'resume':True,'directory':None,'keep_fit_cache':False},
 'logging':{'heartbeat_seconds':10.},'publish':{'policy':'allow_draft'}}

def load_config(path=None,overrides=None):
    cfg=copy.deepcopy(DEFAULT)
    def merge(dst,src,prefix=''):
        if not isinstance(src,dict):raise ValueError('V7配置须为JSON对象：'+prefix)
        for k,v in src.items():
            if k not in dst:raise ValueError('未知V7配置：'+prefix+k)
            if isinstance(dst[k],dict) and k not in ('scores','field_types'):merge(dst[k],v,prefix+k+'.')
            else:dst[k]=copy.deepcopy(v)
    if path:merge(cfg,json.loads(Path(path).read_text(encoding='utf-8-sig')))
    if overrides:merge(cfg,overrides)
    if cfg['schema_version']!=DEFAULT['schema_version']:raise ValueError('V7配置版本无效。')
    base={k:copy.deepcopy(v) for k,v in cfg.items() if k!='v7'};base['schema_version']=V3_DEFAULT['schema_version']
    base=validate_v3(overrides=base);cfg.update({k:v for k,v in base.items() if k!='schema_version'})
    v=cfg['v7'];r=v['review'];c=v['consistency'];b=v['budget'];s=v['model_seeds'];f=v['fusion']
    if v['mode'] not in ('balanced','goal_first','baseline_only'):raise ValueError('V7模式须为balanced/goal_first/baseline_only。')
    if c['mode'] not in ('off','report','soft','strict'):raise ValueError('专家一致性模式须为off/report/soft/strict。')
    if c['mapping']!='expert_grade_counts_rank_bins':raise ValueError('当前五档映射只支持预先固定专家各档数量的排名区间。')
    if v['publish']['policy'] not in ('allow_draft','require_targets'):raise ValueError('V7发布策略无效。')
    if cfg['deployment']['model_scope']!='full_data':raise ValueError('V7保留原V3获分快照，主运行模型统一为全量拟合；原V3可另选selected_split。')
    if cfg['input']['remove_outliers']:raise ValueError('V7不静默删除成品记录。')
    for obj,k,minimum,maximum in [(r,'candidate_pool',1,100),(r,'max_candidates',1,20),(r,'folds',2,20),(r,'tiny_threshold',4,20),
        (b,'minimum_extra_fits',0,100000),(b,'max_extra_fits',1,100000),(s,'count',1,10),(s,'max_candidates',1,10),
        (f,'max_configurations',1,3),(f,'max_combinations',1,100),(c,'permutations',2,100)]:
        if not isinstance(obj[k],int) or isinstance(obj[k],bool) or not minimum<=obj[k]<=maximum:raise ValueError('V7整数范围无效：'+k)
    for obj,k in [(r,'enabled'),(s,'enabled'),(f,'enabled'),(f,'drop_weak_members')]+[(v['persistence'],k) for k in ('enabled','resume','keep_fit_cache')]:
        if not isinstance(obj[k],bool):raise ValueError('V7开关须为true/false：'+k)
    for obj,k in [(r,k) for k in ('anchor_mape_tolerance_pp','anchor_r2_tolerance','cv_r2_tolerance','tail_weight','anchor_weight')]+[
        (b,'extra_fit_ratio'),(b,'extra_seconds'),(f,'max_prior_movement'),(f,'ridge_penalty'),(c,'max_error_percent'),
        (c,'min_coverage'),(c,'tie_tolerance'),(c,'soft_penalty_pp'),(v['logging'],'heartbeat_seconds')]:
        x=obj[k]
        if isinstance(x,bool) or not isinstance(x,(int,float)) or not np.isfinite(x) or x<0:raise ValueError('V7数值设置无效：'+k)
    if not 0<=r['anchor_weight']<=1 or not 0<=f['max_prior_movement']<=1 or f['ridge_penalty']<=0:raise ValueError('V7融合/锚点比例无效。')
    if not 0<=c['max_error_percent']<=100 or not 0<=c['min_coverage']<=1 or b['extra_seconds']<=0 or v['logging']['heartbeat_seconds']<=0:raise ValueError('V7比例/时间无效。')
    for obj,k in ((r,'seed'),(s,'low'),(s,'high'),(c,'seed')):
        if not isinstance(obj[k],int) or isinstance(obj[k],bool) or not 0<=obj[k]<2**32:raise ValueError('V7种子无效：'+k)
    if s['high']-s['low']+1<s['count']:raise ValueError('训练种子范围不足以产生互异种子。')
    if not isinstance(v['expert_strengths'],list) or not v['expert_strengths'] or any(isinstance(x,bool) or not isinstance(x,(int,float)) or not np.isfinite(x) or not 0<=x<=1 for x in v['expert_strengths']):raise ValueError('专家强度须为0—1候选列表。')
    p=v['persistence']['directory']
    if p is not None and (not isinstance(p,str) or not p.strip()):raise ValueError('V7检查点目录无效。')
    return cfg

def baseline_config(cfg):
    raw={k:copy.deepcopy(v) for k,v in cfg.items() if k!='v7'};raw['schema_version']=V3_DEFAULT['schema_version']
    return validate_v3(overrides=raw)

def history_references(cfg):
    result={}
    for key in ('history_file','config_file'):
        value=cfg['v3']['warm_start'][key]
        if value:
            path=Path(value).resolve()
            if not path.is_file():raise ValueError('V3历史/参数文件不存在：'+str(path))
            result[key]={'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    return result

def train_price_optuna_v7(*args,**kwargs):
    from .product_optuna_v7_adapter import train
    return train(*args,**kwargs)
