# -*- coding: utf-8 -*-
"""Apply the documented pipeline hooks on top of the recorded V21.6 source."""
from pathlib import Path
import subprocess
ROOT=Path(__file__).resolve().parents[1]

def replace(name,old,new):
    path=ROOT/name;text=path.read_text(encoding='utf-8')
    if new in text:return
    if old not in text:raise ValueError('Missing compatibility anchor: '+name+' '+old[:50])
    with path.open('w',encoding='utf-8',newline='') as out:out.write(text.replace(old,new,1))

def main():
    defaults=ROOT/'app/defaults/scenario_config.json'
    if not defaults.is_file():
        defaults.parent.mkdir(parents=True,exist_ok=True)
        defaults.write_bytes(subprocess.check_output(['git','-c','safe.directory='+str(ROOT).replace('\\','/'),'show','3439c7dadf3b3dada10482a2c17d99d3f9853b10:config/scenario_config.json'],cwd=str(ROOT)))
    legacy=ROOT/'app/legacy_gflownet.py'
    if not legacy.is_file():
        legacy.write_bytes(subprocess.check_output(['git','-c','safe.directory='+str(ROOT).replace('\\','/'),'show','7332b0b:app/gflownet_generator.py'],cwd=str(ROOT)))
    gflow=ROOT/'app/gflownet_generator.py';value=gflow.read_text(encoding='utf-8')
    if 'from .legacy_gflownet import TabularTrajectoryBalanceGFlowNet' not in value:
        with gflow.open('a',encoding='utf-8',newline='') as out:out.write('\n# Stable import for earlier exported experiments; V21 uses TabularTBGFlowNet.\nfrom .legacy_gflownet import TabularTrajectoryBalanceGFlowNet\n')
    # Preserve the earlier Excel/CSV fixes while retaining V21.6 business-required semantics.
    wide=ROOT/'app/wide_import.py';current=wide.read_text(encoding='utf-8')
    if 'def read_csv_table(' not in current:
        original=subprocess.check_output(['git','-c','safe.directory='+str(ROOT).replace('\\','/'),'show','7332b0b:app/wide_import.py'],cwd=str(ROOT)).decode('utf-8')
        start=current.index('def _decode_text(');end=current.index('def read_table_bytes(')
        block=original[original.index('def _decode_text('):original.index('def read_table_bytes(')]
        with wide.open('w',encoding='utf-8',newline='') as out:out.write(current[:start]+block+current[end:])
    replace('app/server.py','        return self._enrich_workbench_schema(schema, "effectiveness")',
            '        schema["integration_source"] = os.environ.get("IPDEMO_EFFECT_INTEGRATION_KIND", "")\n        return self._enrich_workbench_schema(schema, "effectiveness")')
    replace('app/server.py','    def _static(self, path):\n',
            '    def _static(self, path):\n        if path.rstrip("/") in ("/cost-effectiveness", "/cost-analysis", "/cost_effectiveness"):\n            item = (self.app.portal_config.get("services") or {}).get("cost_effectiveness_analysis") or {}\n            if item.get("enabled") and item.get("url"):\n                self._redirect(item["url"]); return\n            self._json({"error":"analysis_unavailable", "message":"效费分析台未启动，请使用统一START入口。"},503); return\n')
    replace('app/static/portal.js','var meta={recommendation:',
            'var meta={cost_effectiveness_analysis:{icon:"CE",description:"比较方案的价格、效能与帕累托前沿",action:"进入效费分析台",group:"analysis"},recommendation:')
    replace('app/static/price.html','href="/styles.css"></head>',
            'href="/styles.css"><link rel="stylesheet" href="/price_ensemble.css"></head>')
    replace('app/static/price.html','<script src="/price.js"></script>',
            '<script src="/price_ensemble.js"></script><script src="/price.js"></script>')
    replace('app/static/price.js','function render(r){var p=',
            'function render(r){if(window.PriceEnsemble)window.PriceEnsemble.render(r.explanation||r.price_explanation);var p=')
    replace('app/model_runtime.py','"capability_score": round(capability, 4),',
            '"capability_score": round(capability, 4),\n            "capability_source": self.bundle.get("capability_source", "trial_snapshot"),')

if __name__=='__main__':main()
