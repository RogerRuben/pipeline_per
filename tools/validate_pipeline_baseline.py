"""Read-only structural/provenance checks; no model deserialization or services.

Python 3.8+ standard library only. This is not a Win7/model compatibility test.
"""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    errors = []
    checks = []

    def check(condition, message):
        (checks if condition else errors).append(message)

    def read(name):
        return json.loads((ROOT / name).read_text(encoding='utf-8'))

    profile = read('config/build_profile.json')
    baseline = read('docs/audit/source_baseline.json')
    assets = read('docs/audit/attachments.json')
    api = read('docs/contracts/deep_price.openapi.json')
    check(profile['price_engine'] == 'optuna_v3', 'explicit Optuna V3 profile')
    check(profile['runtime_prediction'] == 'http_price_service_only', 'HTTP-only analysis prediction policy')
    check(profile['stage'] in ('P0', 'P1-1', 'P1-2', 'V3-validation') and profile['deep_price_enabled'] == (profile['stage'] in ('P1-2','V3-validation')), 'UI capability matches implemented stage')
    check(profile['mixed_adjustment'] == 'unsupported', 'mixed pricing disabled')
    check(profile['userdata_policy'] == 'external_persistent', 'persistent data separate from distribution')
    check(profile['source_commit'] == baseline['commit'], 'profile source commit matches baseline')
    check(len(assets['archives']) == 2, 'two original archive identities recorded')
    check(len(assets['legacy_routes']) == 20, 'all 20 legacy routes inventoried')
    included = []
    for member in assets['members']:
        if member['included_as_reference']:
            name = 'references/' + member['asset'] + '/' + member['path']
            p = ROOT / name
            check(p.is_file() and hashlib.sha256(p.read_text(encoding='utf-8').encode('utf-8')).hexdigest() == member['reference_sha256'], 'reference normalized UTF-8/LF hash: ' + name)
            included.append(name)
    check(any(p.endswith('/templates/index.html') for p in included), 'index visual reference preserved')
    check(bool(assets['redactions']), 'legacy API key fallback removed and recorded')
    forbidden = {'.pkl', '.pickle', '.pyc', '.npy', '.csv', '.xlsx', '.rar', '.zip'}
    for p in (ROOT / 'references').rglob('*'):
        if p.is_file():
            check(p.suffix.lower() not in forbidden, 'reference text-only: ' + str(p.relative_to(ROOT)))
            text = p.read_text(encoding='utf-8')
            token = re.search(r'\bsk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY', text)
            check(not token, 'no known credential pattern: ' + str(p.relative_to(ROOT)))
    check(api['openapi'] == '3.1.0', 'OpenAPI 3.1 draft')
    check(api['servers'][0]['url'] == '/api/deep-price/v1', 'namespaced analysis APIs')
    check(len(api['paths']) == 13, '13 proposed paths present')
    check(not any('upload' in p or 'active_model' in p for p in api['paths']), 'no analysis model mutation paths')

    def walk(value):
        if isinstance(value, dict):
            if '$ref' in value:
                target = value['$ref']
                node = api
                try:
                    assert target.startswith('#/')
                    for key in target[2:].split('/'):
                        node = node[key]
                except (KeyError, AssertionError):
                    errors.append('unresolved API ref: ' + target)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(api)
    check(not errors, 'API internal references resolved')
    # P0 preserves all business code; P1 permits only documented integration,
    # loaded-model identity metadata, and the reproduced startup defect fix.
    proc = subprocess.run(['git', 'diff', '--name-only', baseline['commit'], '--', 'app', 'automl_pipeline', 'services'], cwd=str(ROOT), capture_output=True, text=True)
    allowed = {'app/server.py', 'app/model_service_client.py', 'app/data_master.py',
               'services/price_service/app.py', 'services/price_service/native_bundle.py'} if profile['stage'] in ('P1-1','P1-2','V3-validation') else set()
    if profile['stage'] in ('P1-2','V3-validation'):
        allowed.update({'app/configuration.py','app/generation_tasks.py','services/common/http_service.py'})
    changed = proc.stdout.splitlines()
    check(proc.returncode == 0 and all(p in allowed or (profile['stage'] in ('P1-1','P1-2','V3-validation') and p.startswith('app/deep_price/')) or (profile['stage'] in ('P1-2','V3-validation') and p.startswith('app/static/deep_price/')) for p in changed), 'source changes limited to documented stage scope')
    report = {'status': 'pass' if not errors else 'fail', 'checks_passed': len(checks), 'errors': errors, 'python': sys.version.split()[0], 'scope': profile['stage'] + ' structural/provenance checks only; no inference, browser, EXE or Win7 acceptance'}
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
