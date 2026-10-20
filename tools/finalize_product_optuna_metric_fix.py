# -*- coding: utf-8 -*-
import hashlib
import json
import shutil
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def main():
    evidence=ROOT/'outputs/optuna_v3_metric_fix_20261016';archive=ROOT/'deliverables/ProductAutoML_V2_V3_MetricFix_PATCH_20261016.zip'
    read=lambda p:json.loads(p.read_text(encoding='utf-8'))
    units=read(evidence/'unit_tests.json');acceptance=read(evidence/'isolated_acceptance.json');normal=read(evidence/'normal_300_parity.json')
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    assert units['status']=='PASS' and units['tests']==144 and acceptance['status']=='PASS' and acceptance['patch_sha256']==digest and normal['status']=='PASS'
    assert acceptance['real_nonfinite_candidate_recovery']['failed_candidates']>0 and acceptance['real_nonfinite_candidate_recovery']['export_completed']
    provenance=ROOT/'outputs/optuna_v2_pipeline_20261015/supplied_version_parity.json';assert read(provenance)['status']=='PASS'
    shutil.copy2(str(provenance),str(evidence/'v2_source_parity.json'))
    metadata=read(Path(str(archive)+'.json'));metadata.update(status='VERIFIED',unit_tests=144,normal_v3_regression_trials=300,
        finite_input_nonfinite_candidate_reproduced=True,sampler_and_cache_keyerror_fault_injection_tested=True,
        original_user_keyerror_stack_available=False,acceptance_report='outputs/optuna_v3_metric_fix_20261016/isolated_acceptance.json')
    Path(str(archive)+'.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    result={'status':'PASS','patch':metadata,'unit_tests':units,'normal_300_trial_regression':normal,
        'real_invalid_candidate_recovery':acceptance['real_nonfinite_candidate_recovery'],'v2_source_parity':read(provenance),
        'four_to_seven_records_40_features_both_engines':True,'saved_runtime_export_relocated_without_input':True,
        'observed_failure':'Valid finite inputs can yield unstable model predictions and nonfinite scoring; previous V3 passed synthetic penalty observations to TPE.',
        'scope_limit':'The user could not provide the original KeyError stack. TPE and cache recoveries were fault-injection tested; its exact original call site is unconfirmed.',
        'business_accuracy_certified':False}
    (evidence/'METRIC_FIX_VERIFIED.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':'VERIFIED','tests':144,'normal_trials':300,'patch':str(archive),'sha256':digest},ensure_ascii=False))
if __name__=='__main__':main()
