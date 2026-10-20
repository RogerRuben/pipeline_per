# -*- coding: utf-8 -*-
import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.product_patch_dependencies import validate_local_imports
from tools.upgrade_product_r43 import upgrade,PATCH_FILES
from tools.product_price_automl_r4 import load_config


class PatchDependencyTests(unittest.TestCase):
    @unittest.skipUnless((ROOT/'deliverables/ProductAutoML_R4_3_PATCH_20261015.zip').is_file(),'Optional historical offline ZIP is not included in a source checkout')
    def test_original_delivered_zip_has_reproducible_missing_dependency(self):
        with zipfile.ZipFile(ROOT/'deliverables/ProductAutoML_R4_3_PATCH_20261015.zip') as z:payload={n:z.read(n) for n in z.namelist()}
        self.assertNotIn('tools/price_attribute_input.py',payload)
        with self.assertRaisesRegex(ValueError,'tools.price_attribute_input'):
            validate_local_imports(payload,ROOT/'deliverables/product_r41_portable/ProductAutoML_R4_1_20261013')
    def test_working_tree_cannot_hide_a_dependency_missing_from_package(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError,'tools.price_attribute_input'):
                validate_local_imports({'tools/input.py':b'from tools.price_attribute_input import field_index\n'},Path(directory))
    def test_packaged_dependency_resolves_against_base_without_new_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);(base/'app').mkdir();(base/'app/product_table.py').write_text('# supplied by base',encoding='utf-8')
            payload={'tools/input.py':b'from tools.price_attribute_input import field_index\n','tools/price_attribute_input.py':(ROOT/'tools/price_attribute_input.py').read_bytes()}
            result=validate_local_imports(payload,base);self.assertEqual(result['missing'],[])
    def test_missing_relative_import_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError,'app.missing'):
                validate_local_imports({'app/consumer.py':b'from .missing import x\n'},Path(directory))
    def test_repair_does_not_rewrite_custom_settings_input_or_model(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);(base/'config').mkdir()
            config=load_config(overrides={'expert':{'scores':{'x0':5}},'seed_search':{'max_trials':9231},'resources':{'per_search_seconds':73.}})
            raw=(json.dumps(config,ensure_ascii=False,indent=3)+'\n').encode('utf-8')
            path=base/'config/price_automl_r4.json';path.write_bytes(raw);(base/'config/portable.json').write_text('{}',encoding='utf-8')
            for relative in PATCH_FILES:
                if relative.endswith(('.py','.js')):
                    file=base/relative;file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(b'# placeholder\n')
            sentinels={'input/products.xlsx':b'original workbook','state/latest.json':b'original pointer','outputs/model.pkl':b'original model'}
            for relative,data in sentinels.items():
                file=base/relative;file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(data)
            mp=base/'PACKAGE_MANIFEST.json';mp.write_text(json.dumps({'immutable_files':[{'path':'untouched.py','size':1,'sha256':'keep-original'}]}),encoding='utf-8')
            upgrade(base,repair_only=True)
            self.assertEqual(path.read_bytes(),raw)
            for relative,data in sentinels.items():self.assertEqual((base/relative).read_bytes(),data)
            entries={r['path']:r for r in json.loads(mp.read_text(encoding='utf-8'))['immutable_files']}
            self.assertEqual(entries['untouched.py']['sha256'],'keep-original')
            self.assertEqual(entries['tools/price_attribute_input.py']['sha256'],hashlib.sha256(b'# placeholder\n').hexdigest())


if __name__=='__main__':unittest.main(verbosity=2)
