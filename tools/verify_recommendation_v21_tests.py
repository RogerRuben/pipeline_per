# -*- coding: utf-8 -*-
"""Run upstream decision/analysis regression with separate test-only dependencies."""
import argparse,json,sys,time,runpy
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import app
app.__path__.insert(0,str(ROOT/'app'))

def main():
    p=argparse.ArgumentParser();p.add_argument('--deps',required=True);p.add_argument('--report',required=True);a=p.parse_args()
    sys.path.insert(0,str(Path(a.deps).resolve()));import pytest
    start=time.monotonic()
    code=pytest.main(['-q','--tb=short','-p','no:cacheprovider',str(ROOT/'cost_effectiveness_analysis/tests'),
        str(ROOT/'tests/v21_6_decision_support_semantics_test.py')])
    if code==0:
        tests=runpy.run_path(str(ROOT/'tests/v21_workflow_portal_workbench_test.py'))
        for name in ('static_contracts','deterministic_history_contract','workbench_enrichment_contract'):tests[name]()
    Path(a.report).write_text(json.dumps({'status':'PASS' if code==0 else 'FAIL','seconds':time.monotonic()-start,'pytest_exit_code':int(code),
        'scope':'26 upstream decision/cost regressions plus 3 frontend/workbench contracts; root deployment BATs are not part of the pipeline entry point'},indent=2),encoding='utf-8')
    return code

if __name__=='__main__':sys.exit(main())
