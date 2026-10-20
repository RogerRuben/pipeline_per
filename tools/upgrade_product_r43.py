# -*- coding: utf-8 -*-
"""Install a policy without training, deleting data or changing the saved model."""
import argparse
import copy
import datetime
import hashlib
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.product_price_automl_r4 import load_config
from tools.product_r43_profiles import profile

PATCH_FILES=(
 'tools/product_price_automl_r4.py','tools/product_r42_search.py','tools/product_r42_profiles.py',
 'tools/product_r43_search.py','tools/product_r43_profiles.py','tools/product_r43_diagnostics.py',
 'tools/product_r4_input.py','tools/price_attribute_input.py','tools/upgrade_product_r43.py',
 'services/price_service/r4_estimators.py','services/price_service/r43_support.py',
 'services/price_service/native_bundle.py','services/price_service/app.py',
 'app/model_service_client.py','app/recommender.py','app/local_generator.py',
 'app/gflownet_generator.py','app/store.py','app/static/price_ensemble.js',
 'APPLY_R4_3_PATCH.bat','APPLY_R4_3_QUICK.bat','APPLY_R4_3_EXPERT_COMPARE.bat','APPLY_R4_3_LOO_AUDIT.bat','APPLY_R4_3_INPUT_FIX.bat')


def refresh_manifest(target,stamp):
    mp=target/'PACKAGE_MANIFEST.json'
    if not mp.is_file():return
    (target/'config'/('PACKAGE_MANIFEST.before_r431_'+stamp+'.json')).write_bytes(mp.read_bytes())
    manifest=json.loads(mp.read_text(encoding='utf-8'));entries={r['path']:r for r in manifest['immutable_files']}
    for rel in PATCH_FILES:
        file=target/rel
        if file.is_file():entries[rel]={'path':rel,'size':file.stat().st_size,'sha256':hashlib.sha256(file.read_bytes()).hexdigest()}
    manifest['immutable_files']=sorted(entries.values(),key=lambda r:r['path']);manifest['training_revision']='R4_3_1_input_dependency_fix'
    pending=mp.with_suffix('.json.tmp');pending.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');pending.replace(mp)


def upgrade(target,name='data_only',repair_only=False):
    target=Path(target).resolve();path=target/'config/price_automl_r4.json'
    if not path.is_file() or not (target/'config/portable.json').is_file():
        raise ValueError('请把PATCH解压覆盖至已有R4/R4.1/R4.2完整目录。')
    stamp=datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    old=load_config(path)
    if repair_only:
        missing=[rel for rel in PATCH_FILES if rel.endswith(('.py','.js')) and not (target/rel).is_file()]
        if missing:raise ValueError('补丁文件不完整，请完整解压覆盖原目录；缺少：'+', '.join(missing))
        refresh_manifest(target,stamp)
        print('R4.3.1输入依赖修复完成；当前训练配置、表格、模型与运行版本均保留。',flush=True)
        print('先运行CHECK_INPUT.bat检查表格，再运行RETRAIN.bat继续训练；无需重装环境。',flush=True)
        return old
    new=profile(name)
    new['quality']=copy.deepcopy(old['quality']);new['missing_tokens']=copy.deepcopy(old['missing_tokens'])
    enabled=new['expert']['enabled'];new['expert']=copy.deepcopy(old['expert']);new['expert']['enabled']=enabled
    new['resources']['cache_directory']=old['resources']['cache_directory']
    load_config(overrides=new)
    path.with_name('price_automl_r4.before_r43_'+stamp+'.json').write_bytes(path.read_bytes())
    pending=path.with_suffix('.json.tmp');pending.write_text(json.dumps(new,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');pending.replace(path)
    refresh_manifest(target,stamp)
    print('R4.3配置已应用：%s；专家价格评分%s。'%(name,'参与候选比较' if enabled else '保留但不参与训练'),flush=True)
    print('原精度门槛、1—5分评分、数据与旧模型保留。运行RETRAIN.bat启用新训练；START.bat仍加载保存模型。',flush=True)
    return new


def main():
    p=argparse.ArgumentParser();p.add_argument('--target',default=str(ROOT));p.add_argument('--profile',default='data_only',choices=['data_only','quick','expert_compare','loo_audit']);p.add_argument('--repair-only',action='store_true');a=p.parse_args()
    try:upgrade(a.target,a.profile,a.repair_only)
    except Exception as exc:print('R4.3配置失败：'+str(exc),flush=True);return 1
    return 0
if __name__=='__main__':sys.exit(main())
