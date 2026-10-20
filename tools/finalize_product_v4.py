# -*- coding: utf-8 -*-
"""Mark only checksum-matched, exercised V4 artifacts as verified."""
import hashlib
import json
import sys
import zipfile
from datetime import datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))


def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def write(path,value):Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    evidence=ROOT/'outputs/optuna_v4_20261016';unit=read(evidence/'v4_tests.json');legacy=read(evidence/'legacy_tests.json');accepted=read(evidence/'package_acceptance.json')
    assert unit['status']==legacy['status']==accepted['status']=='PASS' and unit['tests']>=21 and legacy['tests']==144
    assert len(accepted['actual_bat_training_cases'])==8 and len(accepted['full_saved_mode_switches'])==6
    parity=read(ROOT/'outputs/optuna_v2_pipeline_20261015/supplied_version_parity.json');assert parity['status']=='PASS' and parity['original_core_byte_identical']
    paths={'patch':ROOT/'deliverables/ProductAutoML_V4_PATCH_20261016.zip','full':ROOT/'deliverables/ProductAutoML_V4_Offline_20261016.zip'}
    artifacts=[]
    for kind,path in paths.items():
        digest=sha(path);assert digest==accepted[kind+'_sha256'];metadata=read(str(path)+'.json');assert digest==metadata['sha256']
        with zipfile.ZipFile(str(path)) as archive:
            assert archive.testzip() is None
            if kind=='patch':
                manifest=json.loads(archive.read('OPTUNA_V4_PATCH_FILES.json').decode('utf-8'));assert not manifest['dependency_check']['missing']
                for item in manifest['files']:
                    assert hashlib.sha256(archive.read(item['path'])).hexdigest()==item['sha256']
                    source=ROOT/item['path']
                    if source.is_file() and source.suffix in ('.py','.js','.md','.json'):assert sha(source)==item['sha256'],item['path']
            else:
                assert any('/runtime/python38/python.exe' in name for name in archive.namelist())
                registry=json.loads(archive.read('ProductAutoML_V4/state/modes.json').decode('utf-8'));assert len(registry['models'])==6
        metadata.update(status='VERIFIED',verification_scope='software_pipeline_and_packaging_not_business_accuracy',
                        source_unit_tests=unit['tests']+legacy['tests'],actual_training_cases=len(accepted['actual_bat_training_cases']),
                        saved_modes_verified=6,saved_export_relocated_without_input=True,old_models_preserved=True,
                        real_windows7_machine_tested=False,business_accuracy_certified=False,
                        verified_at_utc=datetime.utcnow().isoformat()+'Z',evidence='outputs/optuna_v4_20261016/package_acceptance.json')
        write(str(path)+'.json',metadata);artifacts.append({'kind':kind,'path':str(path),'bytes':path.stat().st_size,'sha256':digest})
    result={'status':'VERIFIED','unit_tests':unit['tests']+legacy['tests'],'old_v2_core_and_objective_parity':parity,
            'artifacts':artifacts,'acceptance':accepted,'compatibility_environment':'Python3.8.10 x64 / sklearn0.24.2 / Optuna3.6.0',
            'real_windows7_machine_tested':False,'business_accuracy_certified':False}
    write(evidence/'V4_VERIFIED.json',result);print(json.dumps({'status':'VERIFIED','unit_tests':result['unit_tests'],'artifacts':artifacts},ensure_ascii=False))
if __name__=='__main__':main()
