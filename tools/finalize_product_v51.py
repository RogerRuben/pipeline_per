# -*- coding: utf-8 -*-
"""Verify acceptance evidence and current payload bytes before releasing V5.1."""
import hashlib,json,sys,zipfile
from datetime import datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))


def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def write(path,value):Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def main():
    evidence=ROOT/'outputs/v51_20261027'
    expected={'legacy':144,'v4':21,'v5_legacy_final':24,'v51_final':25}
    reports={name:read(evidence/(name+'.json')) for name in expected}
    for name,count in expected.items():
        assert reports[name]['status']=='PASS' and reports[name]['tests']==count
    layout=(evidence/'layout_final.log').read_text(encoding='utf-8');assert 'Ran 9 tests' in layout and 'OK' in layout and 'FAILED' not in layout
    tests=sum(expected.values())+9
    acceptance=read(evidence/'package_acceptance.json');parity=read(evidence/'parity_and_root_cause.json')
    assert acceptance['status']==parity['status']=='PASS'
    assert len(acceptance['saved_modes'])==12 and len(acceptance['actual_training_cases'])==10 and len(acceptance['v2_v3_logs'])==2
    assert acceptance['old_models_preserved']==9 and acceptance['weights_frozen_and_all_rows_cross_validated']
    assert parity['v3_trials']==300 and parity['all_original_parameters_and_valid_scores_equal'] and parity['selected_parameters_equal']
    core='f8f9877029e13535b97b40c429fb4afe6b5d613db635b6bee83307fca30b2899'
    assert sha(ROOT/'services/price_service/optuna_v2/price_automl_core.py')==core==parity['core_sha256']
    with zipfile.ZipFile(ROOT/'outputs/v51_saved_acceptance.zip') as z:
        assert z.testzip() is None and not any('/input/' in name for name in z.namelist())
    artifacts=[]
    for kind,name in [('full','ProductAutoML_V5_1_Offline_20261027.zip'),('patch','ProductAutoML_V5_1_PATCH_20261027.zip')]:
        path=ROOT/'deliverables'/name;meta=read(str(path)+'.json');digest=sha(path)
        assert digest==meta['sha256']==acceptance[kind+'_sha256']
        with zipfile.ZipFile(path) as z:
            assert z.testzip() is None
            if kind=='patch':
                manifest=json.loads(z.read('OPTUNA_V5_1_PATCH_FILES.json').decode('utf-8'));assert not manifest['dependency_check']['missing']
                for entry in manifest['files']:
                    assert hashlib.sha256(z.read(entry['path'])).hexdigest()==entry['sha256']
                    source=ROOT/entry['path']
                    if source.is_file() and source.suffix in ('.py','.js','.md','.json','.txt'):assert sha(source)==entry['sha256'],entry['path']
            else:
                registry=json.loads(z.read('ProductAutoML_V5_1/state/modes.json').decode('utf-8'));assert len(registry['models'])==12
        meta.update(status='VERIFIED',source_tests=tests,actual_v51_bat_training_cases=10,v2_v3_telemetry_bat_cases=2,
                    saved_modes_verified=12,old_models_preserved=9,weights_fitted_on_validation=False,
                    windows7_hardware_tested=False,business_accuracy_certified=False,
                    verified_at_utc=datetime.utcnow().isoformat()+'Z')
        write(str(path)+'.json',meta);artifacts.append({'kind':kind,'path':str(path),'bytes':path.stat().st_size,'sha256':digest})
    receipt={'status':'VERIFIED','source_tests':tests,'artifacts':artifacts,'reports':reports,'parity_and_root_cause':parity,
             'acceptance':acceptance,'business_accuracy_certified':False,'windows7_hardware_tested':False}
    write(evidence/'V5_1_VERIFIED.json',receipt)
    print(json.dumps({'status':'VERIFIED','source_tests':tests,'artifacts':artifacts},ensure_ascii=False))


if __name__=='__main__':main()
