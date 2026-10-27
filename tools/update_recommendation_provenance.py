"""Record the imported upstream files and explicit local integration changes."""
import hashlib,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def update():
    path=ROOT/'docs/RECOMMENDATION_SOURCE_PROVENANCE_V21_6.json';raw=json.loads(path.read_text(encoding='utf-8'))
    raw['pipeline_adjustments']=[
        'Preserve dual Chinese/English headers, CSV encodings/delimiters and workbook diagnostics.',
        'Keep the native price ensemble explanation panel and saved model compatibility imports.',
        'Preserve conservative recommendation prices while retaining nominal price display.',
        'Use a bundled scenario default only when user/workspace configuration is absent.',
        'Link the real read-only cost service and resolve its return link to the current portal.',
        'Record the imported effectiveness component source; service ownership lives in the pipeline, not this frontend.']
    names={row['path'] for row in raw['files']}|{'app/defaults/scenario_config.json','app/legacy_gflownet.py'}
    raw['integrated_sha256']={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sorted(names)}
    raw['original_price_core_sha256']=hashlib.sha256((ROOT/'services/price_service/optuna_v2/price_automl_core.py').read_bytes()).hexdigest()
    path.write_text(json.dumps(raw,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

if __name__=='__main__':update()
