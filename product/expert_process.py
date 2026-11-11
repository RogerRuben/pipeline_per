"""Select the sealed expert interpreter; the price interpreter stays pinned."""
import os,sys
from pathlib import Path
def clean_env():return {k:v for k,v in os.environ.items() if not k.startswith(('PYTHON','_PYI','_MEIPASS'))}
def executable(package=None):
 candidates=[Path(package)/'expert/effect_worker.exe'] if package else []
 if getattr(sys,'frozen',False):candidates.append(Path(sys.executable).parent/'expert/effect_worker.exe')
 root=Path(__file__).resolve().parents[1]
 candidates.extend([root.parent/'bootstrap/expert/effect_worker.exe',root/'outputs/r12_expert_final/effect_worker.exe',root/'outputs/r12_expert_engine/effect_worker.exe'])
 for path in candidates:
  if path.is_file():return [str(path)]
 python=Path(os.environ.get('IPDEMO_EXPERT_PYTHON',str(root/'outputs/r12_expert_runtime/python.exe')))
 if python.is_file():return [str(python),'-B',str(root/'product/expert_worker.py')]
 raise ValueError('当前软件缺少专家效能运行时，请使用支持原效能导出包的新版本')
def inspect_command(path,report):return executable()+['--asset-check','effectiveness',str(path),'-',str(report)]
