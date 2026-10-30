"""Run P1 HTTP/compatibility checks using a selected installed interpreter.

No pip install, training, or production DB access. Reports go to logs/.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def source_fingerprint():
    h = hashlib.sha256()
    files = []
    for directory in ('app', 'services', 'tests'):
        files.extend((ROOT/directory).rglob('*.py'))
    for p in sorted(files):
        h.update(p.relative_to(ROOT).as_posix().encode('utf-8'))
        h.update(p.read_text(encoding='utf-8-sig').encode('utf-8'))
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--python', default=sys.executable)
    args = parser.parse_args()
    temp = ROOT/'outputs/test_tmp'
    temp.mkdir(parents=True, exist_ok=True)
    logs = ROOT/'logs'
    logs.mkdir(exist_ok=True)
    # Use distribution lookup, available in Python3.8.
    env_program = "import sys,platform,json,importlib.metadata as m\np={}\nfor n in ('optuna','numpy','scipy','scikit-learn'):\n try:p[n]=m.version(n)\n except m.PackageNotFoundError:pass\nprint(json.dumps({'python':sys.version,'platform':platform.platform(),'packages':p},ensure_ascii=True))"
    env = subprocess.run([args.python, '-c', env_program], cwd=str(ROOT), capture_output=True, text=True)
    if env.returncode:
        print(env.stderr)
        return 1
    report = {'time_utc':datetime.now(timezone.utc).isoformat(),
              'source_sha':subprocess.check_output(['git','rev-parse','HEAD'],cwd=str(ROOT),text=True).strip(),
              'source_tree_sha256':source_fingerprint(), 'environment':json.loads(env.stdout),
              'model':'trusted generated LinearFixture + existing virtual portable JSON, not formal V3',
              'win7_validated':False, 'checks':[]}
    for name in ('deep_price_contract_test.py','portable_compatibility_test.py','price_dynamic_model_hotfix_test.py'):
        program = 'import tempfile,runpy;tempfile.tempdir='+repr(str(temp))+';runpy.run_path('+repr(str(ROOT/'tests'/name))+",run_name='__main__')"
        result = subprocess.run([args.python,'-X','utf8','-c',program],cwd=str(ROOT),capture_output=True,text=True,encoding='utf-8',errors='replace')
        (logs/('p1_'+name+'.log')).write_text(result.stdout+result.stderr,encoding='utf-8')
        report['checks'].append({'command':'python tests/'+name,'status':'pass' if result.returncode==0 else 'fail','exit_code':result.returncode})
        count = re.search(r'Ran (\d+) tests?', result.stderr)
        if count:
            report['checks'][-1]['tests_count'] = int(count.group(1))
        print(name, report['checks'][-1]['status'], flush=True)
    report['status'] = 'pass' if all(c['exit_code']==0 for c in report['checks']) else 'fail'
    identity = logs/'deep_price_fixture_identity.json'
    if identity.is_file():
        report['fixture_identity'] = json.loads(identity.read_text(encoding='utf-8'))
    target = logs/'p1_validation_report.json'
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True,indent=2))
    return 0 if report['status']=='pass' else 1


if __name__ == '__main__':
    sys.exit(main())
