"""Offline provenance and external port configuration boundaries."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.source_provenance import source_commit
from product.common import write_json,sha
from product.desktop_config import load_config,validate_config,template_config


class ClientKitTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=str(ROOT/'outputs/test_tmp'));self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def test_sealed_offline_sources_need_no_git_or_parent_checkout(self):
        source=self.root/'module.py';source.write_text('pass\n')
        write_json(self.root/'SOURCE_BUILD.json',{'format':'pipeline-source-distribution-1.0',
                   'source_sha':'a'*40,'files':{'module.py':sha(source)}})
        with patch('tools.source_provenance.subprocess.check_output',side_effect=AssertionError('Git must not be called')):
            self.assertEqual(source_commit(self.root),'a'*40)
        source.write_text('changed\n')
        with self.assertRaisesRegex(ValueError,'changed'):source_commit(self.root)
    def test_source_seal_rejects_path_escape(self):
        write_json(self.root/'SOURCE_BUILD.json',{'format':'pipeline-source-distribution-1.0','source_sha':'b'*40,
                   'files':{'../outside.py':'0'*64}})
        with self.assertRaises(ValueError):source_commit(self.root)
    def test_external_user_configuration_overrides_packaged_template(self):
        package=self.root/'package';data=self.root/'userdata';settings=template_config()
        write_json(package/'defaults/desktop.json',settings)
        self.assertEqual(load_config(package,data),settings)
        custom={'ports':{'control':31890,'web':31891,'price':32101,'effectiveness':32102},'open_browser':False}
        write_json(data/'config/desktop.json',custom)
        self.assertEqual(load_config(package,data),custom)
        self.assertEqual(json.loads((package/'defaults/desktop.json').read_text(encoding='utf-8')),settings)
    def test_external_ports_reject_duplicates_invalid_values_and_unknown_keys(self):
        valid={'ports':{'control':31890,'web':31891,'price':32101,'effectiveness':32102}}
        for role,value in [('price',31890),('web',True),('web',-1),('effectiveness',65536)]:
            bad=json.loads(json.dumps(valid));bad['ports'][role]=value
            with self.assertRaises(ValueError):validate_config(bad)
        with self.assertRaises(ValueError):validate_config(dict(valid,public_host='0.0.0.0'))
    def test_ephemeral_diagnostic_ports_are_explicit(self):
        value={'ports':{role:0 for role in ('control','web','price','effectiveness')},'open_browser':False}
        self.assertEqual(validate_config(value),value)


if __name__=='__main__':
    (ROOT/'outputs/test_tmp').mkdir(parents=True,exist_ok=True);unittest.main(verbosity=2)
