# -*- coding: utf-8 -*-
"""Install V5 and telemetry, preserving every existing model and input file."""
import argparse
import copy
import datetime
import hashlib
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.upgrade_product_v4 import PATCH_FILES as V4_FILES
from tools.upgrade_product_optuna_v2 import atomic_json
from tools.product_price_optuna_v5 import load_config
from tools.product_modes import select,register

PATCH_FILES=tuple(dict.fromkeys(V4_FILES+(
 'tools/product_price_optuna_v5.py','tools/product_optuna_v5_adapter.py','tools/upgrade_product_v5.py','docs/OPTUNA_V5_PIPELINE.md',
 'APPLY_V5.bat','APPLY_V5_QUICK.bat','APPLY_V5_DEEP.bat','SELECT_V5.bat','V5_GOAL_FIRST.bat','V5_BALANCED.bat','V5_ROBUST.bat','RESUME.bat')))


def upgrade(root,profile='standard'):
    root=Path(root).resolve();portable=root/'config/portable.json'
    if not portable.is_file() or not (root/'runtime/python38/python.exe').is_file():raise ValueError('请完整覆盖到已有离线包根目录，再运行APPLY_V5。')
    missing=[name for name in PATCH_FILES if not (root/name).is_file()]
    if missing:raise ValueError('V5补丁缺失文件：'+','.join(missing))
    from tools.product_price_optuna_v2 import load_config as v2_config
    from tools.product_price_optuna_v3 import load_config as v3_config
    from tools.product_price_optuna_v4 import load_config as v4_config
    settings=json.loads(portable.read_text(encoding='utf-8-sig'));configs={}
    for engine,loader in [('optuna_v2',v2_config),('optuna_v3',v3_config),('optuna_v4',v4_config)]:
        path=root/('config/price_'+engine+'.json');configs[engine]=loader(path) if path.is_file() else loader()
    path=root/'config/price_optuna_v5.json'
    if path.is_file():cfg=load_config(path)
    else:
        cfg=load_config();source=configs.get(settings.get('price_engine'),configs['optuna_v3'])
        for name in ('expert','input','targets','validation'):cfg[name]=copy.deepcopy(source[name])
        cfg['input']['remove_outliers']=False
    if profile=='quick':cfg['search']['n_trials']=40;cfg['v5']['enhancement']['max_new_fits']=0
    elif profile=='deep':
        cfg['search']['n_trials']=300;cfg['search']['timeout_seconds']=1800;cfg['v5']['enhancement']['max_new_fits']=5000;cfg['v5']['budget']['max_member_fits']=5000
        cfg['v5']['enhancement']['split_anchors']=3
    elif profile!='standard':raise ValueError('未知V5配置。')
    cfg=load_config(overrides=cfg);configs['optuna_v5']=cfg
    provenance=json.loads((root/'services/price_service/optuna_v2/SOURCE_PROVENANCE.json').read_text(encoding='utf-8'))
    if hashlib.sha256((root/'services/price_service/optuna_v2/price_automl_core.py').read_bytes()).hexdigest()!=provenance['original_core_sha256']:raise ValueError('V2原始核心摘要不一致。')
    stamp=datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    for file in [portable]+[root/('config/price_'+name+'.json') for name in configs]:
        if file.is_file():file.with_name(file.stem+'.before_v5_'+stamp+file.suffix).write_bytes(file.read_bytes())
    for name,value in configs.items():atomic_json(root/('config/price_'+name+'.json'),value)
    from tools.product_optuna_versions import saved_pointer
    for engine in configs:
        pointer=saved_pointer(root,engine)
        if pointer and (root/pointer['output']/'price/effective_config.json').is_file():register(root,pointer)
    select(root,'optuna_v5')
    path=root/'PACKAGE_MANIFEST.json'
    if path.is_file():
        manifest=json.loads(path.read_text(encoding='utf-8'));entries={row['path']:row for row in manifest['immutable_files']}
        for name in PATCH_FILES:
            file=root/name;entries[name]={'path':name,'size':file.stat().st_size,'sha256':hashlib.sha256(file.read_bytes()).hexdigest()}
        manifest.update(training_revision='Optuna_V5_v3_preserving_1.0',immutable_files=sorted(entries.values(),key=lambda row:row['path']));atomic_json(path,manifest)
    print('V5已安装。V2/V3日志增加训练与开发指标；旧模型保留。MODE_MENU切换；RETRAIN/RESUME显式训练；START只加载保存模型。',flush=True)
    return cfg


def main():
    p=argparse.ArgumentParser();p.add_argument('--profile',choices=['standard','quick','deep'],default='standard');a=p.parse_args()
    try:upgrade(ROOT,a.profile);return 0
    except Exception as exc:print('V5安装失败：'+str(exc),file=sys.stderr,flush=True);return 1
if __name__=='__main__':sys.exit(main())
