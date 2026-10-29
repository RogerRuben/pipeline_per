# -*- coding: utf-8 -*-
"""The two source areas, legacy imports and source-only CLI work together."""
import ast
import importlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import app
app.__path__.insert(0,str(ROOT/'app'))
from tools.pipeline_source_layout import MODULES,SOURCE_FILES


class SourceLayoutTests(unittest.TestCase):
    def test_legacy_imports_are_the_canonical_modules(self):
        for name in MODULES:
            with self.subTest(name=name):
                old=importlib.import_module('tools.'+name);new=importlib.import_module('automl_pipeline.'+name)
                self.assertIs(old,new)
                self.assertEqual(Path(new.__file__).parent,ROOT/'automl_pipeline')
    def test_patching_old_module_reaches_real_implementation(self):
        from tools import product_pipeline as old
        from automl_pipeline import product_pipeline as new
        with patch('tools.product_pipeline.ROOT',Path('example')):
            self.assertEqual(new.ROOT,Path('example'));self.assertIs(old,new)
    def test_source_package_files_are_present_and_in_patch_manifest(self):
        from tools.upgrade_product_v4 import PATCH_FILES
        for name in SOURCE_FILES:self.assertTrue((ROOT/name).is_file(),name);self.assertIn(name,PATCH_FILES)
    def test_dependency_checker_rejects_missing_canonical_implementation(self):
        from tools.product_patch_dependencies import validate_local_imports
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError,'automl_pipeline.product_pipeline'):
                validate_local_imports({'tools/product_pipeline.py':(ROOT/'tools/product_pipeline.py').read_bytes()},Path(folder))
    def test_all_default_configs_validate_without_private_state(self):
        from automl_pipeline.__main__ import configuration
        for engine in ('optuna_v2','optuna_v3','optuna_v4','optuna_v5','optuna_v6','optuna_v7'):
            cfg=configuration(engine,None);self.assertEqual(cfg['input']['min_samples'],4)
            self.assertEqual(cfg['schema_version'],'price-'+engine.replace('_','-')+'-pipeline-1.0')
    def test_fingerprints_reference_real_training_sources(self):
        tree=ast.parse((ROOT/'automl_pipeline/product_optuna_v2_input.py').read_text(encoding='utf-8'))
        sources=[node.s for node in ast.walk(tree) if isinstance(node,ast.Str) and node.s.startswith('automl_pipeline/') and node.s.endswith('.py')]
        self.assertGreaterEqual(len(sources),5)
        for name in sources:self.assertTrue((ROOT/name).is_file(),name)
    def test_recommendation_app_does_not_import_the_training_pipeline(self):
        for path in (ROOT/'app').glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
                names=[item.name for item in node.names] if isinstance(node,ast.Import) else [node.module or ''] if isinstance(node,ast.ImportFrom) else []
                self.assertFalse(any(n.startswith('automl_pipeline') for n in names),str(path))
    def test_current_readme_links_resolve_in_the_repository(self):
        for relative in ('README.md','automl_pipeline/README.md','app/README.md','docs/CODEBASE_LAYOUT.md'):
            path=ROOT/relative
            for link in re.findall(r'\]\(([^)]+)\)',path.read_text(encoding='utf-8')):
                if '://' not in link and not link.startswith('#'):self.assertTrue((path.parent/link.split('#')[0]).exists(),relative+': '+link)
    def test_source_cli_generates_then_inspects_without_portable_configuration(self):
        from automl_pipeline.__main__ import main
        with tempfile.TemporaryDirectory() as folder:
            cfg=Path(folder)/'price.json'
            self.assertEqual(main(['config','--profile','quick','--output',str(cfg)]),0)
            value=json.loads(cfg.read_text(encoding='utf-8'));self.assertEqual(value['search']['n_trials'],40)
            self.assertEqual(value['v7']['mode'],'balanced');self.assertEqual(value['v7']['consistency']['mode'],'off');self.assertEqual(value['v7']['budget']['max_extra_fits'],128)
            self.assertEqual(main(['inspect',str(ROOT/'automl_pipeline/examples/virtual_16x36.xlsx'),'--price-config',str(cfg)]),0)
            original=cfg.read_bytes()
            with self.assertRaises(SystemExit) as raised:main(['config','--output',str(cfg)])
            self.assertEqual(raised.exception.code,1);self.assertEqual(original,cfg.read_bytes())


if __name__=='__main__':unittest.main(verbosity=2)
