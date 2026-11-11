"""Freeze the unmodified expert runtime using its own dependency environment."""
import argparse,subprocess,sys,json,shutil,hashlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from product.common import sha,write_json
def build(out,tools):
 out=Path(out).resolve();work=Path(str(out)+'_work');work.mkdir(parents=True,exist_ok=False)
 args=['--noconfirm','--clean','--onedir','--windowed','--name','effect_worker','--distpath',str(work/'dist'),'--workpath',str(work/'cache'),'--specpath',str(work),'--paths',str(ROOT)]
 vendor=ROOT/'services/effectiveness_service/expert_runtime'
 for family in ('lock','pump'):
  for path in (vendor/family).glob('*.py'):
   if path.stem not in ('__init__','system_app','lock_v17_app'):args+=['--hidden-import','services.effectiveness_service.expert_runtime.'+family+'.'+path.stem]
 for name in ['app','services.price_service','automl_pipeline','product.supervisor','matplotlib','tkinter','pandas','sklearn','optuna','scipy.tests','numpy.tests','pip','pytest']:
  args+=['--exclude-module',name]
 args.append(str(ROOT/'product/expert_worker.py'))
 program='import sys;sys.path.insert(0,'+repr(str(Path(tools).resolve()))+');import PyInstaller.__main__;PyInstaller.__main__.run('+repr(args)+')'
 with (work/'freeze.log').open('wb') as log:r=subprocess.run([sys.executable,'-B','-c',program],cwd=str(ROOT),stdout=log,stderr=subprocess.STDOUT)
 if r.returncode:raise ValueError('Expert freeze failed: '+str(work/'freeze.log'))
 shutil.copytree(work/'dist/effect_worker',out)
 if any(p.suffix.lower() in ('.py','.pyw','.pdb','.bat') for p in out.rglob('*') if p.is_file()):raise ValueError('Expert source leak')
 import numpy,scipy,openpyxl
 write_json(out/'EXPERT_RUNTIME.json',{'python':sys.version.split()[0],'numpy':numpy.__version__,'scipy':scipy.__version__,'openpyxl':openpyxl.__version__,'source_hashes':json.loads((vendor/'SOURCE_HASHES.json').read_text(encoding='utf-8-sig')),'exe_sha256':sha(out/'effect_worker.exe')})
 print(json.dumps({'engine':str(out),'exe_sha256':sha(out/'effect_worker.exe')}))
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--out',required=True);p.add_argument('--build-tools',required=True);a=p.parse_args();build(a.out,a.build_tools)
