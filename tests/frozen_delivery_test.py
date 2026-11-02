"""Release boundary regressions: source leakage and V3 provenance scope."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import tools.build_frozen_engine as frozen
from tools.build_product_exe import audit_runtime

class FrozenDeliveryTests(unittest.TestCase):
    def test_runtime_rejects_source_and_development_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'product.exe').write_bytes(b'MZ')
            audit_runtime(root)
            source=root/'business.py';source.write_text('print(1)')
            with self.assertRaisesRegex(ValueError,'source'):audit_runtime(root)
            source.unlink();(root/'payload').mkdir()
            with self.assertRaisesRegex(ValueError,'Development directory'):audit_runtime(root)

    def test_provenance_includes_v3_but_excludes_old_engines_and_demo_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            paths=['product_entry.py','services/__init__.py','app/server.py','product/launcher.py',
                   'services/price_service/app.py','services/price_service/optuna_v2/native_member.py',
                   'services/price_service/optuna_v7/engine.py',
                   'services/effectiveness_service/model/current/source/expert.py']
            for relative in paths:
                p=root/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('# fixture')
            with patch.object(frozen,'ROOT',root):records=frozen.engine_sources()
            self.assertIn(paths[5],records)
            self.assertIn('app/server.py',records)
            self.assertNotIn(paths[6],records)
            self.assertNotIn(paths[7],records)


    def test_only_typed_port_template_is_editable_while_models_remain_sealed(self):
        from product.common import write_json,sha,verify_package
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);model=root/'release/model.bin';model.parent.mkdir();model.write_bytes(b'trusted')
            config=root/'defaults/desktop.json'
            ports={'control':17900,'web':17901,'price':18201,'effectiveness':18202}
            write_json(config,{'ports':ports,'open_browser':False})
            manifest={'format':'pipeline-per-desktop-1.0','files':{'release/model.bin':sha(model),'defaults/desktop.json':sha(config)},
              'release':{k:'release/model.bin' for k in ('price_model','effectiveness_model','business_package','evaluation_metadata')},
              'configurable_files':['defaults/desktop.json']}
            write_json(root/'PACKAGE_MANIFEST.json',manifest)
            ports['web']=17903;write_json(config,{'ports':ports,'open_browser':False})
            verify_package(root,True)
            write_json(config,{'ports':ports,'open_browser':False,'host':'0.0.0.0'})
            with self.assertRaises(ValueError):verify_package(root,True)
            write_json(config,{'ports':ports,'open_browser':False})
            model.write_bytes(b'tampered')
            with self.assertRaisesRegex(ValueError,'integrity'):verify_package(root,True)

if __name__=='__main__':unittest.main()

