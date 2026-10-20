# -*- coding: utf-8 -*-
"""Publish verification metadata only after the exact ZIP digests have passed."""
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def read(path):return json.loads(path.read_text(encoding='utf-8'))
def write(path,data):path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    evidence=ROOT/'outputs/optuna_dual_20261016';units=read(evidence/'unit_tests.json')
    assert units['status']=='PASS' and units['tests']==133 and not units['errors'] and not units['failures']
    fidelity=ROOT/'outputs/optuna_v2_pipeline_20261015/supplied_version_parity.json'
    assert read(fidelity)['status']=='PASS' and read(fidelity)['original_core_byte_identical']
    shutil.copy2(str(fidelity),str(evidence/'supplied_version_parity.json'))
    deliveries=[]
    for file,report in (('ProductAutoML_V2_V3_Offline_20261016.zip','full_acceptance.json'),('ProductAutoML_V2_V3_PATCH_20261016.zip','patch_acceptance.json')):
        path=ROOT/'deliverables'/file;result=read(evidence/report)
        assert result['status']=='PASS' and result['zip_sha256']==sha(path)
        with zipfile.ZipFile(str(path)) as archive:
            assert archive.testzip() is None
            if report=='full_acceptance.json':
                assert len([name for name in archive.namelist() if name.endswith('/runtime/python38/python.exe')])==1
                assert not any('runtime/tabpfn311' in name or '/node_modules/' in name for name in archive.namelist())
        metadata=read(Path(str(path)+'.json'));metadata.update(status='VERIFIED',acceptance_report=str((evidence/report).relative_to(ROOT)),unit_tests=133)
        write(Path(str(path)+'.json'),metadata);deliveries.append(metadata)
    result={'status':'PASS','unit_tests':units,'deliveries':deliveries,'v2_source_parity':read(evidence/'supplied_version_parity.json'),
        'mixed_feature_virtual_data':'16 records, 36 original attributes; 1-5 counts, 20-80 magnitudes, 1800-7200 magnitudes, text and numeric-code configurations',
        'ten_records_forty_features_test':True,'configuration_tokens_preserved':['001','1','IP65','IP67'],
        'offline_engine_selection_and_retraining':True,'fair_comparison_entrypoint':True,'saved_start_does_not_retrain':True,
        'exported_bundle_relocated_without_training_table':True,'shared_environment':'Python3.8.10 x64 / sklearn0.24.2 / Optuna3.6.0',
        'v3_source_scope':'Project implementation of uploaded V3 notebook design; companion V3 Python module was absent.',
        'business_accuracy_certified':False,'win7_physical_machine_tested_in_this_session':False}
    write(evidence/'OPTUNA_V2_V3_VERIFIED.json',result)
    print(json.dumps({'status':'PASS','tests':133,'zip_files':[x['path'] for x in deliveries]},ensure_ascii=False))
if __name__=='__main__':main()
