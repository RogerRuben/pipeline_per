"""Assemble a relocatable offline V3 training and product packaging kit."""
import argparse
import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from product.common import read_json,write_json,sha
from product.desktop_config import DEFAULT_PORTS,template_config
from tools.source_provenance import source_commit
from tools.client_kit_paths import batch_entry,MAX_KIT_ROOT_CHARS

SKIP={'__pycache__','tests','test','benchmarks','.git','Scripts'}
TOOLS=('ds_examples.py','build_ds_release.py','ds_client_config.py','product_identity.py','client_builder.py','client_kit_paths.py','client_workbook.py','build_product_exe.py','build_frozen_engine.py','source_provenance.py',
       'verify_v3_release.py','bind_price_evaluation_metadata.py','release_analysis_metadata.py','build_v3_release.py',
       'product_price_automl.py','product_r4_input.py','price_attribute_input.py','product_saved_start.py')


def copy_tree(source,target):
    for path in sorted(Path(source).rglob('*')):
        relative=path.relative_to(source)
        if path.is_file() and path.name!='_frozen_resources.py' and not set(relative.parts)&SKIP and path.suffix not in ('.pyc','.pyo','.log'):
            if path.is_symlink():raise ValueError('Linked source is not distributable: '+str(path))
            destination=target/relative;destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(str(path),str(destination))


def build(out,runtime,bootstrap,example,model_cases=None):
    out,runtime,bootstrap,example=map(lambda p:Path(p).resolve(),(out,runtime,bootstrap,example))
    if out.exists() or ROOT not in out.parents or out.parts[len(ROOT.parts)] not in ('outputs','deliverables'):
        raise ValueError('Choose a new project outputs/ or deliverables/ directory')
    if read_json(bootstrap/'BOOTSTRAP_META.json')['kind']!='pyinstaller-full-app-onedir':
        raise ValueError('Client kit requires a precompiled source-free software engine')
    out.mkdir(parents=True);project=out/'project'
    for directory in ('app','product','automl_pipeline','cost_effectiveness_analysis'):copy_tree(ROOT/directory,project/directory)
    for directory in ('services/common','services/price_service','services/effectiveness_service'):
        (project/directory).mkdir(parents=True,exist_ok=True)
        for path in (ROOT/directory).glob('*.py'):shutil.copy2(str(path),str(project/directory/path.name))
    copy_tree(ROOT/'services/price_service/optuna_v2',project/'services/price_service/optuna_v2')
    copy_tree(ROOT/'services/effectiveness_service/expert_runtime',project/'services/effectiveness_service/expert_runtime')
    shutil.copy2(str(ROOT/'services/__init__.py'),str(project/'services/__init__.py'))
    (project/'tools').mkdir()
    for name in TOOLS:shutil.copy2(str(ROOT/'tools'/name),str(project/'tools'/name))
    for name in ('product_entry.py',):shutil.copy2(str(ROOT/name),str(project/name))
    (project/'docs').mkdir()
    for name in ('P4_DESKTOP_USAGE.md','P4_1_TARGET_MACHINE.md','NATIVE_DESKTOP_USAGE.md','NATIVE_TARGET_CHECK.md','CLIENT_BUILD_KIT.md'):
        shutil.copy2(str(ROOT/'docs'/name),str(project/'docs'/name))
    copy_tree(bootstrap,out/'bootstrap')
    embedded=out/'runtime/python38';embedded.mkdir(parents=True)
    for path in runtime.iterdir():
        if path.is_file() and (path.suffix.lower() in ('.dll','.pyd') or path.name in
                              ('python.exe','pythonw.exe','python38.zip','LICENSE.txt','python.cat')):
            shutil.copy2(str(path),str(embedded/path.name))
    copy_tree(runtime/'Lib/site-packages',embedded/'Lib/site-packages')
    (embedded/'python38._pth').write_bytes(b'python38.zip\n.\nLib/site-packages\n../../project\nimport site\n')
    for directory in ('input','examples','settings','logs','deliverables'): (out/directory).mkdir()
    from tools.client_workbook import example_with_scores
    sample=example_with_scores(example.read_bytes())
    (out/'input/products.xlsx').write_bytes(sample)
    (out/'examples/products_example.xlsx').write_bytes(sample)
    (out/'examples/产品与评分空白模板.xlsx').write_bytes(example_with_scores(example.read_bytes(),blank=True))
    example=out/'examples/products_example.xlsx'
    write_json(out/'examples/PROVENANCE.json',{'classification':'synthetic','source_sha256':sha(example),
               'purpose':'Offline pipeline software acceptance; no real business accuracy certification'})
    write_json(out/'settings/project.json',{'product_code':'CLIENT_PRODUCT','product_name':'甲方成品',
               'input_file':'input/products.xlsx','data_kind':'synthetic','price_backend':'optuna_v3','effectiveness_package':''})
    cfg=read_json(ROOT/'automl_pipeline/configs/price_optuna_v3.json')
    cfg['v3']['warm_start'].update(auto_history=False,history_file=None,config_file=None)
    write_json(out/'settings/v3.json',cfg)
    write_json(out/'settings/ds.json',{'model_file':'input/DS_model.json','field_map':{},'value_aliases':{}})
    from tools.ds_examples import workbook as ds_workbook
    ds_example=out/'examples/ds';ds_example.mkdir()
    shutil.copy2(str(ROOT/'examples/ds/DEMO_model.json'),str(ds_example/'DS_model.json'))
    (ds_example/'products.xlsx').write_bytes(ds_workbook(ds_example/'DS_model.json'))
    write_json(ds_example/'ds.json',{'model_file':'examples/ds/DS_model.json','field_map':{},'value_aliases':{}})
    if model_cases:
        case_folder=out/'examples/model_cases';case_folder.mkdir()
        for path in Path(model_cases).glob('*'):
            if path.is_file():shutil.copy2(str(path),str(case_folder/path.name))
    write_json(out/'settings/desktop.json',template_config())
    # Win7 Notepad reliably recognizes editable Chinese JSON with a UTF-8 BOM.
    for path in (out/'settings').glob('*.json'):
        content=path.read_text(encoding='utf-8');path.write_text(content,encoding='utf-8-sig')
    shutil.copy2(str(ROOT/'docs/CLIENT_BUILD_KIT.md'),str(out/'先读我.md'))
    (out/'先读我.txt').write_text((ROOT/'docs/CLIENT_BUILD_KIT.md').read_text(encoding='utf-8'),encoding='utf-8-sig')
    commands={'CHECK_ENVIRONMENT.bat':'check','CHECK_INPUT.bat':'inspect','TRAIN_AND_PACKAGE.bat':'build','BUILD_AND_PACKAGE.bat':'build',
              'SELF_TEST.bat':'self-test','SELF_TEST_DS.bat':'self-test-ds','REPACK_LAST.bat':'repack'}
    for filename,command in commands.items():
        (out/filename).write_bytes(batch_entry(command))
    source=source_commit(ROOT)
    source_files={path.relative_to(project).as_posix():sha(path) for path in sorted(project.rglob('*')) if path.is_file()}
    write_json(project/'SOURCE_BUILD.json',{'format':'pipeline-source-distribution-1.0','source_sha':source,'files':source_files})
    program='import importlib.metadata as m,json;print(json.dumps({d.metadata["Name"]:d.version for d in m.distributions()}))'
    completed=subprocess.run([str(embedded/'python.exe'),'-B','-X','utf8','-c',program],capture_output=True,check=True)
    dependencies=json.loads(completed.stdout.decode('utf-8'))
    files={path.relative_to(out).as_posix():sha(path) for path in sorted(out.rglob('*')) if path.is_file()
           and path.relative_to(out).parts[0] not in ('input','settings','logs','deliverables')}
    write_json(out/'KIT_MANIFEST.json',{'format':'pipeline-client-build-kit-1.0','source_sha':source,'engine':'optuna_v3',
               'target':'Windows 7 SP1 x64','python':'3.8.10','files':files,'example_sha256':sha(example),
               'training_dependencies':dependencies,'external_python_required':False,'git_required':False,
               'compiler_on_client_required':False,'network_required':False,'win7_machine_validated':False,
               'path_policy':{'max_root_utf16_chars':MAX_KIT_ROOT_CHARS,'recommended_root':'C:\\CB6'},
               'mutable_directories':['input','settings','logs','deliverables','builder_state','project/outputs','project/deliverables']})
    archive=Path(str(out)+'.zip')
    with zipfile.ZipFile(str(archive),'x',zipfile.ZIP_DEFLATED,compresslevel=6) as bundle:
        for path in sorted(out.rglob('*')):
            if path.is_file():bundle.write(str(path),out.name+'/'+path.relative_to(out).as_posix())
    checksum=sha(archive);Path(str(archive)+'.sha256').write_text(checksum+'  '+archive.name+'\n',encoding='ascii')
    report={'status':'built','source_sha':source,'kit':str(out),'archive':str(archive),'sha256':checksum,
            'archive_bytes':archive.stat().st_size,'immutable_files':len(files),'win7_machine_validated':False}
    write_json(Path(str(out)+'_build.json'),report);return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',required=True);p.add_argument('--runtime',required=True)
    p.add_argument('--bootstrap',required=True);p.add_argument('--example',required=True);p.add_argument('--model-cases');a=p.parse_args()
    print(json.dumps(build(a.out,a.runtime,a.bootstrap,a.example,a.model_cases),ensure_ascii=True,indent=2))
