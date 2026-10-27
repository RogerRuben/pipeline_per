# -*- coding: utf-8 -*-
import argparse,json,sys,time,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import app
app.__path__.insert(0,str(ROOT/'app'))


def main():
    p=argparse.ArgumentParser();p.add_argument('--report',required=True);a=p.parse_args();start=time.monotonic()
    suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'),pattern='product_optuna_v51_test.py')
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    report=Path(a.report);report.parent.mkdir(parents=True,exist_ok=True)
    report.write_text(json.dumps({'status':'PASS' if result.wasSuccessful() else 'FAIL','tests':result.testsRun,
        'failures':len(result.failures),'errors':len(result.errors),'seconds':time.monotonic()-start},indent=2),encoding='utf-8')
    return 0 if result.wasSuccessful() else 1


if __name__=='__main__':sys.exit(main())
