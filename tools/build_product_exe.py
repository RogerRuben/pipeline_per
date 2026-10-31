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

SKIP = {'__pycache__','tests','test','benchmarks','.git'}
RUNTIME_PACKAGES = ('numpy','scipy','sklearn','joblib')


def copy_tree(source,destination):
    for path in sorted(Path(source).rglob('*')):
        relative = path.relative_to(source)
        if path.is_file() and not set(relative.parts)&SKIP and path.suffix not in ('.pyc','.pyo'):
            if path.is_symlink():
                raise ValueError('Linked input is not allowed: '+str(path))
            target = destination/relative
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(str(path),str(target))


def build(release_dir,runtime,tool_dir,out,version,bootstrap=None):
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
                   '--specpath',str(stamp),'--paths',str(ROOT),str(ROOT/'product_entry.py')]
        env = dict(os.environ,PYINSTALLER_CONFIG_DIR=str(stamp/'cache'))
        result = subprocess.run(command,cwd=str(ROOT),env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        (stamp/'pyinstaller.log').write_bytes(result.stdout)
        if result.returncode:
            raise RuntimeError('PyInstaller failed; see '+str(stamp/'pyinstaller.log'))
        bootstrap = stamp/'dist/product'
    bootstrap = Path(bootstrap).resolve()
    # A bootstrap reuse is permitted only with matching frozen launcher source.
    launcher_sources = {str(p.relative_to(ROOT).as_posix()):sha(p) for p in
                        [ROOT/'product_entry.py',ROOT/'product/__init__.py',ROOT/'product/common.py',
                         ROOT/'product/launcher.py',ROOT/'product/windows.py']}
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
    licenses = out/'licenses'
    licenses.mkdir()
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
        distribution = 'scikit_learn' if name == 'sklearn' else name
        metadata = list(origin_site.glob(distribution+'-*.dist-info'))
        if len(metadata) != 1:
            raise ValueError('Missing/ambiguous runtime metadata: '+name)
        copy_tree(metadata[0],site/metadata[0].name)
    shutil.copy2(str(origin_site/'threadpoolctl.py'),str(site/'threadpoolctl.py'))
    for path in origin_site.glob('threadpoolctl-*.dist-info'):
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
    paths.update(evaluation_metadata='release/evaluation_metadata.json',product_code=manifest['product_code'],
                 product_name=manifest['product_name'],classification=classification,
                 effectiveness='trial_snapshot',virtual_data=manifest.get('virtual_data'))
    defaults = out/'defaults'
    defaults.mkdir()
    shutil.copy2(str(ROOT/'app/defaults/scenario_config.json'),str(defaults/'scenario_config.json'))
    write_json(defaults/'model_services.json',{'execution_mode':'services','local_fallback':False,
                                              'price_output':{'unit':'wan_yuan','scale':1.0}})
    write_json(defaults/'deep_price_adjustment.json',{'confirmed':False,'message':'未配置正式价格校正规则'})
    help_path = ROOT/'docs/P4_DESKTOP_USAGE.md'
    if help_path.is_file():
        shutil.copy2(str(help_path),str(out/'使用说明.md'))
    source_sha = subprocess.check_output(['git','rev-parse','HEAD'],cwd=str(ROOT),text=True).strip()
    files = {p.relative_to(out).as_posix():sha(p) for p in sorted(out.rglob('*')) if p.is_file()}
    write_json(out/'PACKAGE_MANIFEST.json',{'format':'pipeline-per-desktop-1.0','version':version,'data_schema':1,
               'entrypoint':'product.exe','packaging':'PyInstaller-5.13.2-onedir-windowed-launcher+embedded-model-runtime',
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
    args = parser.parse_args()
    print(json.dumps(build(args.release_dir,args.python_runtime,args.build_tools,args.out,args.version,args.bootstrap),ensure_ascii=True,indent=2))
