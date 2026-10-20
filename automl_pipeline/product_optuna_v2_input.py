# -*- coding: utf-8 -*-
"""Bilingual product-table adaptation for the supplied Optuna V2 engine."""
import hashlib
import json
from pathlib import Path
from app.product_table import product_table_rows
from app.historical_onboarding import HistoricalProductOnboarding
from .product_price_optuna_v2 import load_config,workbook_config,dataset,training_split
from .product_effectiveness_bootstrap import load_config as effectiveness_config
from tools.product_price_automl import write_json


def prepare(source,root,settings):
    source,root=Path(source),Path(root);raw=source.read_bytes()
    engine=settings.get('price_engine','optuna_v2');loader=load_config
    if engine=='optuna_v3':
        from .product_price_optuna_v3 import load_config as loader
    elif engine=='optuna_v4':
        from .product_price_optuna_v4 import load_config as loader
    cfg=loader(root/('config/price_'+engine+'.json'))
    table=product_table_rows(source.name,raw,require_dual_header=True)
    write_json(root/'logs/input_diagnostics.json',table['diagnostics'])
    analyzed=HistoricalProductOnboarding().analyze(source.name,raw,settings['product_code'],settings['product_name'],cfg['input']['missing_tokens'],require_dual_header=True)
    if not analyzed['price_column_detected']:raise ValueError('成品表须包含价格列，建议中文名价格(万元)，英文代号price_wan。')
    known={p['parameter_id'] for p in analyzed['data']['parameters']}
    cfg=workbook_config(source,cfg,known,loader=loader)
    from services.price_service.optuna_v2.typed_features import restore_configurations
    restore_configurations(analyzed['data'],table,cfg['expert']['field_types'],cfg['input']['missing_tokens'])
    frame,y,specs,mappings,groups,ids=dataset(analyzed['data'],cfg)
    _,split_report=training_split(len(y),cfg)
    if engine=='optuna_v3':
        warm=cfg['v3']['warm_start']
        if warm['auto_history'] and not warm['history_file'] and not warm['config_file']:
            from .product_optuna_versions import saved_pointer
            for previous in ('optuna_v3','optuna_v2'):
                pointer=saved_pointer(root,previous)
                candidate=root/pointer['output']/'price/study_history.json' if pointer else None
                if candidate is not None and candidate.is_file():
                    warm['history_file']=str(candidate.resolve());break
        for key in ('history_file','config_file'):
            value=cfg['v3']['warm_start'][key]
            if value:
                path=Path(value);path=path if path.is_absolute() else root/path
                if not path.is_file():raise ValueError('V3历史文件不存在：'+str(path))
                cfg['v3']['warm_start'][key]=str(path.resolve())
    effect=effectiveness_config(source,allow_legacy=False);effect['auto_discover_couplings']=False
    code_files=('automl_pipeline/product_price_optuna_v2.py','automl_pipeline/product_optuna_v2_input.py','tools/product_r4_input.py','tools/price_attribute_input.py',
                'services/price_service/optuna_v2/price_automl_core.py','services/price_service/optuna_v2/optimizer.py','services/price_service/optuna_v2/native_member.py',
                'services/price_service/optuna_v2/typed_features.py','services/price_service/optuna_v2/search_history.py')
    if engine=='optuna_v3':code_files+=('automl_pipeline/product_price_optuna_v3.py','services/price_service/optuna_v2/optimizer_v3.py','services/price_service/optuna_v2/cached_backend.py')
    if engine=='optuna_v4':
        code_files+=('automl_pipeline/product_price_optuna_v4.py','automl_pipeline/product_optuna_v4_adapter.py','services/price_service/optuna_v4/backend.py',
                     'services/price_service/optuna_v4/optimizer.py','services/price_service/optuna_v4/parameters.py','services/price_service/optuna_v4/metrics.py',
                     'services/price_service/optuna_v4/splits.py','services/price_service/optuna_v4/native_member.py','services/price_service/optuna_v2/optimizer_v3.py')
        from .product_optuna_v4_adapter import groups_for
        from services.price_service.optuna_v4.splits import holdout_indices,make_splits
        from services.price_service.optuna_v4.optimizer import budget_plan
        group_ids=groups_for(frame,ids,analyzed['data'],cfg)
        dev,outer=holdout_indices(len(y),ids,group_ids,cfg)
        if len(outer) and cfg['v4']['evaluation']['require_declared_types'] and set(frame.columns)-set(cfg['expert']['field_types']):raise ValueError('固定留出模式须声明全部属性的数值/配置类型。')
        parts,plan=make_splits(y[dev],group_ids[dev],cfg)
        if cfg['v4']['search']['mode']=='random':parts=parts[:cfg['v4']['splits']['random_splits']]
        budget_plan(cfg,len(parts));split_report=dict(plan,development_rows=len(dev),locked_holdout_rows=len(outer))
    identity={'input_sha256':hashlib.sha256(raw).hexdigest(),'price_config':cfg,'effect_config':effect,
              'code':{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in code_files}}
    if engine=='optuna_v3':identity['history_files']={key:hashlib.sha256(Path(value).read_bytes()).hexdigest() for key,value in cfg['v3']['warm_start'].items() if key in ('history_file','config_file') and value}
    notes=['保留提供版本的NSGA-II双目标、六模型、MinMax、对数价格和联合划分种子搜索。',
           '价格1—5分专家评分与效能偏好分别读取；未提供评分按该版本默认1分。',
           '开发指标来自被搜索的验证划分；默认服务模型按所选参数在全量记录重拟合。',
           '没有声明耦合关系时不自动生成耦合。']
    notes.append('属性只有数值与配置类型；配置独热编码，数值在训练集上MinMax缩放。')
    if split_report.get('adjusted'):notes.append('小样本验证自动调整：%d条训练、%d条验证；配置最低%d条。'%(split_report['training_rows'],split_report['validation_rows'],split_report['min_samples']))
    if engine=='optuna_v3':notes[0]='V3：NSGA-II广域搜索后，以Pareto多锚点MOTPE精修；总预算在两阶段共享。'
    if engine=='optuna_v4':
        notes[0]='V4：%s；%d个固定划分复核，训练门槛%s/%s。'%(cfg['v4']['search']['mode'],split_report['evaluated_splits'],cfg['v4']['train_gate']['mode'],cfg['v4']['train_gate']['scope'])
        notes[2]='训练、跨划分开发、可选固定外层留出分别报告。全部开发记录重拟合后再次检查训练指标；开发成绩参与选择。'
    if not cfg['expert']['enabled']:notes[1]='纯数据对照已启用；原1—5分价格评分保留，训练使用统一默认倍率。'
    details={'records':len(y),'attributes':len(specs),'independent_groups':len(set(groups)),
             'source_filename':source.name,'input_sha256':identity['input_sha256'],'fingerprint':hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest(),
             'notes':notes,'input_diagnostics':table['diagnostics'],'price_expert_scores_count':len(cfg['expert']['scores']),
             'virtual_data':identity['input_sha256']==settings.get('demo_input_sha256'),'price_engine':engine,
             'attribute_types':{p['parameter_id']:('配置类型' if p['parameter_id'] in mappings else '数值') for p in specs},'validation_split':split_report}
    return settings,cfg,effect,details
