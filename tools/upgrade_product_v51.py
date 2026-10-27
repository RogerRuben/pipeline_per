# -*- coding: utf-8 -*-
"""Explicitly migrate editable V5 settings; preserve every saved model and input."""
import argparse
import datetime
import hashlib
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.upgrade_product_v5 import PATCH_FILES as V5_FILES,upgrade as upgrade_v5
from tools.product_price_optuna_v5 import load_config
from tools.product_modes import select
from tools.upgrade_product_optuna_v2 import atomic_json

PATCH_FILES=tuple(dict.fromkeys(V5_FILES+('tools/upgrade_product_v51.py','docs/OPTUNA_V5_1_PIPELINE.md',
    'APPLY_V5_1.bat','APPLY_V5_1_QUICK.bat','APPLY_V5_1_DEEP.bat','V5_FIXED_CV.bat','V5_LEGACY.bat')))


def upgrade(root,profile='standard'):
    root=Path(root).resolve()
    missing=[name for name in PATCH_FILES if not (root/name).is_file()]
    if missing:raise ValueError('V5.1补丁不完整：'+','.join(missing))
    cfg_path=root/'config/price_optuna_v5.json'
    raw=json.loads(cfg_path.read_text(encoding='utf-8-sig')) if cfg_path.is_file() else None
    stamp=datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    if raw is not None:atomic_json(root/('config/price_optuna_v5.before_v5_1_'+stamp+'.json'),raw)
    cfg=upgrade_v5(root,'standard')
    migrating=raw is None or raw.get('v5',{}).get('protocol','legacy_split')!='fixed_cv'
    cfg['v5']['protocol']='fixed_cv';cfg['v5']['enhancement']['split_anchors']=1
    if migrating:
        cfg['v5']['weight_policy']='baseline_fixed';cfg['deployment']['model_scope']='full_data'
        cfg['v5']['enhancement']['max_new_fits']=60
    if profile=='quick':
        cfg['search']['n_trials']=40;cfg['v5']['enhancement']['max_new_fits']=20
    elif profile=='deep':
        cfg['search'].update(n_trials=300,timeout_seconds=1800)
        cfg['v5']['budget']['max_member_fits']=5000;cfg['v5']['enhancement']['max_new_fits']=5000
    elif profile!='standard':raise ValueError('未知V5.1预算。')
    cfg=load_config(overrides=cfg);atomic_json(cfg_path,cfg)
    manifest_path=root/'PACKAGE_MANIFEST.json'
    if manifest_path.is_file():
        manifest=json.loads(manifest_path.read_text(encoding='utf-8'));entries={r['path']:r for r in manifest['immutable_files']}
        for name in PATCH_FILES:
            path=root/name;entries[name]={'path':name,'size':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        manifest.update(training_revision='Optuna_V5_1_fixed_cv_1.0',immutable_files=sorted(entries.values(),key=lambda r:r['path']))
        atomic_json(manifest_path,manifest)
    select(root,'optuna_v5')
    print('V5.1已启用：固定覆盖全部样本的CV，新增阶段冻结权重；旧模型和输入保留。CHECK_INPUT后RETRAIN，START不重训。',flush=True)
    return cfg


def main():
    p=argparse.ArgumentParser();p.add_argument('--profile',choices=['standard','quick','deep'],default='standard');a=p.parse_args()
    try:upgrade(ROOT,a.profile);return 0
    except Exception as exc:print('V5.1安装失败：'+str(exc),file=sys.stderr,flush=True);return 1


if __name__=='__main__':sys.exit(main())
