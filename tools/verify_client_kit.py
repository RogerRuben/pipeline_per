"""Extract the actual client ZIP and exercise its BAT entry points offline."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from product.common import read_json,write_json,sha


def verify(archive,out):
    archive,out=Path(archive).resolve(),Path(out).resolve()
    if out.exists() or ROOT not in out.parents or out.parts[len(ROOT.parts)]!='outputs':raise ValueError('Choose new project outputs')
    out.mkdir(parents=True);temporary=Path(tempfile.mkdtemp(prefix='甲方 开发包_'))
    report={'checks':{},'archive_sha256':sha(archive),'extracted_to':str(temporary),
            'host_scope':'Actual Windows host; Win7 hardware unavailable','win7_machine_validated':False}
    def check(key,value):
        report['checks'][key]=bool(value);write_json(out/'kit_acceptance.json',report)
        if not value:raise AssertionError(key)
    try:
        with zipfile.ZipFile(str(archive)) as bundle:
            for item in bundle.infolist():
                target=(temporary/item.filename).resolve()
                if temporary not in target.parents:raise ValueError('Unsafe distribution ZIP path')
            bundle.extractall(temporary)
        folders=[path for path in temporary.iterdir() if path.is_dir()]
        if len(folders)!=1:raise ValueError('Expected one kit folder')
        kit=folders[0];report['kit_root']=str(kit)
        check('no_git_checkout_in_distribution',not (kit/'project/.git').exists())
        env=dict(os.environ,PATH='',PRODUCT_NO_PAUSE='1')
        for key in list(env):
            if key.startswith(('PYTHON','IPDEMO_','_PYI','_MEIPASS')):env.pop(key,None)
        cmd=Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/cmd.exe'
        def run(batch,success=True):
            command=[str(cmd),'/d','/c','call',str(kit/batch)]
            result=subprocess.run(command,cwd=str(kit),env=env,capture_output=True,timeout=1500)
            (out/(batch+'.log')).write_bytes(result.stdout+result.stderr)
            if success and result.returncode:
                raise RuntimeError(batch+' failed; see '+str(out/(batch+'.log')))
            return result
        run('CHECK_ENVIRONMENT.bat')
        check('bat_environment_check_with_empty_path',True)
        run('CHECK_INPUT.bat')
        check('bat_input_check',True)
        run('SELF_TEST.bat')
        success=read_json(kit/'LAST_SUCCESS.json')
        check('full_bat_train_verify_package_pass',success['status']=='pass' and success['engine']=='optuna_v3')
        train=Path(success['release']);model_manifest=read_json(train/'prototype_manifest.json')
        training=read_json(train/'price/training_report.json')
        api=read_json(Path(success['job'])/'api_validation/acceptance.local.json')
        smoke=read_json(Path(success['job'])/'delivery_smoke.json')
        report.update(training_trials_completed=training['trials_completed'],
                      trained_model_sha256=model_manifest['artifact_sha256']['price_model'],
                      generated_delivery_zip=success['delivery_zip'],field_count=api['field_count'],
                      runtime_executable=str(kit/'runtime/python38/python.exe'))
        check('actual_v3_training_40_trials',training['engine']=='optuna_v3' and training['trials_completed']==40)
        check('http_consistency_checks',api['status']=='pass' and api['checks']['price_consistency_max_delta_wan']<1e-8)
        check('generated_native_exe_starts_and_predicts',smoke['status']=='pass' and max(smoke['prices_wan'])-min(smoke['prices_wan'])<1e-8)
        before={path.relative_to(train).as_posix():sha(path) for path in train.rglob('*') if path.is_file()}
        first_zip=success['delivery_zip'];run('REPACK_LAST.bat');repacked=read_json(kit/'LAST_SUCCESS.json')
        after={path.relative_to(train).as_posix():sha(path) for path in train.rglob('*') if path.is_file()}
        check('repack_does_not_retrain_or_modify_model',before==after and first_zip!=repacked['delivery_zip'])
        project=kit/'settings/project.json';original=project.read_bytes();settings=read_json(project)
        count=len(list((kit/'deliverables').glob('*.zip')))
        settings['data_kind']='customer';write_json(project,settings)
        failed=run('TRAIN_AND_PACKAGE.bat',False)
        check('sample_cannot_be_mislabeled_customer_data',failed.returncode!=0 and
              len(list((kit/'deliverables').glob('*.zip')))==count and read_json(kit/'LAST_SUCCESS.json')==repacked)
        project.write_bytes(original)
        code=kit/'project/app/static/portal.js';original_code=code.read_bytes();code.write_bytes(original_code+b'\n')
        failed=run('CHECK_ENVIRONMENT.bat',False)
        check('modified_immutable_source_rejected',failed.returncode!=0)
        code.write_bytes(original_code)
        run('CHECK_ENVIRONMENT.bat')
        check('kit_still_valid_after_mutable_job_outputs',True)
        for name,source in [('training_report.json',train/'price/training_report.json'),
                            ('native_api_validation.json',Path(success['job'])/'api_validation/acceptance.local.json'),
                            ('delivery_smoke.json',Path(success['job'])/'delivery_smoke.json')]:
            (out/name).write_bytes(source.read_bytes())
        report.update(status='pass');write_json(out/'kit_acceptance.json',report);return report
    except Exception as exc:
        report.update(status='fail',error=str(exc));write_json(out/'kit_acceptance.json',report);raise


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--archive',required=True);p.add_argument('--out',required=True);a=p.parse_args()
    print(json.dumps(verify(a.archive,a.out),ensure_ascii=True,indent=2))
