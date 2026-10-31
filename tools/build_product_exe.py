"""Build/repack a new onedir delivery from an existing trusted V3 release.

No training or production-data access. Never overwrite a package or userdata.
Run with Python 3.8.10 x64 and a separately pinned PyInstaller tool directory.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from product.common import contained,read_json,sha,write_json
from tools.verify_v3_release import preflight
from tools.bind_price_evaluation_metadata import bind
from product.compatibility import make_contract
from tools.source_provenance import source_commit
from tools.build_native_bootstrap import source_fingerprints
from product.desktop_config import DEFAULT_PORTS,validate_config,template_config

SKIP = {'__pycache__','tests','test','benchmarks','.git'}
RUNTIME_PACKAGES = ('numpy','scipy','sklearn','joblib','pandas','dateutil','pytz')


def copy_tree(source,destination):
    for path in sorted(Path(source).rglob('*')):
        relative = path.relative_to(source)
        if path.is_file() and not set(relative.parts)&SKIP and path.suffix not in ('.pyc','.pyo'):
            if path.is_symlink():
                raise ValueError('Linked input is not allowed: '+str(path))
            target = destination/relative
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(str(path),str(target))


def build(release_dir,runtime,tool_dir,out,version,bootstrap=None,legacy_packages=(),desktop_config=None):
    if sys.version_info[:3] != (3,8,10) or sys.maxsize <= 2**32:
        raise ValueError('Build requires Python 3.8.10 x64 matching the V3 model')
    manifest,artifacts,source,environment = preflight(release_dir,'unconfirmed')
    out = Path(out).resolve()
    # Never replace/delete a prior delivery, even when its name looks generated.
    if out.exists() or out == ROOT or ROOT not in out.parents or out.parts[len(ROOT.parts)] not in ('outputs','deliverables'):
        raise ValueError('Choose a new outputs/ or deliverables/ directory')
    runtime,tool_dir = Path(runtime).resolve(),Path(tool_dir).resolve()
    if not (runtime/'python.exe').is_file() or not (runtime/'pythonw.exe').is_file():
        raise ValueError('An embedded CPython runtime with pythonw.exe is required')
    stamp = out.parent/(out.name+'_build')
    if stamp.exists():
        raise ValueError('Build workspace exists; choose a new output name')
    stamp.mkdir(parents=True)
    if bootstrap is None:
        sys.path.insert(0,str(tool_dir))
        import PyInstaller
        if PyInstaller.__version__ != '5.13.2':
            raise ValueError('PyInstaller must be pinned to 5.13.2')
        program = 'import sys;sys.path.insert(0,'+repr(str(tool_dir))+');import PyInstaller.__main__;PyInstaller.__main__.run(sys.argv[1:])'
        command = [sys.executable,'-c',program,'--noconfirm','--clean','--onedir','--windowed',
                   '--name','product','--distpath',str(stamp/'dist'),'--workpath',str(stamp/'work'),
                   '--specpath',str(stamp),'--paths',str(ROOT),'--icon',str(ROOT/'app/static/launcher.ico'),str(ROOT/'product_entry.py')]
        env = dict(os.environ,PYINSTALLER_CONFIG_DIR=str(stamp/'cache'))
        result = subprocess.run(command,cwd=str(ROOT),env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        (stamp/'pyinstaller.log').write_bytes(result.stdout)
        if result.returncode:
            raise RuntimeError('PyInstaller failed; see '+str(stamp/'pyinstaller.log'))
        bootstrap = stamp/'dist/product'
    bootstrap = Path(bootstrap).resolve()
    # A bootstrap reuse is permitted only with matching frozen launcher source.
    launcher_sources = source_fingerprints()
    provenance = bootstrap/'BOOTSTRAP_SOURCE.json'
    if provenance.exists():
        if read_json(provenance) != launcher_sources:
            raise ValueError('Frozen bootstrap source does not match current launcher')
    else:
        # Only a fresh compiler result may create the bootstrap seal.
        if bootstrap != stamp/'dist/product':
            raise ValueError('Reused bootstrap lacks source provenance')
        write_json(provenance,launcher_sources)
    shutil.copytree(str(bootstrap),str(out))
    bootstrap_meta=read_json(bootstrap/'BOOTSTRAP_META.json') if (bootstrap/'BOOTSTRAP_META.json').exists() else {'kind':'pyinstaller-5.13.2'}
    payload = out/'payload'
    copy_tree(ROOT/'app',payload/'app')
    copy_tree(ROOT/'product',payload/'product')
    for directory in ('services/common','services/price_service','services/effectiveness_service'):
        target = payload/directory
        target.mkdir(parents=True,exist_ok=True)
        # Exclude model inventories, wheelhouses, notebooks and training engines.
        for path in (ROOT/directory).glob('*.py'):
            shutil.copy2(str(path),str(target/path.name))
    shutil.copy2(str(ROOT/'services/__init__.py'),str(payload/'services/__init__.py'))
    # Native V3 pickles reference these existing preprocessing/member classes.
    # Copy their original source unchanged; no optimizer is invoked or bundled.
    native_classes = payload/'services/price_service/optuna_v2'
    native_classes.mkdir()
    for name in ('__init__.py','typed_features.py','native_member.py','price_automl_core.py'):
        shutil.copy2(str(ROOT/'services/price_service/optuna_v2'/name),str(native_classes/name))
    licenses = out/'licenses'
    licenses.mkdir(exist_ok=True)
    if bootstrap_meta['kind'].startswith('pyinstaller'):
        shutil.copy2(str(tool_dir/'pyinstaller-5.13.2.dist-info/COPYING.txt'),str(licenses/'PyInstaller_COPYING.txt'))
    # Module and bundled numerical-library licenses are also retained in each
    # runtime .dist-info directory; the CPython license is adjacent to Python.
    (licenses/'README.txt').write_text('CPython: runtime/python38/LICENSE.txt\nNumerical dependencies: runtime/python38/Lib/site-packages/*.dist-info/LICENSE*\nECharts: payload/app/static/deep_price/vendor/LICENSE*\n',encoding='utf-8')
    embedded = out/'runtime/python38'
    embedded.mkdir(parents=True)
    for path in runtime.iterdir():
        if path.is_file() and (path.suffix.lower() in ('.dll','.pyd') or path.name in
                              ('python.exe','pythonw.exe','python38.zip','LICENSE.txt','python.cat')):
            shutil.copy2(str(path),str(embedded/path.name))
    # Relative-only search path; no build machine, parent repo or system Python.
    (embedded/'python38._pth').write_bytes(b'python38.zip\n.\nLib/site-packages\n../../payload\nimport site\n')
    site = embedded/'Lib/site-packages'
    site.mkdir(parents=True)
    origin_site = runtime/'Lib/site-packages'
    for name in RUNTIME_PACKAGES:
        copy_tree(origin_site/name,site/name)
        distribution = {'sklearn':'scikit_learn','dateutil':'python_dateutil'}.get(name,name)
        metadata = list(origin_site.glob(distribution+'-*.dist-info'))
        if len(metadata) != 1:
            raise ValueError('Missing/ambiguous runtime metadata: '+name)
        copy_tree(metadata[0],site/metadata[0].name)
    shutil.copy2(str(origin_site/'threadpoolctl.py'),str(site/'threadpoolctl.py'))
    shutil.copy2(str(origin_site/'six.py'),str(site/'six.py'))
    for pattern in ('threadpoolctl-*.dist-info','six-*.dist-info'):
        for path in origin_site.glob(pattern):
            copy_tree(path,site/path.name)
    release = out/'release'
    release.mkdir()
    paths = {}
    for key,path in artifacts.items():
        name = {'price_model':'price_native_bundle.pkl','effectiveness_model':'effectiveness_bundle.json',
                'business_package':'business_release.iprelease.json'}[key]
        shutil.copy2(str(path),str(release/name))
        paths[key] = 'release/'+name
    for name,source_path in [('training_report.json',artifacts['price_model'].parent/'training_report.json'),
                             ('price_native_bundle.pkl.manifest.json',Path(str(artifacts['price_model'])+'.manifest.json'))]:
        shutil.copy2(str(source_path),str(release/name))
    classification = 'synthetic' if manifest.get('virtual_data') else 'unconfirmed'
    bind(release/'price_native_bundle.pkl',release/'training_report.json',release/'evaluation_metadata.json',classification)
    # Fail the build before sealing when a pickle-referenced class or indirect
    # DLL/dependency is missing. Use the delivery interpreter, not the builder.
    smoke = ('import json;from services.price_service.app import PriceService;'
             's=PriceService('+repr(str(release/'price_native_bundle.pkl'))+',None,evaluation_metadata='
             +repr(str(release/'evaluation_metadata.json'))+');'
             's.handle_post("/api/v1/predict",s.example_request());print(json.dumps({"health":s.health(),"schema":s.schema()}))')
    result = subprocess.run([str(embedded/'python.exe'),'-I','-B','-X','utf8','-c',smoke],
                            cwd=str(out),stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    (stamp/'embedded_model_smoke.log').write_bytes(result.stdout)
    if result.returncode:
        raise RuntimeError('Bundled model runtime smoke failed; see '+str(stamp/'embedded_model_smoke.log'))
    observed=json.loads(result.stdout.decode('utf-8'))
    contract=make_contract(observed['schema'],read_json(artifacts['business_package']),sha(artifacts['price_model']),
                           {'unit':'wan_yuan','scale':1.0})
    from product.common import verify_package
    for legacy_dir in legacy_packages:
        old=verify_package(legacy_dir,True)
        # An old package lacking schema state can be admitted only for the exact
        # same model and business package. Real retraining upgrades require a
        # predecessor that already carries the full accepted schema contract.
        if (old['files'][old['release']['price_model']]!=contract['model_sha256'] or
            old['files'][old['release']['business_package']]!=sha(artifacts['business_package'])):
            raise ValueError('Legacy predecessor must use the identical model and business package')
        contract['compatible_legacy'].append({'version':old['version'],
                      'business_package_sha256':old['files'][old['release']['business_package']]})
    write_json(release/'delivery_contract.json',contract)
    paths.update(evaluation_metadata='release/evaluation_metadata.json',product_code=manifest['product_code'],
                 product_name=manifest['product_name'],classification=classification,
                 effectiveness='trial_snapshot',virtual_data=manifest.get('virtual_data'),
                 delivery_contract='release/delivery_contract.json')
    defaults = out/'defaults'
    defaults.mkdir()
    shutil.copy2(str(ROOT/'app/defaults/scenario_config.json'),str(defaults/'scenario_config.json'))
    write_json(defaults/'model_services.json',{'execution_mode':'services','local_fallback':False,
                                              'price_output':{'unit':'wan_yuan','scale':1.0}})
    write_json(defaults/'deep_price_adjustment.json',{'confirmed':False,'message':'未配置正式价格校正规则'})
    desktop=validate_config(read_json(desktop_config)) if desktop_config else template_config()
    write_json(defaults/'desktop.json',desktop)
    (out/'访问地址.txt').write_text('Portal: http://127.0.0.1:%d/portal\nManagement: http://127.0.0.1:%d/\nChange ports in user-data/config/desktop.json; restart the application to apply.\n'%
        (desktop['ports']['web'],desktop['ports']['control']),encoding='utf-8')
    (out/'SHOW_ADDRESSES.bat').write_bytes(b'@echo off\r\nsetlocal\r\nchcp 65001 >nul\r\n"%~dp0runtime\\python38\\python.exe" -B -X utf8 "%~dp0payload\\product\\desktop_entry.py" --package-root "%~dp0." --addresses %*\r\nif not defined PRODUCT_NO_PAUSE pause\r\nendlocal\r\n')
    help_path = ROOT/('docs/NATIVE_DESKTOP_USAGE.md' if bootstrap_meta['kind']=='win32-native-cpython-onedir' else 'docs/P4_DESKTOP_USAGE.md')
    if help_path.is_file():
        shutil.copy2(str(help_path),str(out/'使用说明.md'))
    shutil.copy2(str(ROOT/'docs/P4_1_TARGET_MACHINE.md'),str(out/'目标机验收说明.md'))
    launcher=('@echo off\r\nsetlocal\r\nchcp 65001 >nul\r\n'
              'set "P4_TARGET_OUT=%~1"\r\n'
              'if not defined P4_TARGET_OUT set "P4_TARGET_OUT=%LOCALAPPDATA%\\PipelinePerTargetCheck_%RANDOM%_%RANDOM%"\r\n'
              '"%~dp0runtime\\python38\\python.exe" -B -X utf8 "%~dp0payload\\product\\target_check.py" --package "%~dp0." --out "%P4_TARGET_OUT%" > "%P4_TARGET_OUT%.bootstrap.log" 2>&1\r\n'
              'if errorlevel 1 (\r\n echo Target check failed. See "%P4_TARGET_OUT%.bootstrap.log" and target_report.json\r\n'
              ' if not defined PRODUCT_NO_PAUSE pause\r\n exit /b 1\r\n)\r\n'
              'echo Target check completed: "%P4_TARGET_OUT%\\target_report.json"\r\n'
              'if not defined PRODUCT_NO_PAUSE pause\r\nendlocal\r\n')
    (out/'TARGET_CHECK.bat').write_bytes(launcher.encode('ascii'))
    source_sha = source_commit(ROOT)
    files = {p.relative_to(out).as_posix():sha(p) for p in sorted(out.rglob('*')) if p.is_file()}
    write_json(out/'PACKAGE_MANIFEST.json',{'format':'pipeline-per-desktop-1.0','version':version,'data_schema':1,
               'entrypoint':'product.exe','packaging':bootstrap_meta['kind']+'+embedded-model-runtime',
               'build_source_sha':source_sha,'launcher_source_sha256':launcher_sources,'files':files,'release':paths,
               'training_environment':environment,'runtime_python':'3.8.10','architecture':'x64',
               'default_data_dir':'%LOCALAPPDATA%/PipelinePer','external_python_required':False,
               'training_on_start':False,'win7_validated':False,'code_signed':False})
    archive = out.with_suffix('.zip')
    if archive.exists():
        raise ValueError('Archive already exists')
    with zipfile.ZipFile(str(archive),'x',zipfile.ZIP_DEFLATED,compresslevel=6) as bundle:
        for path in sorted(out.rglob('*')):
            if path.is_file():
                bundle.write(str(path),out.name+'/'+path.relative_to(out).as_posix())
    result = {'status':'built','package':str(out),'archive':str(archive),'archive_sha256':sha(archive),
              'archive_bytes':archive.stat().st_size,'files':len(files),'model_sha256':sha(artifacts['price_model']),
              'bootstrap':str(bootstrap),'source_sha':source_sha,'win7_validated':False}
    write_json(stamp/'build_report.json',result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--release-dir',required=True)
    parser.add_argument('--python-runtime',required=True)
    parser.add_argument('--build-tools',required=True)
    parser.add_argument('--out',required=True)
    parser.add_argument('--version',required=True)
    parser.add_argument('--bootstrap',help='Reuse a sealed matching frozen launcher without recompiling')
    parser.add_argument('--legacy-package',action='append',default=[],help='Approve a verified P4 predecessor with identical artifacts')
    parser.add_argument('--desktop-config',help='JSON defining fixed service ports and open_browser')
    args = parser.parse_args()
    print(json.dumps(build(args.release_dir,args.python_runtime,args.build_tools,args.out,args.version,args.bootstrap,args.legacy_package,args.desktop_config),ensure_ascii=True,indent=2))
