# -*- coding: utf-8 -*-
"""Import verified V21.6 recommendation sources; preserve pipeline input compatibility."""
import hashlib,json,shutil,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
REFERENCE='3439c7dadf3b3dada10482a2c17d99d3f9853b10'
COST_REFERENCE='16ab95512f18f818a65572d982095dbd1e9622bf'


def git(*args):return subprocess.check_output(['git','-c','safe.directory='+ROOT.as_posix()]+list(args),cwd=str(ROOT))


def main():
    backup=ROOT/'outputs/v6_20261027/app_before_v21_snapshot'
    if backup.exists():raise ValueError('Recommendation migration snapshot already exists; do not overwrite it.')
    shutil.copytree(str(ROOT/'app'),str(backup),ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    retained={'app/console_compat.py','app/product_table.py','app/xlsx_utils.py','app/static/price_ensemble.js','app/static/price_ensemble.css'}
    files=git('ls-tree','-r','--name-only',REFERENCE,'app').decode().splitlines();provenance=[]
    for name in files:
        if name in retained or Path(name).suffix not in ('.py','.html','.js','.css'):continue
        payload=git('show',REFERENCE+':'+name);target=ROOT/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(payload)
        provenance.append({'path':name,'upstream_sha256':hashlib.sha256(payload).hexdigest()})
    onboarding=ROOT/'app/historical_onboarding.py';text=onboarding.read_text(encoding='utf-8')
    text=text.replace('from .wide_import import read_table_bytes, split_tags','from .wide_import import read_table_bytes, split_tags\nfrom .product_table import product_table_rows')
    old='''    def analyze(self, filename, raw, product_code, product_name, missing_tokens=None):
        rows = read_table_bytes(filename, raw)
        rows = [row for row in rows if any(_clean(value) for value in row)]
        if len(rows) < 2:
            raise ValueError("历史成品表至少需要表头和一条数据。")
        headers = [_clean(value) for value in rows[0]]'''
    new='''    def analyze(self, filename, raw, product_code, product_name, missing_tokens=None, require_dual_header=False):
        table = product_table_rows(filename, raw, require_dual_header=require_dual_header)
        headers = [_clean(value) for value in table["labels"]]
        field_ids = table["field_ids"]'''
    if old not in text:raise ValueError('V21.6 onboarding shape differs from reviewed source.')
    text=text.replace(old,new).replace('normalized_headers = [_header_key(value) for value in headers if value]','normalized_headers = [_header_key(value) for value in (field_ids or headers) if value]')
    text=text.replace('data_rows = rows[1:]','data_rows = table["rows"]').replace('parameter_id = _parameter_id(header, len(attribute_columns) + 1, used_ids)','parameter_id = field_ids[index] if field_ids else _parameter_id(header, len(attribute_columns) + 1, used_ids)')
    text=text.replace('"warnings": warnings,','"warnings": warnings, "header_rows": table["header_rows"], "field_ids": field_ids, "input_diagnostics": table.get("diagnostics"),')
    onboarding.write_text(text,encoding='utf-8')
    # The cost-effectiveness service is independent and read-only. Import its
    # actual implementation, not a placeholder link or replacement calculator.
    costfiles=git('ls-tree','-r','--name-only',COST_REFERENCE,'cost_effectiveness_analysis').decode().splitlines()
    for name in costfiles:
        if Path(name).suffix not in ('.py','.html','.js','.css'):continue
        payload=git('show',COST_REFERENCE+':'+name);target=ROOT/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(payload)
        provenance.append({'path':name,'upstream_sha256':hashlib.sha256(payload).hexdigest()})
    for name in ('tests/v21_workflow_portal_workbench_test.py','docs/V21_WORKFLOW_PORTAL_WORKBENCH.md','docs/COST_EFFECTIVENESS_ANALYSIS.md'):
        target=ROOT/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(git('show',COST_REFERENCE+':'+name))
    # Version-specific decision support checks are copied with their source.
    tests=git('ls-tree','-r','--name-only',REFERENCE,'tests').decode().splitlines()
    for name in tests:
        if any(k in name for k in ('v21_6','v21_semantic','v21_business')):
            (ROOT/name).write_bytes(git('show',REFERENCE+':'+name))
    result={'recommendation_version':'V21.6','recommendation_reference':REFERENCE,'cost_effectiveness_reference':COST_REFERENCE,
        'retained_pipeline_compatibility':sorted(retained),'files':provenance,
        'note':'Original input/console fixes retained; pipeline service management and native model classes are independent.'}
    (ROOT/'docs/RECOMMENDATION_SOURCE_PROVENANCE_V21_6.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'version':'V21.6','source_files':len(provenance),'reference':REFERENCE}))


if __name__=='__main__':main()
