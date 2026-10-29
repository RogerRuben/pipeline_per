# -*- coding: utf-8 -*-
"""Enable the supplied V2 pipeline without training or changing saved models."""
import argparse
import copy
import datetime
import hashlib
import json
import sys
import time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.product_price_optuna_v2 import load_config
from tools.pipeline_source_layout import SOURCE_FILES
from tools.recommendation_source_layout import SOURCE_FILES as RECOMMENDATION_FILES

PATCH_FILES=(
 'services/price_service/optuna_v2/__init__.py','services/price_service/optuna_v2/price_automl_core.py',
 'services/price_service/optuna_v2/optimizer.py','services/price_service/optuna_v2/native_member.py',
 'services/price_service/optuna_v2/SOURCE_PROVENANCE.json',
 'services/price_service/optuna_v2/typed_features.py','services/price_service/optuna_v2/search_history.py',
 'services/price_service/optuna_v2/training_observation.py',
 'services/price_service/optuna_v5/__init__.py','services/price_service/optuna_v5/engine.py','services/price_service/optuna_v5/fusion.py',
 'services/price_service/optuna_v5/persistence.py','services/price_service/optuna_v5/review.py',
 'services/price_service/optuna_v5/fixed_cv.py','services/price_service/optuna_v5/trace.py',
 'services/price_service/optuna_v6/__init__.py','services/price_service/optuna_v6/backend.py','services/price_service/optuna_v6/blend.py',
 'services/price_service/optuna_v6/protocol.py','services/price_service/optuna_v6/search.py','services/price_service/optuna_v6/engine.py',
 'tools/product_price_optuna_v6.py','tools/product_optuna_v6_adapter.py',
 'tools/product_price_optuna_v7.py','tools/product_optuna_v7_adapter.py','services/price_service/optuna_v7/__init__.py',
 'services/price_service/optuna_v7/backend.py','services/price_service/optuna_v7/engine.py','services/price_service/optuna_v7/review.py','services/price_service/optuna_v7/importance.py',
 'tools/product_effectiveness_import.py','app/server.py','app/static/portal.html','tools/recommendation_source_layout.py',
 'services/price_service/optuna_v2/cached_backend.py','services/price_service/optuna_v2/optimizer_v3.py',
 'services/price_service/optuna_v2/V3_DESIGN_PROVENANCE.json','tools/product_price_optuna_v3.py','tools/product_optuna_versions.py',
 'tools/product_price_optuna_v2.py','tools/product_optuna_v2_input.py','tools/upgrade_product_optuna_v2.py',
 'tools/product_pipeline.py','tools/product_portable.py','tools/product_saved_start.py','tools/product_export_saved.py',
 'tools/product_price_automl.py','tools/product_r4_input.py','tools/price_attribute_input.py',
 'services/price_service/native_bundle.py','services/price_service/app.py',
 'app/console_compat.py','app/product_table.py','app/static/price_ensemble.js',
 'APPLY_OPTUNA_V2.bat','APPLY_OPTUNA_V2_QUICK.bat','APPLY_OPTUNA_V2_DEEP.bat',
 'docs/OPTUNA_V2_PIPELINE.md','tools/pipeline_source_layout.py')+SOURCE_FILES+RECOMMENDATION_FILES


def atomic_json(path,value):
    temp=path.with_name(path.name+'.tmp');temp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    for attempt in range(5):
        try:temp.replace(path);return
        except PermissionError:
            if attempt==4:raise
            time.sleep(.05*2**attempt)


def upgrade(target,profile='standard'):
    target=Path(target).resolve();portable=target/'config/portable.json';cfgpath=target/'config/price_optuna_v2.json'
    if not portable.is_file() or not (target/'runtime/python38/python.exe').is_file():raise ValueError('请完整覆盖到已有离线包根目录，再运行本入口。')
    missing=[name for name in PATCH_FILES if name.endswith(('.py','.js','.json')) and not (target/name).is_file()]
    if missing:raise ValueError('补丁解压不完整，缺少：'+', '.join(missing))
    settings=json.loads(portable.read_text(encoding='utf-8-sig'));cfg=load_config(cfgpath) if cfgpath.is_file() else load_config()
    stamp=datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    if not cfgpath.is_file():
        oldpath=target/'config/price_automl_r4.json'
        if oldpath.is_file():
            old=json.loads(oldpath.read_text(encoding='utf-8-sig'));expert=old.get('expert') or {};quality=old.get('quality') or {}
            for key in ('scores','records','field_types'):
                if key in expert:cfg['expert'][key]=copy.deepcopy(expert[key])
            for oldkey,newkey in (('r2_min','r2_min'),('mape_percent_max','mape_percent_max'),('hard_gate','hard_gate')):
                if oldkey in quality:cfg['targets'][newkey]=quality[oldkey]
            cfg['input']['missing_tokens']=copy.deepcopy(old.get('missing_tokens',[]))
    if profile=='quick':cfg['search']['n_trials']=10
    elif profile=='deep':cfg['search']['n_trials']=5000
    elif profile!='standard':raise ValueError('未知Optuna V2模式：'+profile)
    load_config(overrides=cfg)
    provenance=json.loads((target/'services/price_service/optuna_v2/SOURCE_PROVENANCE.json').read_text(encoding='utf-8'))
    core=target/'services/price_service/optuna_v2/price_automl_core.py'
    if hashlib.sha256(core.read_bytes()).hexdigest()!=provenance['original_core_sha256']:raise ValueError('提供版本的训练核心文件不完整。')
    for path in (portable,cfgpath):
        if path.is_file():path.with_name(path.stem+'.before_optuna_v2_'+stamp+path.suffix).write_bytes(path.read_bytes())
    settings.update(price_engine='optuna_v2',startup_policy='saved_only')
    atomic_json(cfgpath,cfg);atomic_json(portable,settings)
    mp=target/'PACKAGE_MANIFEST.json'
    if mp.is_file():
        (target/'config'/('PACKAGE_MANIFEST.before_optuna_v2_'+stamp+'.json')).write_bytes(mp.read_bytes())
        manifest=json.loads(mp.read_text(encoding='utf-8'));entries={r['path']:r for r in manifest['immutable_files']}
        for name in PATCH_FILES:
            p=target/name
            if p.is_file():entries[name]={'path':name,'size':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
        manifest['immutable_files']=sorted(entries.values(),key=lambda x:x['path']);manifest['training_revision']='supplied_Optuna_V2_pipeline_1.0';atomic_json(mp,manifest)
    print('已启用提供的Optuna V2六模型联合搜索；预算%d轮。原价格评分和目标已保留。'%cfg['search']['n_trials'],flush=True)
    print('CHECK_INPUT检查输入；RETRAIN显式训练并导出三环节成品。START继续载入现有保存版本；EXPORT导出模型与环境。',flush=True)
    return cfg


def main():
    p=argparse.ArgumentParser();p.add_argument('--target',default=str(ROOT));p.add_argument('--profile',default='standard',choices=['standard','quick','deep']);a=p.parse_args()
    try:upgrade(a.target,a.profile)
    except Exception as e:print('Optuna V2适配配置失败：'+str(e),flush=True);return 1
    return 0
if __name__=='__main__':sys.exit(main())
