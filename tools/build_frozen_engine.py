"""Build the reusable, source-free onedir engine on the developer host."""
import argparse
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from product.common import sha,write_json

def engine_sources():
    records={}
    for folder in ('app','product','cost_effectiveness_analysis','services/common','services/price_service','services/effectiveness_service'):
        for path in sorted((ROOT/folder).rglob('*')):
            rel=path.relative_to(ROOT).as_posix()
            nested=('services/price_service/optuna_v2/','services/price_service/optuna_v4/','services/price_service/optuna_v5/','services/price_service/optuna_v6/','services/price_service/optuna_v7/','services/effectiveness_service/expert_runtime/')
            if folder.startswith('services/') and '/' in path.relative_to(ROOT/folder).as_posix() and not rel.startswith(nested):
                continue
            if not path.is_file() or set(path.relative_to(ROOT).parts)&{'__pycache__','tests','test','benchmarks'} or path.name=='_frozen_resources.py':continue
            if path.suffix=='.py' or rel.startswith(('app/static/','cost_effectiveness_analysis/static/')) or '/defaults/' in rel or rel=='product/control.html':
                records[rel]=sha(path)
    records['product_entry.py']=sha(ROOT/'product_entry.py')
    records['services/__init__.py']=sha(ROOT/'services/__init__.py')
    override=page_override_root()
    if override:
        from product.static_override import override_file
        for path in sorted(override.rglob('*')):
            if not path.is_file():
                continue
            rel=path.relative_to(override).as_posix()
            if override_file(override, rel) is None:
                continue
            records['page_overrides/'+rel]=sha(path)
    return records

def page_override_root():
    """Pages shown by dev mode. Recompile must bake the same files into the exe."""
    import os
    chosen=os.environ.get('PIPELINE_STATIC_DIR')
    if chosen and Path(chosen).is_dir():
        return Path(chosen)
    kit=ROOT.parent
    candidate=kit/'page_overrides'
    if (kit/'KIT_MANIFEST.json').is_file() and candidate.is_dir():
        return candidate
    return None

def overlay_static_files(files, override_root):
    from product.static_override import override_file
    if not override_root or not Path(override_root).is_dir():
        return files
    root=Path(override_root)
    for path in sorted(root.rglob('*')):
        if not path.is_file():
            continue
        rel=path.relative_to(root).as_posix()
        if override_file(root, rel) is None:
            continue
        files['app/static/'+rel]=path.read_bytes()
    return files

def build(out,tools,expert_engine=None):
    out=Path(out).resolve();tools=Path(tools).resolve()
    if out.exists() or ROOT/'outputs' not in out.parents:raise ValueError('Choose new outputs directory')
    sources=engine_sources()
    work=out.parent/(out.name+'_work')
    work.parent.mkdir(parents=True, exist_ok=True)
    if work.exists():raise ValueError('Choose new outputs directory')
    work.mkdir()
    files={}
    for folder in ('app/static','app/defaults','product/defaults','cost_effectiveness_analysis/static'):
        for path in sorted((ROOT/folder).rglob('*')):
            if path.is_file():files[path.relative_to(ROOT).as_posix()]=path.read_bytes()
    files['product/control.html']=(ROOT/'product/control.html').read_bytes()
    overlay_static_files(files, page_override_root())
    memory=io.BytesIO()
    with zipfile.ZipFile(memory,'w',zipfile.ZIP_DEFLATED) as bundle:
        for name in sorted(files):
            bundle.writestr(name, files[name])
    generated=ROOT/'product/_frozen_resources.py'
    cache=ROOT/'product'/'__pycache__'
    if cache.is_dir():
        for stale in cache.glob('_frozen_resources*.pyc'):
            stale.unlink()
    generated.write_text('import io,zipfile\nBLOB='+repr(memory.getvalue())+'\nARCHIVE=zipfile.ZipFile(io.BytesIO(BLOB))\ndef read(name):return ARCHIVE.read(name)\n',encoding='utf-8')
    hooks=work/'hooks';hooks.mkdir()
    (hooks/'hook-sklearn.py').write_text('from PyInstaller.utils.hooks import collect_dynamic_libs\nbinaries=collect_dynamic_libs("sklearn")\n',encoding='utf-8')
    args=['--additional-hooks-dir',str(hooks),'--noconfirm','--clean','--onedir','--windowed','--name','product','--distpath',str(work/'dist'),
          '--workpath',str(work/'cache'),'--specpath',str(work),'--paths',str(ROOT),
          '--icon',str(ROOT/'app/static/launcher.ico')]
    for module in ('services.price_service.ds_backend','services.price_service.ds_engine','product._frozen_resources','cost_effectiveness_analysis.app','cost_effectiveness_analysis.embed',
                   'cost_effectiveness_analysis.db_reader','cost_effectiveness_analysis.model_client',
                   'cost_effectiveness_analysis.analysis','services.price_service.optuna_v2.typed_features',
                   'services.price_service.optuna_v2.native_member','services.price_service.optuna_v2.price_automl_core',
                   'services.price_service.optuna_v4.native_member','services.price_service.optuna_v4.backend',
                   'services.price_service.optuna_v5.trace','services.price_service.optuna_v5.persistence',
                   'services.price_service.optuna_v6.backend','services.price_service.optuna_v7.backend',
                   'sklearn.ensemble._forest','sklearn.tree._tree','sklearn.neighbors._typedefs',
                   'sklearn.utils._weight_vector','sklearn.utils._cython_blas','sklearn.svm._libsvm',
                   'sklearn.svm._libsvm_sparse','scipy.special._ufuncs_cxx','scipy.linalg.cython_blas',
                   'scipy.linalg.cython_lapack','pandas._libs.tslibs.base','pkg_resources'):
        args+=['--hidden-import',module]
    args+=['--hidden-import','product.delivery_profile']
    for module in ('optuna','automl_pipeline','matplotlib','tkinter','IPython','pytest','pip','notebook','torch',
                   'scipy.tests','numpy.tests','pandas.tests','sklearn.tests','tools','sqlalchemy','greenlet','alembic','mako','markupsafe'):
        args+=['--exclude-module',module]
    args.append(str(ROOT/'product_entry.py'))
    program='import sys;sys.path.insert(0,'+repr(str(tools))+');import PyInstaller.__main__;PyInstaller.__main__.run('+repr(args)+')'
    with (work/'freeze.log').open('wb') as log:
        result=subprocess.run([sys.executable,'-c',program],cwd=str(ROOT),stdout=log,stderr=subprocess.STDOUT)
    if result.returncode:raise RuntimeError('Freeze failed: '+str(work/'freeze.log'))
    import shutil
    shutil.copytree(str(work/'dist/product'),str(out))
    expert_meta=None
    if expert_engine:
        expert_engine=Path(expert_engine).resolve();expert_meta=json.loads((expert_engine/'EXPERT_RUNTIME.json').read_text(encoding='utf-8-sig'))
        if sha(expert_engine/'effect_worker.exe')!=expert_meta['exe_sha256']:raise ValueError('Expert engine changed')
        shutil.copytree(str(expert_engine),str(out/'expert'))
    forbidden=[str(p) for p in out.rglob('*') if p.is_file() and p.suffix.lower() in ('.py','.pyw','.ipynb','.pdb','.c','.cpp','.h','.bat')]
    if forbidden:raise ValueError('Source/build files leaked: '+str(forbidden))
    if sources!=engine_sources():raise ValueError('Application sources changed during freeze')
    write_json(out/'BOOTSTRAP_SOURCE.json',sources)
    write_json(out/'BOOTSTRAP_META.json',{'kind':'pyinstaller-full-app-onedir','exe_sha256':sha(out/'product.exe'),
               'runtime_python':'3.8.10','source_files_distributed':False,'ui_resources':'embedded',
               'win7_machine_tested':False,'pyinstaller':'5.13.2','expert_runtime':expert_meta})
    return {'engine':str(out),'files':len(list(out.rglob('*'))),'exe_sha256':sha(out/'product.exe')}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',required=True);p.add_argument('--build-tools',required=True)
    p.add_argument('--expert-engine');a=p.parse_args();print(json.dumps(build(a.out,a.build_tools,a.expert_engine),indent=2))

