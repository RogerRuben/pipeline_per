# -*- coding: utf-8 -*-
import argparse,copy,datetime,hashlib,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.upgrade_product_v6 import PATCH_FILES as PREVIOUS_FILES
from tools.upgrade_product_optuna_v2 import atomic_json
from tools.product_price_optuna_v7 import load_config
from tools.product_modes import select
PATCH_FILES=tuple(dict.fromkeys(PREVIOUS_FILES+('tools/upgrade_product_v7.py','tools/product_compare_v7.py','docs/OPTUNA_V7_PIPELINE.md',
    'APPLY_V7.bat','APPLY_V7_QUICK.bat','APPLY_V7_DEEP.bat','SELECT_V7.bat','V7_BALANCED.bat','V7_GOAL_FIRST.bat','V7_BASELINE_ONLY.bat',
    'V7_EXPERT_OFF.bat','V7_EXPERT_REPORT.bat','V7_EXPERT_SOFT.bat','V7_EXPERT_STRICT.bat','COMPARE_V3_PLUS.bat')))

def upgrade(root,profile='standard'):
    root=Path(root).resolve();missing=[n for n in PATCH_FILES if not (root/n).is_file()]
    if missing:raise ValueError('V7补丁不完整：'+','.join(missing))
    settings=json.loads((root/'config/portable.json').read_text(encoding='utf-8-sig'));path=root/'config/price_optuna_v7.json'
    cfg=load_config(path if path.is_file() else None);stamp=datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    if path.is_file():atomic_json(root/('config/price_optuna_v7.before_'+stamp+'.json'),cfg)
    else:
        old=root/'config/price_optuna_v3.json'
        if old.is_file():
            raw=json.loads(old.read_text(encoding='utf-8-sig'))
            for k in ('search','validation','v3'):cfg[k]=copy.deepcopy(raw[k])
        active=root/('config/price_'+settings.get('price_engine','optuna_v6')+'.json')
        if active.is_file():
            raw=json.loads(active.read_text(encoding='utf-8-sig'))
            for k in ('expert','input','targets'):
                if k in raw:cfg[k]=copy.deepcopy(raw[k])
        cfg['input']['remove_outliers']=False;cfg['v3']['warm_start'].update(auto_history=False,history_file=None,config_file=None)
    if profile=='quick':cfg['search'].update(n_trials=40,timeout_seconds=300);cfg['v7']['budget'].update(max_extra_fits=128,extra_seconds=90)
    elif profile=='deep':cfg['search'].update(n_trials=5000,timeout_seconds=1800);cfg['v7']['budget'].update(max_extra_fits=1000,extra_seconds=300)
    elif profile!='standard':raise ValueError('未知V7预算。')
    cfg=load_config(overrides=cfg);atomic_json(path,cfg)
    mp=root/'PACKAGE_MANIFEST.json'
    if mp.is_file():
        manifest=json.loads(mp.read_text(encoding='utf-8'));entries={r['path']:r for r in manifest['immutable_files']}
        for name in PATCH_FILES:
            p=root/name;entries[name]={'path':name,'size':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
        manifest.update(training_revision='Optuna_V7_V3plus_1.0',immutable_files=sorted(entries.values(),key=lambda r:r['path']));atomic_json(mp,manifest)
    select(root,'optuna_v7');print('V7/V3+已启用；原输入、旧版本配置及保存模型未删除。专家五档约束默认关闭，CHECK_INPUT后显式RETRAIN。',flush=True)
    return cfg

def main():
    p=argparse.ArgumentParser();p.add_argument('--profile',choices=['standard','quick','deep'],default='standard');a=p.parse_args()
    try:upgrade(ROOT,a.profile);return 0
    except Exception as e:print('V7安装失败：'+str(e),file=sys.stderr);return 1
if __name__=='__main__':sys.exit(main())
