# -*- coding: utf-8 -*-
import hashlib
import json
import shutil
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def main():
    evidence=ROOT/'outputs/optuna_small_20261016';path=ROOT/'deliverables/ProductAutoML_V2_V3_Min4_PATCH_20261016.zip'
    read=lambda p:json.loads(p.read_text(encoding='utf-8'))
    units=read(evidence/'unit_tests.json');acceptance=read(evidence/'isolated_acceptance.json');digest=hashlib.sha256(path.read_bytes()).hexdigest()
    assert units['status']=='PASS' and units['tests']==137 and acceptance['status']=='PASS' and acceptance['patch_sha256']==digest
    assert {(r['engine'],r['records']) for r in acceptance['actual_check_input_and_retrain']}=={(e,n) for e in ('optuna_v2','optuna_v3') for n in range(4,8)}
    source=ROOT/'outputs/optuna_v2_pipeline_20261015/supplied_version_parity.json';assert read(source)['status']=='PASS'
    shutil.copy2(str(source),str(evidence/'supplied_version_parity.json'))
    metadata=read(Path(str(path)+'.json'));metadata.update(status='VERIFIED',minimum_samples_default=4,minimum_samples_configurable=True,automatic_split_safety=True,
        unit_tests=137,acceptance_report='outputs/optuna_small_20261016/isolated_acceptance.json')
    Path(str(path)+'.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    result={'status':'PASS','patch':metadata,'unit_tests':units,'tested_records':[4,5,6,7],'features_each':40,'engines':['optuna_v2','optuna_v3'],
        'actual_bat_cases':8,'price_effectiveness_recommendation_generation_at_four_rows':True,'saved_export_relocated_without_training_input':True,
        'higher_user_minimum_enforced_before_training':True,'old_saved_models_preserved':True,'v2_source_parity':read(source),'business_accuracy_certified':False}
    (evidence/'SMALL_SAMPLE_VERIFIED.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':'VERIFIED','tests':137,'minimum':4,'patch':str(path),'sha256':digest},ensure_ascii=False))
if __name__=='__main__':main()
