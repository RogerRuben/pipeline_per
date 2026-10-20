# -*- coding: utf-8 -*-
"""Exercise real BATs in isolated offline ZIPs, with no source-directory fallback."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.verify_product_r4_portable import exercise


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    p=argparse.ArgumentParser();p.add_argument('--zip',required=True);p.add_argument('--destination',required=True);p.add_argument('--report',required=True);p.add_argument('--patch',action='store_true');a=p.parse_args()
    destination=Path(a.destination).resolve();report=Path(a.report).resolve();report.parent.mkdir(parents=True,exist_ok=True)
    if ROOT/'outputs' not in destination.parents or destination.exists():raise ValueError('Choose a new short outputs path')
    if a.patch:
        original=ROOT/'deliverables/product_r41_portable/ProductAutoML_R4_1_20261013'
        def copier(src,dst):
            if Path(src).relative_to(original).parts[0]=='runtime':os.link(str(src),str(dst));return dst
            return shutil.copy2(src,dst)
        shutil.copytree(str(original),str(destination),copy_function=copier,ignore=shutil.ignore_patterns('__pycache__','cache','oof_cache','exports'))
        with zipfile.ZipFile(str(ROOT/'deliverables/ProductOptunaV2_Full_Offline_20261015.zip')) as z:
            for name in z.namelist():
                relative='/'.join(name.split('/')[1:])
                if relative and not relative.startswith('runtime/'):
                    path=destination/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(z.read(name))
        package=destination
    else:
        with zipfile.ZipFile(a.zip) as z:assert z.testzip() is None;z.extractall(str(destination))
        package=destination/'ProductAutoML_V2_V3'
    env=dict(os.environ,PRODUCT_NO_PAUSE='1',PYTHONIOENCODING='utf-8',PYTHONPATH='');python=package/'runtime/python38/python.exe';events={}
    def run(args,label,timeout=240):
        result=subprocess.run(args,cwd=str(package),env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=timeout,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        text=result.stdout.decode('utf-8',errors='replace');report.with_name(report.stem+'_'+label+'.log').write_text(text,encoding='utf-8')
        if result.returncode:raise AssertionError(label+': '+text)
        return text
    def bat(name,label,*args):return run(['cmd.exe','/d','/c',name]+list(args),label)
    def snapshot():
        pointer=json.loads((package/'state/latest.json').read_text(encoding='utf-8'));output=package/pointer['output'];manifest=json.loads((output/'prototype_manifest.json').read_text(encoding='utf-8'))
        return pointer,output,{key:(sha(output/manifest[key]),(output/manifest[key]).stat().st_mtime_ns) for key in ('price_model','effectiveness_model','business_package')}
    if a.patch:
        before,old_output,old_hashes=snapshot()
        with zipfile.ZipFile(a.zip) as z:
            payload=json.loads(z.read('OPTUNA_DUAL_PATCH_FILES.json').decode('utf-8'))
            for item in payload['files']:assert hashlib.sha256(z.read(item['path'])).hexdigest()==item['sha256']
            z.extractall(str(package))
        bat('APPLY_V2_V3.bat','patch_01_install','--engine','optuna_v2')
        assert old_hashes==snapshot()[2]
        events['old_saved_v2_after_patch']=exercise(package,report.with_name(report.stem+'_patch_02_old_saved_start.log'),generation=False,expected_rows=16,expected_features=36,change_field='x0',change_delta=.05)
        (package/'input/products.xlsx').write_bytes((ROOT/'outputs/optuna_dual_20261016/products.xlsx').read_bytes())
        settings_path=package/'config/portable.json';settings=json.loads(settings_path.read_text(encoding='utf-8'));settings['demo_input_sha256']=sha(package/'input/products.xlsx');settings_path.write_text(json.dumps(settings,ensure_ascii=False,indent=2),encoding='utf-8')
    run([str(python),'-X','utf8','tools/product_portable.py','verify','--full'],'01_manifest')
    bat('CHECK_INPUT.bat','02_check_input')
    if not a.patch:
        assert not json.loads((package/'config/price_optuna_v2.json').read_text(encoding='utf-8'))['expert']['scores']
        for short in ('V2','V3'):
            bat('SELECT_'+short+'.bat','03_select_demo_'+short)
            events['saved_demo_'+short]=exercise(package,report.with_name(report.stem+'_04_saved_demo_'+short+'.log'),generation=(short=='V3'),expected_rows=16,expected_features=36,change_field='x1',change_delta=1.)
    preserved={}
    for short,count in (('v2',3),('v3',6)):
        config_path=package/('config/price_optuna_'+short+'.json');cfg=json.loads(config_path.read_text(encoding='utf-8'));cfg['search']['n_trials']=count
        if short=='v3':cfg['v3']['startup_trials']=1
        config_path.write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding='utf-8')
        bat('SELECT_'+short.upper()+'.bat','05_select_'+short)
        bat('CHECK_INPUT.bat','06_check_'+short)
        bat('RETRAIN.bat','07_retrain_'+short)
        pointer,output,hashes=snapshot();preserved[short]=(pointer,output,hashes)
        report_data=json.loads((output/'price/training_report.json').read_text(encoding='utf-8'))
        assert report_data['engine']=='optuna_'+short and report_data['training_rows']==16 and report_data['feature_count']==36
        assert report_data['encoded_feature_count']==40 and report_data['trials_completed']==count
        assert report_data['attribute_types']['x0']=='数值' and report_data['attribute_types']['x3']=='配置类型' and report_data['attribute_types']['x4']=='配置类型'
        assert report_data['native_equivalence_max_delta_wan']<1e-5 and report_data['selected_split_reload_verified']
        if short=='v3':
            assert report_data['phase_counts']=={'nsga':4,'tpe':2}
            assert report_data['history_trials_imported']>=3
            assert report_data['tpe_inherited_completed_trials']>=7
        events['retrained_'+short]=exercise(package,report.with_name(report.stem+'_08_retrained_'+short+'.log'),generation=(short=='v3'),expected_rows=16,expected_features=36,change_field='x1',change_delta=1.)
    # Switch back after both trainings. A prediction service must load the V2 registry,
    # and neither selection nor saved START can mutate the V3 files.
    bat('SELECT_V2.bat','09_back_to_v2');assert snapshot()[0]['output']==preserved['v2'][0]['output']
    events['switch_back_v2']=exercise(package,report.with_name(report.stem+'_10_switched_v2.log'),generation=False,expected_rows=16,expected_features=36,change_field='x1',change_delta=1.)
    assert preserved['v3'][2]=={key:(sha(preserved['v3'][1]/key_path),(preserved['v3'][1]/key_path).stat().st_mtime_ns) for key,key_path in (('price_model','price/price_native_bundle.pkl'),('effectiveness_model','effectiveness/effectiveness_bundle.json'),('business_package','business_release.iprelease.json'))}
    bat('COMPARE_V2_V3.bat','11_compare','--trials','6')
    comparisons=sorted((package/'outputs').glob('compare_*/comparison.json'));assert comparisons
    comparison=json.loads(comparisons[-1].read_text(encoding='utf-8'));assert comparison['matching_data_and_protocol'] and comparison['new_trial_budget_each']==6
    assert {r['完成新试验'] for r in comparison['results']}=={6}
    assert json.loads((package/'config/portable.json').read_text(encoding='utf-8'))['price_engine']=='optuna_v2'
    events['comparison']=comparison
    bat('SELECT_V3.bat','12_final_v3');before,version,hashes=snapshot()
    disabled=package/'input/products.xlsx.disabled';(package/'input/products.xlsx').rename(disabled)
    events['saved_no_input']=exercise(package,report.with_name(report.stem+'_13_no_input.log'),generation=False,expected_rows=16,expected_features=36,change_field='x1',change_delta=1.)
    # Export only in full acceptance; patch acceptance already exercises independent runtime.
    if not a.patch:
        export=destination.parent/'v23_saved_acceptance.zip'
        if export.exists():raise ValueError('Choose a new export destination')
        bat('EXPORT.bat','14_export','--output',str(export));assert hashes==snapshot()[2]
        unpack=destination.parent/'v23x';unpack.mkdir()
        with zipfile.ZipFile(str(export)) as z:
            assert z.testzip() is None and not any('/input/' in name or '/backups/' in name for name in z.namelist())
            z.extractall(str(unpack));folder=z.namelist()[0].split('/')[0]
        saved=unpack/folder
        events['relocated_saved_export']=exercise(saved,report.with_name(report.stem+'_15_exported_start.log'),generation=True,expected_rows=16,expected_features=36,change_field='x1',change_delta=1.)
        events['export_model_hashes_unchanged']=True
    events.update(status='PASS',zip_sha256=sha(a.zip),isolated_embedded_runtime=True,actual_bat_training_and_switching=True,training_input_not_required_for_start=True,
                  missing_input_dependency_regression=False,attribute_semantics=['数值','配置类型'],business_accuracy_certified=False)
    report.write_text(json.dumps(events,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({'status':'PASS','patch':a.patch,'zip_sha256':events['zip_sha256']},ensure_ascii=False))
if __name__=='__main__':main()
