# -*- coding: utf-8 -*-
"""Install switchable V4 without touching existing model files or training."""
import argparse
import copy
import datetime
import hashlib
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.upgrade_product_optuna_dual import PATCH_FILES as DUAL_FILES
from tools.product_price_optuna_v4 import load_config as v4_config
from tools.product_price_optuna_v2 import load_config as v2_config
from tools.product_price_optuna_v3 import load_config as v3_config
from tools.product_modes import register,select
from tools.upgrade_product_optuna_v2 import atomic_json

PATCH_FILES=tuple(dict.fromkeys(DUAL_FILES+(
 'services/price_service/optuna_v4/__init__.py','services/price_service/optuna_v4/backend.py','services/price_service/optuna_v4/metrics.py',
 'services/price_service/optuna_v4/splits.py','services/price_service/optuna_v4/parameters.py','services/price_service/optuna_v4/optimizer.py',
 'services/price_service/optuna_v4/native_member.py','tools/product_price_optuna_v4.py','tools/product_optuna_v4_adapter.py',
 'tools/product_modes.py','tools/upgrade_product_v4.py','docs/OPTUNA_V4_PIPELINE.md',
 'APPLY_V4.bat','APPLY_V4_QUICK.bat','APPLY_V4_DEEP.bat','SELECT_V4.bat','MODE_MENU.bat',
 'V4_RANDOM.bat','V4_GLOBAL.bat','V4_PER_SPLIT.bat','V4_HYBRID.bat')))


def upgrade(root,profile='standard'):
    root=Path(root).resolve();portable=root/'config/portable.json'
    if not portable.is_file() or not (root/'runtime/python38/python.exe').is_file():raise ValueError('请覆盖到已有离线包根目录，再运行APPLY_V4。')
    missing=[name for name in PATCH_FILES if not (root/name).is_file()]
    if missing:raise ValueError('补丁不完整：'+','.join(missing))
    provenance=json.loads((root/'services/price_service/optuna_v2/SOURCE_PROVENANCE.json').read_text(encoding='utf-8'))
    if hashlib.sha256((root/'services/price_service/optuna_v2/price_automl_core.py').read_bytes()).hexdigest()!=provenance['original_core_sha256']:raise ValueError('V2原始核心校验失败。')
    settings=json.loads(portable.read_text(encoding='utf-8-sig'));stamp=datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    configs={}
    for engine,load in (('optuna_v2',v2_config),('optuna_v3',v3_config)):
        path=root/('config/price_'+engine+'.json');configs[engine]=load(path) if path.is_file() else load()
    path=root/'config/price_optuna_v4.json'
    if path.is_file():cfg=v4_config(path)
    else:
        cfg=v4_config();source=configs.get(settings.get('price_engine'),configs['optuna_v3'])
        for key in ('expert','input','targets','deployment'):cfg[key]=copy.deepcopy(source[key])
        cfg['search']['n_trials']=max(300,source['search']['n_trials'])
    if profile=='quick':cfg['search']['n_trials']=40;cfg['v4']['splits']['max_splits']=12
    elif profile=='deep':cfg['search']['n_trials']=5000
    elif profile!='standard':raise ValueError('未知配置：'+profile)
    cfg=v4_config(overrides=cfg);configs['optuna_v4']=cfg
    for file in [portable]+[root/('config/price_'+engine+'.json') for engine in configs]:
        if file.is_file():file.with_name(file.stem+'.before_v4_'+stamp+file.suffix).write_bytes(file.read_bytes())
    for engine,value in configs.items():atomic_json(root/('config/price_'+engine+'.json'),value)
    # Register existing completed versions from their actual frozen config.
    from tools.product_optuna_versions import saved_pointer
    for engine in ('optuna_v2','optuna_v3','optuna_v4'):
        pointer=saved_pointer(root,engine)
        if pointer and (root/pointer['output']/'price/effective_config.json').is_file():register(root,pointer)
    select(root,'optuna_v4')
    manifest_path=root/'PACKAGE_MANIFEST.json'
    if manifest_path.is_file():
        manifest=json.loads(manifest_path.read_text(encoding='utf-8'));files={r['path']:r for r in manifest['immutable_files']}
        for name in PATCH_FILES:
            p=root/name;files[name]={'path':name,'size':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
        manifest.update(training_revision='Optuna_V4_switchable_1.0',immutable_files=sorted(files.values(),key=lambda r:r['path']));atomic_json(manifest_path,manifest)
    print('V4已安装，未训练、未改动旧模型。MODE_MENU切换全部模式；CHECK_INPUT检查；RETRAIN训练；START运行；EXPORT导出。',flush=True)
    return cfg


def main():
    p=argparse.ArgumentParser();p.add_argument('--profile',choices=['standard','quick','deep'],default='standard');a=p.parse_args()
    try:upgrade(ROOT,a.profile);return 0
    except Exception as exc:print('V4安装失败：'+str(exc),file=sys.stderr,flush=True);return 1
if __name__=='__main__':sys.exit(main())
