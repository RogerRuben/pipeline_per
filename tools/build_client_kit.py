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

SKIP={'__pycache__','tests','test','benchmarks','.git','Scripts'}
TOOLS=('client_builder.py','build_product_exe.py','build_native_bootstrap.py','source_provenance.py',
       'verify_v3_release.py','bind_price_evaluation_metadata.py','build_v3_release.py',
       'product_price_automl.py','product_r4_input.py','price_attribute_input.py','product_saved_start.py')


def copy_tree(source,target):
    for path in sorted(Path(source).rglob('*')):
        relative=path.relative_to(source)
        if path.is_file() and not set(relative.parts)&SKIP and path.suffix not in ('.pyc','.pyo','.log'):
            if path.is_symlink():raise ValueError('Linked source is not distributable: '+str(path))
            destination=target/relative;destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(str(path),str(destination))


def build(out,runtime,bootstrap,example):
    out,runtime,bootstrap,example=map(lambda p:Path(p).resolve(),(out,runtime,bootstrap,example))
    if out.exists() or ROOT not in out.parents or out.parts[len(ROOT.parts)] not in ('outputs','deliverables'):
        raise ValueError('Choose a new project outputs/ or deliverables/ directory')
    if read_json(bootstrap/'BOOTSTRAP_META.json')['kind']!='win32-native-cpython-onedir':
        raise ValueError('Win7-target client kit requires the native bootstrap')
    out.mkdir(parents=True);project=out/'project'
    for directory in ('app','product','automl_pipeline'):copy_tree(ROOT/directory,project/directory)
    for directory in ('services/common','services/price_service','services/effectiveness_service'):
        (project/directory).mkdir(parents=True,exist_ok=True)
        for path in (ROOT/directory).glob('*.py'):shutil.copy2(str(path),str(project/directory/path.name))
    copy_tree(ROOT/'services/price_service/optuna_v2',project/'services/price_service/optuna_v2')
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
    shutil.copy2(str(example),str(out/'input/products.xlsx'))
    shutil.copy2(str(example),str(out/'examples/products_example.xlsx'))
    write_json(out/'examples/PROVENANCE.json',{'classification':'synthetic','source_sha256':sha(example),
               'purpose':'Offline pipeline software acceptance; no real business accuracy certification'})
    write_json(out/'settings/project.json',{'product_code':'CLIENT_PRODUCT','product_name':'甲方成品',
               'input_file':'input/products.xlsx','data_kind':'synthetic'})
    cfg=read_json(ROOT/'automl_pipeline/configs/price_optuna_v3.json')
    cfg['v3']['warm_start'].update(auto_history=False,history_file=None,config_file=None)
    write_json(out/'settings/v3.json',cfg)
    write_json(out/'settings/desktop.json',template_config())
    # Win7 Notepad reliably recognizes editable Chinese JSON with a UTF-8 BOM.
    for path in (out/'settings').glob('*.json'):
        content=path.read_text(encoding='utf-8');path.write_text(content,encoding='utf-8-sig')
    shutil.copy2(str(ROOT/'docs/CLIENT_BUILD_KIT.md'),str(out/'先读我.md'))
    (out/'先读我.txt').write_text((ROOT/'docs/CLIENT_BUILD_KIT.md').read_text(encoding='utf-8'),encoding='utf-8-sig')
    commands={'CHECK_ENVIRONMENT.bat':'check','CHECK_INPUT.bat':'inspect','TRAIN_AND_PACKAGE.bat':'build',
              'SELF_TEST.bat':'self-test','REPACK_LAST.bat':'repack'}
    for filename,command in commands.items():
        content=('@echo off\r\nsetlocal\r\n"%SystemRoot%\\System32\\chcp.com" 65001 >nul\r\ncd /d "%~dp0"\r\n'
                 '"%~dp0runtime\\python38\\python.exe" -B -X utf8 "%~dp0project\\tools\\client_builder.py" '+command+' %*\r\n'
                 'set "CLIENT_BUILD_EXIT=%ERRORLEVEL%"\r\n'
                 'if not "%CLIENT_BUILD_EXIT%"=="0" echo FAILED - see project\\outputs\\client_jobs logs\r\n'
                 'if not defined PRODUCT_NO_PAUSE pause\r\nexit /b %CLIENT_BUILD_EXIT%\r\n')
        (out/filename).write_bytes(content.encode('ascii'))
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
    p.add_argument('--bootstrap',required=True);p.add_argument('--example',required=True);a=p.parse_args()
    print(json.dumps(build(a.out,a.runtime,a.bootstrap,a.example),ensure_ascii=True,indent=2))
