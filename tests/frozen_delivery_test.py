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

if __name__=='__main__':unittest.main()

