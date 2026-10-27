"""Launch services with this checkout, including source tests using an older runtime."""
import runpy,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import app
if str(ROOT/'app') not in app.__path__:app.__path__.insert(0,str(ROOT/'app'))
module=sys.argv[1];sys.argv=sys.argv[1:];runpy.run_module(module,run_name='__main__')
