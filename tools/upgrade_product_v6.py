# -*- coding: utf-8 -*-
"""Install V6 without rewriting inputs, existing model files or old settings."""
import argparse,copy,datetime,hashlib,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.upgrade_product_v51 import PATCH_FILES as PREVIOUS_FILES
from tools.upgrade_product_optuna_v2 import atomic_json
from tools.product_price_optuna_v6 import load_config
from tools.product_modes import select
PATCH_FILES=tuple(dict.fromkeys(PREVIOUS_FILES+('tools/upgrade_product_v6.py','tools/product_patch_dependencies.py',
    'docs/OPTUNA_V6_PIPELINE.md','docs/RECOMMENDATION_SOURCE_PROVENANCE_V21_6.json','docs/COST_EFFECTIVENESS_ANALYSIS.md',
    'APPLY_V6.bat','APPLY_V6_QUICK.bat','APPLY_V6_DEEP.bat','SELECT_V6.bat','V6_GOAL_FIRST.bat','V6_BALANCED.bat','V6_ROBUST.bat','IMPORT_EFFECTIVENESS.bat')))

def upgrade(root,profile='standard'):
    root=Path(root).resolve()
    if not (root/'runtime/python38/python.exe').is_file():raise ValueError('请完整覆盖到原离线包根目录。')
    missing=[n for n in PATCH_FILES if not (root/n).is_file()]
    if missing:raise ValueError('V6补丁不完整：'+','.join(missing))
    cfgpath=root/'config/price_optuna_v6.json';cfg=load_config(cfgpath if cfgpath.is_file() else None)
    stamp=datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    if cfgpath.is_file():atomic_json(root/('config/price_optuna_v6.before_'+stamp+'.json'),cfg)
    else:
        for version in (5,4,3,2):
            old=root/('config/price_optuna_v%d.json'%version)
            if old.is_file():
                raw=json.loads(old.read_text(encoding='utf-8-sig'))
                for key in ('expert','input','targets'):
                    if key in raw:cfg[key]=copy.deepcopy(raw[key])
                cfg['input']['remove_outliers']=False
                break
    if profile=='quick':cfg['search'].update(n_trials=12,timeout_seconds=300);cfg['v6']['budget']['max_member_fits']=700
    elif profile=='deep':cfg['search'].update(n_trials=120,timeout_seconds=1800);cfg['v6']['budget']['max_member_fits']=6000
    elif profile!='standard':raise ValueError('未知V6预算。')
    cfg=load_config(overrides=cfg);atomic_json(cfgpath,cfg)
    mp=root/'PACKAGE_MANIFEST.json'
    if mp.is_file():
        manifest=json.loads(mp.read_text(encoding='utf-8'));rows={r['path']:r for r in manifest['immutable_files']}
        for name in PATCH_FILES:
            path=root/name;rows[name]={'path':name,'size':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        manifest.update(training_revision='Optuna_V6_sealed_1.0',recommendation_version='V21.6',immutable_files=sorted(rows.values(),key=lambda r:r['path']))
        atomic_json(mp,manifest)
    select(root,'optuna_v6')
    print('V6独立主线及V21.6前台已启用；旧模式、模型与输入保留。CHECK_INPUT后显式RETRAIN，START只读保存结果。',flush=True)
    return cfg

def main():
    p=argparse.ArgumentParser();p.add_argument('--profile',choices=['standard','quick','deep'],default='standard');a=p.parse_args()
    try:upgrade(ROOT,a.profile);return 0
    except Exception as exc:print('V6安装失败：'+str(exc),file=sys.stderr,flush=True);return 1

if __name__=='__main__':sys.exit(main())
