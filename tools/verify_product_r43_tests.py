# -*- coding: utf-8 -*-
import argparse
import json
import sys
import time
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import app
app.__path__.insert(0,str(ROOT/'app'))


def main():
    p=argparse.ArgumentParser();p.add_argument('--report',required=True);a=p.parse_args()
    patterns=['product_price_r4_test.py','product_price_r41_test.py','product_price_r42_test.py',
              'product_price_r43_test.py','product_patch_dependency_test.py','product_saved_start_test.py','portable_compatibility_test.py','test_gflownet_generator.py']
    suites=[unittest.defaultTestLoader.discover(str(ROOT/'tests'),pattern=pattern) for pattern in patterns]
    started=time.monotonic();result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(suites))
    report={'status':'PASS' if result.wasSuccessful() else 'FAIL','tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),'seconds':time.monotonic()-started,
            'python':sys.version,'test_patterns':patterns,'app_source':list(app.__path__)}
    destination=Path(a.report);destination.parent.mkdir(parents=True,exist_ok=True);destination.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if result.wasSuccessful() else 1
if __name__=='__main__':sys.exit(main())
