"""Desktop boundary tests. Actual frozen-process acceptance is a separate tool."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request,build_opener,ProxyHandler
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from product.common import InstanceLock,contained,data_directory,read_json,sha,verify_package,write_json
from product.launcher import Supervisor,backup_data,fetch
from product.worker import initialize_data


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=str(ROOT/'outputs/test_tmp'))
        self.root=Path(self.tmp.name)
        self.package=self.root/'package';self.package.mkdir()
        self.data=self.root/'userdata';self.data.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def database(self,code='P'):
        path=self.data/'data/protocol_demo.db';path.parent.mkdir(exist_ok=True)
        with closing(sqlite3.connect(str(path))) as connection:
            connection.executescript("CREATE TABLE products(product_code TEXT,enabled INTEGER);"
                                     "CREATE TABLE user_sentinel(value TEXT);")
            connection.execute('INSERT INTO products VALUES (?,1)',(code,))
            connection.execute('INSERT INTO user_sentinel VALUES (?)',('用户保存的方案',))
            connection.commit()
        return path

    def test_data_path_is_independent_and_override_is_explicit(self):
        with patch.dict(os.environ,{'LOCALAPPDATA':str(self.root/'local')},clear=True):
            self.assertEqual(data_directory(self.package),self.root/'local/PipelinePer')
            self.assertEqual(data_directory(self.package,str(self.data)),self.data)
        for path in (self.package,self.package/'userdata',self.root):
            with self.assertRaises(ValueError):data_directory(self.package,str(path))

    def test_manifest_cannot_escape_or_accept_changed_model(self):
        release=self.package/'release';release.mkdir()
        model=release/'model.pkl';model.write_bytes(b'trusted-test-bytes')
        artifact='release/model.pkl'
        write_json(self.package/'PACKAGE_MANIFEST.json',{'format':'pipeline-per-desktop-1.0',
                   'files':{artifact:sha(model)},'release':{key:artifact for key in
                   ('price_model','effectiveness_model','business_package','evaluation_metadata')}})
        verify_package(self.package)
        model.write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError,'integrity'):verify_package(self.package)
        with self.assertRaises(ValueError):contained(self.package,'../secret')

    def test_existing_data_and_confirmed_config_never_reseeded(self):
        database=self.database()
        config=self.data/'config';config.mkdir()
        rule=config/'deep_price_adjustment.json';rule.write_text('{"confirmed":true}',encoding='utf-8')
        before=sha(database),rule.read_bytes()
        initialize_data(self.package,self.data,{'release':{'product_code':'P'}})
        self.assertEqual((sha(database),rule.read_bytes()),before)

    def test_wrong_product_and_corrupt_existing_db_fail_without_replacement(self):
        database=self.database('OTHER');before=database.read_bytes()
        with self.assertRaisesRegex(ValueError,'does not match'):
            initialize_data(self.package,self.data,{'release':{'product_code':'P'}})
        self.assertEqual(database.read_bytes(),before)
        database.write_bytes(b'corrupt-existing-user-data')
        with self.assertRaises(sqlite3.DatabaseError):
            initialize_data(self.package,self.data,{'release':{'product_code':'P'}})
        self.assertEqual(database.read_bytes(),b'corrupt-existing-user-data')

    def test_live_backup_is_consistent_and_excludes_runtime_secrets(self):
        database=self.database()
        write_json(self.data/'config/deep_price_adjustment.json',{'confirmed':False,'sentinel':'保留'})
        write_json(self.data/'runtime/control.json',{'token':'must-not-export'})
        archive=backup_data(self.data)
        with zipfile.ZipFile(str(archive)) as bundle:
            self.assertEqual(set(bundle.namelist()),{'data/protocol_demo.db','config/deep_price_adjustment.json'})
            snapshot=self.root/'snapshot.db';snapshot.write_bytes(bundle.read('data/protocol_demo.db'))
        with closing(sqlite3.connect(str(snapshot))) as connection:
            self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(connection.execute('SELECT value FROM user_sentinel').fetchone()[0],'用户保存的方案')

    def test_instance_lock_is_exclusive_and_os_released(self):
        lock=InstanceLock(self.data)
        program='import sys;sys.path.insert(0,'+repr(str(ROOT))+');from product.common import InstanceLock;InstanceLock('+repr(str(self.data))+')'
        result=subprocess.run([sys.executable,'-c',program],capture_output=True)
        self.assertNotEqual(result.returncode,0)
        lock.close()
        result=subprocess.run([sys.executable,'-c',program],capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_control_rejects_missing_token_cross_origin_and_rebinding_host(self):
        supervisor=Supervisor(self.package,self.data,{'version':'test','release':{'classification':'synthetic'}},True)
        worker=threading.Thread(target=supervisor.server.serve_forever,daemon=True);worker.start()
        opener=build_opener(ProxyHandler({}))
        try:
            status=fetch(supervisor.url+'/api/status')
            self.assertNotIn('token',status)
            for headers in ({},{'X-Product-Token':supervisor.token,'Origin':'https://attacker.example'},
                            {'X-Product-Token':supervisor.token,'Host':'attacker.example'},
                            {'X-Product-Token':supervisor.token,'Sec-Fetch-Site':'cross-site'}):
                with self.assertRaises(HTTPError) as failure:
                    opener.open(Request(supervisor.url+'/api/stop',data=b'{}',headers=headers))
                self.assertEqual(failure.exception.code,403)
                self.assertFalse(supervisor.stopping.is_set())
            fetch(supervisor.url+'/api/stop',{},supervisor.token)
            self.assertTrue(supervisor.stopping.is_set())
        finally:
            supervisor.server.shutdown();supervisor.server.server_close();worker.join();supervisor.job.close()


if __name__=='__main__':
    (ROOT/'outputs/test_tmp').mkdir(parents=True,exist_ok=True)
    unittest.main(verbosity=2)
