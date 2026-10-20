# -*- coding: utf-8 -*-
"""Install both selectable engines, preserving custom settings and saved models."""
import argparse
import copy
import datetime
import hashlib
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.upgrade_product_optuna_v2 import PATCH_FILES as V2_FILES,upgrade as upgrade_v2,atomic_json
from tools.product_price_optuna_v3 import load_config as v3_config
from tools.product_optuna_versions import saved_pointer,select

PATCH_FILES=tuple(dict.fromkeys(V2_FILES+(
    'services/price_service/optuna_v2/typed_features.py','services/price_service/optuna_v2/cached_backend.py',
    'services/price_service/optuna_v2/search_history.py','services/price_service/optuna_v2/optimizer_v3.py',
    'services/price_service/optuna_v2/V3_DESIGN_PROVENANCE.json',
    'tools/product_price_optuna_v3.py','tools/product_optuna_versions.py','tools/upgrade_product_optuna_dual.py',
    'APPLY_V2_V3.bat','APPLY_V2_V3_QUICK.bat','APPLY_V2_V3_DEEP.bat',
    'SELECT_V2.bat','SELECT_V3.bat','COMPARE_V2_V3.bat','docs/OPTUNA_V2_V3_PIPELINE.md','docs/OPTUNA_SMALL_SAMPLE.md','docs/OPTUNA_V3_METRIC_FIX.md')))


def upgrade(root,profile='standard',engine=None):
    root=Path(root).resolve();portable=root/'config/portable.json'
    if not portable.is_file():raise ValueError('请将补丁完整覆盖到已有离线包根目录。')
    missing=[name for name in PATCH_FILES if name.endswith(('.py','.js','.json')) and not (root/name).is_file()]
    if missing:raise ValueError('双版本补丁缺少文件：'+', '.join(missing))
    before=json.loads(portable.read_text(encoding='utf-8-sig'));chosen=engine or before.get('price_engine')
    if chosen not in ('optuna_v2','optuna_v3'):chosen='optuna_v3'
    cfg_v2=upgrade_v2(root,profile)
    path=root/'config/price_optuna_v3.json'
    if path.is_file():
        cfg=v3_config(path);stamp=datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        path.with_name('price_optuna_v3.before_dual_'+stamp+'.json').write_bytes(path.read_bytes())
    else:
        cfg=v3_config()
        for key,value in cfg_v2.items():
            if key not in ('schema_version','search'):cfg[key]=copy.deepcopy(value)
        for key,value in cfg_v2['search'].items():
            if key!='n_trials':cfg['search'][key]=copy.deepcopy(value)
    if profile in ('quick','deep'):cfg['search']['n_trials']=10 if profile=='quick' else 5000
    v3_config(overrides=cfg);atomic_json(path,cfg)
    for mode in ('optuna_v2','optuna_v3'):
        pointer=saved_pointer(root,mode)
        if pointer:atomic_json(root/'state'/('latest_'+mode+'.json'),pointer)
    select(root,chosen)
    manifest_path=root/'PACKAGE_MANIFEST.json'
    if manifest_path.is_file():
        manifest=json.loads(manifest_path.read_text(encoding='utf-8'));files={r['path']:r for r in manifest['immutable_files']}
        for name in PATCH_FILES:
            p=root/name
            if p.is_file():files[name]={'path':name,'size':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
        manifest.update(training_revision='Optuna_V2_V3_two_type_1.0',immutable_files=sorted(files.values(),key=lambda r:r['path']));atomic_json(manifest_path,manifest)
    print('双版本入口已安装。SELECT_V2/SELECT_V3选择；CHECK_INPUT检查；RETRAIN训练；START读取所选版本；COMPARE_V2_V3同条件对比；EXPORT导出所选成品。',flush=True)
    return cfg_v2,cfg


def main():
    p=argparse.ArgumentParser();p.add_argument('--profile',choices=['standard','quick','deep'],default='standard');p.add_argument('--engine',choices=['optuna_v2','optuna_v3']);a=p.parse_args()
    try:upgrade(ROOT,a.profile,a.engine);return 0
    except Exception as exc:print('双版本安装失败：'+str(exc),file=sys.stderr,flush=True);return 1
if __name__=='__main__':sys.exit(main())
