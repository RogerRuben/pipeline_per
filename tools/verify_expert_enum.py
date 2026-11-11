"""Original categorical exports plus explicitly declared-domain test variants.

The supplied exporters omit enum domains. Never relabel the amended variants
as unchanged original exports, nor infer their domains from workbook samples.
"""
import argparse
import copy
import json
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from product.common import read_json, write_json, sha
from product.compatibility import effectiveness_schema_from_document, make_contract, fingerprint
from services.effectiveness_service.expert_packages import read_export

FAMILIES = {'lock': 'r12_1_lock', 'product': 'r12_1_product_export', 'pump': 'r12_1_pump'}


def prepare(out):
    out.mkdir(exist_ok=False)
    for family, folder in FAMILIES.items():
        source = ROOT / 'outputs' / folder
        original = next(source.glob('*.zip'))
        target = out / family
        target.mkdir()
        shutil.copy2(original, target / 'original.zip')
        raw = json.loads(read_export(original.read_bytes()).decode('utf-8'))
        schema_key = 'schema' if family == 'lock' else 'project_schema' if family == 'product' else 'product_schema'
        observed = next(f for f in effectiveness_schema_from_document(raw)['fields'] if f['field_name'] == 'expert_category')
        assert observed['dtype'] == 'enum' and observed['allowed_values'] == []
        # A separate synthetic metadata fixture, not an unmodified UI export.
        # Its complete domain is authored explicitly, not guessed from samples.
        declared = copy.deepcopy(raw)
        declared[schema_key]['fields'] = [{'field_name': 'expert_category', 'allowed_values': ['1', '2']}]
        core = {k: v for k, v in declared.items() if k not in ('model_digest', 'model_version', 'frozen_at')}
        declared['model_digest'] = fingerprint(core)
        declared['model_version'] += '-declared-enum-fixture'
        model_bytes = json.dumps(declared, ensure_ascii=False, indent=2).encode('utf-8')
        with zipfile.ZipFile(original) as src:
            original_contents = {n: src.read(n) for n in src.namelist()}
        contents = dict(original_contents)
        manifest_name = next(n for n in contents if n.endswith('_manifest.json'))
        manifest = json.loads(contents[manifest_name].decode('utf-8'))
        manifest.update(model_digest=declared['model_digest'], model_version=declared['model_version'])
        contents[manifest['model']] = model_bytes
        import hashlib
        for item in manifest['files']:
            if item['path'] == manifest['model']:
                item.update(sha256=hashlib.sha256(model_bytes).hexdigest(), size=len(model_bytes))
        contents[manifest_name] = json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')
        with zipfile.ZipFile(target / 'declared_domain_test.zip', 'x', zipfile.ZIP_DEFLATED) as dest:
            for name, content in contents.items():
                dest.writestr(name, content)
        assert read_export((target / 'declared_domain_test.zip').read_bytes()) == model_bytes
        (target / 'model.json').write_bytes(model_bytes)
        shutil.copy2(source / 'expected.json', target / 'expected.json')
        write_json(target / 'provenance.json', {
            'original_zip_sha256': sha(original),
            'declared_domain_test_sha256': sha(target / 'declared_domain_test.zip'),
            'explicit_test_domain': ['1', '2'],
            'domain_in_original_export': False,
            'algorithm_sources_unchanged': all(contents[n] == original_contents[n] for n in contents if n.endswith('.py')),
            'v15_ui_asset': 'original embedded HTML copied into required export filename' if family == 'product' else None})
    # Existing price release remains immutable; only the isolated test business
    # adds an expert-only enum field, with no additional price input or training.
    release = out / 'price_release'
    shutil.copytree(ROOT / 'outputs/r9_verified_v3', release)
    business_path = release / 'business_release.iprelease.json'
    business = read_json(business_path)
    business['data']['parameters'].append({
        'parameter_id': 'expert_category', 'label': '接口类型', 'unit': '',
        'value_type': 'enum', 'required': 1, 'enabled': 1, 'search_type': 'unordered_enum',
        'allowed_values_json': '["1", "2"]', 'display_order': 100, 'model_bound': 0})
    for row in business['data']['agreements']:
        row['params']['expert_category'] = '1'
    from app.semantic_snapshot import semantic_signature
    business['semantic_signature'] = semantic_signature(business['data'])
    core = {k: business[k] for k in ('format', 'product_code', 'product_name', 'data', 'semantic_schema_version',
                                    'semantic_signature', 'runtime_contract', 'semantic_contract')}
    business['payload_sha256'] = fingerprint(core)
    write_json(business_path, business)
    manifest = read_json(release / 'prototype_manifest.json')
    manifest['artifact_sha256']['business_package'] = sha(business_path)
    write_json(release / 'prototype_manifest.json', manifest)
    write_json(out / 'prepared.json', {'status': 'pass', 'families': list(FAMILIES), 'price_retrained': False})


def package(out, bootstrap):
    from tools.build_product_exe import build, audit_runtime
    from tools.client_builder import free_ports
    from product.launcher import running, fetch
    report = {'checks': {}, 'win7_tested': False, 'cases': {}}

    def check(name, value):
        report['checks'][name] = bool(value)
        write_json(out / 'report.json', report)
        if not value:
            raise AssertionError(name)

    release = out / 'price_release'
    from services.price_service.ds_backend import load_price_service
    manifest = read_json(release / 'prototype_manifest.json')
    price_path = release / manifest['price_model']
    price_schema = load_price_service(price_path, None).schema()
    business = read_json(release / 'business_release.iprelease.json')
    for family in FAMILIES:
        target = out / family
        raw = read_export((target / 'original.zip').read_bytes())
        try:
            make_contract(price_schema, business, sha(price_path), {'unit': 'wan_yuan', 'scale': 1},
                          effectiveness_schema=json.loads(raw), effectiveness_model_sha='test')
        except ValueError as exc:
            check(family + '_original_missing_domain_rejected', 'expert_category' in str(exc))
        else:
            check(family + '_original_missing_domain_rejected', False)
        destination = target / 'software'
        build(release, ROOT / 'deliverables/CB8/runtime/python38', ROOT / 'outputs/p4_build_tools',
              destination, 'r12.1-enum-' + family, bootstrap=bootstrap,
              effectiveness_package=target / 'declared_domain_test.zip', archive=False)
        audit_runtime(destination)
        field = next(f for f in read_json(destination / 'release/delivery_contract.json')['effectiveness_schema']['fields']
                     if f['field_name'] == 'expert_category')
        check(family + '_sealed_contract_domain', field['allowed_values'] == ['1', '2'])
        data = target / 'userdata'
        write_json(data / 'config/desktop.json', free_ports())
        process = subprocess.Popen([str(destination / 'product.exe'), '--no-browser', '--data-dir', str(data)],
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            for _ in range(300):
                current = running(data)
                if current and current[1]['status'] == 'ready':
                    break
                if process.poll() is not None:
                    raise ValueError(family + ' EXE exited')
                time.sleep(.2)
            else:
                raise ValueError(family + ' readiness timeout')
            check(family + '_actual_exe_ready', True)
            base = current[1]['portal_url'][:-7]
            schema = fetch(base + '/api/effectiveness-workbench/schema')
            field = next(f for f in schema['fields'] if f['field_name'] == 'expert_category')
            check(family + '_http_schema_domain', field['allowed_values'] == ['1', '2'])
            records = []
            for case in read_json(target / 'expected.json')['cases']:
                for value in ['1', '2']:
                    params = dict(case['parameters'], expert_category=value)
                    result = fetch(base + '/api/effectiveness-workbench/evaluate',
                                   {'parameters': params, 'target_protocol': case['target_protocol']}, timeout=60)
                    score = result['evaluation']['effectiveness_score']
                    records.append({'enum': value, 'expected': case['score'], 'actual': score})
                    assert abs(score - case['score']) < 1e-6, (family, records[-1])
            report['cases'][family] = records
            check(family + '_enum_predictions_equal_original', True)
            check(family + '_price_model_unchanged', sha(destination / 'release/price_native_bundle.pkl') == sha(price_path))
        finally:
            current = running(data)
            if current:
                fetch(current[0]['url'] + '/api/stop', {}, current[0]['token'])
            process.wait(timeout=30)
    report['status'] = 'pass'
    write_json(out / 'report.json', report)
    print(json.dumps(report))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', required=True)
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--bootstrap')
    args = parser.parse_args()
    target = Path(args.out).resolve()
    if ROOT / 'outputs' not in target.parents:
        raise ValueError('Choose an outputs/ directory')
    if args.prepare:
        prepare(target)
    else:
        package(target, Path(args.bootstrap).resolve())
