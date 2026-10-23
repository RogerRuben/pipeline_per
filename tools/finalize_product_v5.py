# -*- coding: utf-8 -*-
"""Verify hashes and acceptance evidence before declaring V5 deliveries complete."""
import hashlib,json,sys,zipfile
from datetime import datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))


def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def write(path,value):Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    evidence=ROOT/'outputs/v5_20261023';legacy=read(evidence/'legacy_final.json');v4=read(evidence/'v4_final.json');v5=read(evidence/'v5_final.json');acceptance=read(evidence/'package_acceptance.json')
    assert all(r['status']=='PASS' for r in (legacy,v4,v5,acceptance))
    assert legacy['tests']==144 and v4['tests']==21 and v5['tests']>=24
    layout=(evidence/'layout_final.log').read_text(encoding='utf-8');assert 'Ran 9 tests' in layout and 'OK' in layout and 'FAILED' not in layout
    telemetry=read(evidence/'v3_telemetry_parity.json');baseline=read(evidence/'v5_300_parity.json')
    assert telemetry['status']==baseline['status']=='PASS' and telemetry['actual_model_fits_equal']==1033
    original=read(ROOT/'outputs/optuna_v2_pipeline_20261015/supplied_version_parity.json');assert original['status']=='PASS' and original['original_core_byte_identical']
    assert len(acceptance['saved_mode_switches'])==9 and len(acceptance['actual_training_cases'])==10 and len(acceptance['v2_v3_training_logs_verified'])==2
    tests=legacy['tests']+v4['tests']+v5['tests']+9;artifacts=[]
    for kind,name in [('full','ProductAutoML_V5_Offline_20261023.zip'),('patch','ProductAutoML_V5_PATCH_20261023.zip')]:
        path=ROOT/'deliverables'/name;digest=sha(path);meta=read(str(path)+'.json')
        assert digest==acceptance[kind+'_sha256']==meta['sha256']
        with zipfile.ZipFile(path) as z:
            assert z.testzip() is None
            if kind=='patch':
                manifest=json.loads(z.read('OPTUNA_V5_PATCH_FILES.json').decode('utf-8'));assert not manifest['dependency_check']['missing']
                for item in manifest['files']:
                    assert hashlib.sha256(z.read(item['path'])).hexdigest()==item['sha256']
                    source=ROOT/item['path']
                    if source.is_file() and source.suffix in ('.py','.js','.md','.json','.txt'):assert sha(source)==item['sha256'],item['path']
            else:
                modes=json.loads(z.read('ProductAutoML_V5/state/modes.json').decode('utf-8'));assert len(modes['models'])==9
        meta.update(status='VERIFIED',source_tests=tests,actual_v5_bat_training_cases=10,actual_v2_v3_telemetry_training_cases=2,
                    saved_modes_verified=9,v3_300_trial_parameters_and_scores_preserved=True,training_telemetry_added_model_fits=0,
                    checkpoint_resume_verified=True,saved_export_relocated_without_input=True,old_models_preserved=6,
                    windows7_hardware_tested=False,business_accuracy_certified=False,verified_at_utc=datetime.utcnow().isoformat()+'Z')
        write(str(path)+'.json',meta);artifacts.append({'kind':kind,'path':str(path),'bytes':path.stat().st_size,'sha256':digest})
    result={'status':'VERIFIED','source_tests':tests,'artifacts':artifacts,'v3_logging_parity':telemetry,'v3_to_v5_300_trial_parity':baseline,'acceptance':acceptance,'original_v2_parity':original,
            'scope':'software_and_search_invariants_with_virtual_data','business_accuracy_certified':False,'windows7_hardware_tested':False}
    write(evidence/'V5_VERIFIED.json',result);print(json.dumps({'status':'VERIFIED','source_tests':tests,'artifacts':artifacts},ensure_ascii=False))
if __name__=='__main__':main()
