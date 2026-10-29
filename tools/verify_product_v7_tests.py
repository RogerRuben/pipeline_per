import argparse,json,sys,time,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import app
app.__path__.insert(0,str(ROOT/'app'))
def main():
    p=argparse.ArgumentParser();p.add_argument('--report',required=True);a=p.parse_args();start=time.monotonic()
    suite=unittest.TestSuite([unittest.defaultTestLoader.discover(str(ROOT/'tests'),pattern=x) for x in ('product_optuna_v7_test.py','source_layout_test.py')])
    r=unittest.TextTestRunner(verbosity=2).run(suite)
    Path(a.report).write_text(json.dumps({'status':'PASS' if r.wasSuccessful() else 'FAIL','tests':r.testsRun,'failures':len(r.failures),'errors':len(r.errors),'seconds':time.monotonic()-start},indent=2),encoding='utf-8')
    return 0 if r.wasSuccessful() else 1
if __name__=='__main__':sys.exit(main())
