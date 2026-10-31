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
import copy

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from product.common import InstanceLock,contained,data_directory,read_json,sha,verify_package,write_json
from product.launcher import Supervisor,backup_data,fetch
from product.worker import initialize_data
from product.compatibility import make_contract,accept_initial,validate_data,fingerprint
from product.recovery import restore_backup


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
            connection.executescript('CREATE TABLE parameter_definitions(parameter_id TEXT,unit TEXT,value_type TEXT,search_type TEXT,required INTEGER,enabled INTEGER,min_value REAL,max_value REAL);'
                                     'CREATE TABLE product_releases(release_id TEXT,product_code TEXT,status TEXT,data_json TEXT);'
                                     'CREATE TABLE model_input_bindings(model_kind TEXT,parameter_id TEXT,enabled INTEGER);')
            parameter={'parameter_id':'size','unit':'mm','value_type':'number','search_type':'continuous',
                       'required':1,'enabled':1,'min_value':0,'max_value':100}
            self.business={'product_code':'P','source_release_id':'SOURCE-1','payload_sha256':'published-payload',
                           'data':{'parameters':[parameter]}}
            connection.execute('INSERT INTO parameter_definitions VALUES(?,?,?,?,?,?,?,?)',
                               tuple(parameter[key] for key in ('parameter_id','unit','value_type','search_type','required','enabled','min_value','max_value')))
            connection.execute('INSERT INTO product_releases VALUES(?,?,?,?)',('REL-1',code,'active',json.dumps(self.business['data'])))
            connection.commit()
        model=self.package/'release/model.pkl';model.parent.mkdir();model.write_bytes(b'unit-test-artifact')
        self.contract=make_contract({'model_version':'v1','feature_order':['size'],
             'fields':[{'field_name':'size','unit':'mm','dtype':'number','required':True,'missing_policy':'reject'}]},
             self.business,sha(model),{'unit':'wan_yuan','scale':1.0})
        self.manifest={'version':'desktop-test','release':{'product_code':'P','price_model':'release/model.pkl',
                       'delivery_contract':'release/delivery_contract.json'},'files':{'release/model.pkl':sha(model)}}
        self.seal_contract()
        write_json(self.data/'config/model_services.json',{'price_output':{'unit':'wan_yuan','scale':1.0}})
        write_json(self.data/'installed.json',{'version':'desktop-test','data_schema':1})
        if code=='P':write_json(self.data/'delivery_state.json',accept_initial(self.data,self.manifest,self.contract))
        return path

    def seal_contract(self):
        path=self.package/'release/delivery_contract.json';write_json(path,self.contract)
        self.manifest['files']['release/delivery_contract.json']=sha(path)

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
        config=self.data/'config';config.mkdir(exist_ok=True)
        rule=config/'deep_price_adjustment.json';rule.write_text('{"confirmed":true}',encoding='utf-8')
        before=sha(database),rule.read_bytes()
        initialize_data(self.package,self.data,self.manifest)
        self.assertEqual((sha(database),rule.read_bytes()),before)

    def test_wrong_product_and_corrupt_existing_db_fail_without_replacement(self):
        database=self.database('OTHER');before=database.read_bytes()
        with self.assertRaisesRegex(ValueError,'incompatible'):
            initialize_data(self.package,self.data,self.manifest)
        self.assertEqual(database.read_bytes(),before)
        database.write_bytes(b'corrupt-existing-user-data')
        with self.assertRaises(sqlite3.DatabaseError):
            initialize_data(self.package,self.data,self.manifest)
        self.assertEqual(database.read_bytes(),b'corrupt-existing-user-data')

    def test_live_backup_is_consistent_and_excludes_runtime_secrets(self):
        database=self.database()
        write_json(self.data/'config/deep_price_adjustment.json',{'confirmed':False,'sentinel':'保留'})
        write_json(self.data/'runtime/control.json',{'token':'must-not-export'})
        archive=backup_data(self.data)
        with zipfile.ZipFile(str(archive)) as bundle:
            self.assertTrue({'data/protocol_demo.db','config/deep_price_adjustment.json',
                             'config/model_services.json','delivery_state.json','BACKUP_MANIFEST.json'}<=set(bundle.namelist()))
            self.assertNotIn('runtime/control.json',bundle.namelist())
            snapshot=self.root/'snapshot.db';snapshot.write_bytes(bundle.read('data/protocol_demo.db'))
        with closing(sqlite3.connect(str(snapshot))) as connection:
            self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(connection.execute('SELECT value FROM user_sentinel').fetchone()[0],'用户保存的方案')

    def test_compatible_retrained_model_accepts_new_sha_without_writing_database(self):
        database=self.database();before=sha(database)
        self.contract.update(model_sha256='b'*64,model_version='v2')
        self.manifest['files']['release/model.pkl']='b'*64;self.seal_contract()
        state=validate_data(self.package,self.data,self.manifest)
        self.assertEqual(state['model_sha256'],'b'*64)
        self.assertEqual(sha(database),before)

    def test_same_product_changed_model_schema_is_rejected_before_database_write(self):
        database=self.database();before=sha(database)
        self.contract['model_schema']['fields'][0]['missing_policy']='training_mean'
        self.contract['model_schema_sha256']=fingerprint(self.contract['model_schema']);self.seal_contract()
        with self.assertRaisesRegex(ValueError,'Replacement model schema'):
            validate_data(self.package,self.data,self.manifest)
        self.assertEqual(sha(database),before)

    def test_price_unit_and_release_identity_changes_are_not_silently_accepted(self):
        self.database()
        write_json(self.data/'config/model_services.json',{'price_output':{'unit':'yuan','scale':1.0}})
        with self.assertRaisesRegex(ValueError,'unit/scale'):validate_data(self.package,self.data,self.manifest)
        write_json(self.data/'config/model_services.json',{'price_output':{'unit':'wan_yuan','scale':1.0}})
        with closing(sqlite3.connect(str(self.data/'data/protocol_demo.db'))) as conn:
            conn.execute("UPDATE product_releases SET release_id='REL-CHANGED'");conn.commit()
        with self.assertRaisesRegex(ValueError,'active_release_id'):validate_data(self.package,self.data,self.manifest)

    def test_live_field_mapping_drift_from_published_release_is_rejected(self):
        self.database()
        with closing(sqlite3.connect(str(self.data/'data/protocol_demo.db'))) as conn:
            conn.execute("UPDATE parameter_definitions SET unit='cm'");conn.commit()
        with self.assertRaisesRegex(ValueError,'activated release'):validate_data(self.package,self.data,self.manifest)

    def test_legacy_requires_explicit_verified_predecessor_approval(self):
        self.database();(self.data/'delivery_state.json').unlink()
        write_json(self.data/'runtime/initialized.json',{'business_package_sha256':'legacy-business-sha'})
        with self.assertRaisesRegex(ValueError,'approved predecessor'):validate_data(self.package,self.data,self.manifest)
        self.contract['compatible_legacy']=[{'version':'desktop-test','business_package_sha256':'legacy-business-sha'}]
        self.seal_contract()
        self.assertEqual(validate_data(self.package,self.data,self.manifest)['active_release_id'],'REL-1')

    def test_restore_checks_real_database_config_and_can_be_validated_again(self):
        self.database();write_json(self.data/'config/deep_price_adjustment.json',{'confirmed':False,'note':'恢复后保留'})
        archive=backup_data(self.data);target=self.root/'restored-userdata'
        result=restore_backup(self.package,target,self.manifest,archive)
        self.assertEqual(result['status'],'restored')
        self.assertEqual(validate_data(self.package,target,self.manifest)['active_release_id'],'REL-1')
        self.assertEqual(read_json(target/'config/deep_price_adjustment.json')['note'],'恢复后保留')
        with closing(sqlite3.connect(str(target/'data/protocol_demo.db'))) as conn:
            self.assertEqual(conn.execute('SELECT value FROM user_sentinel').fetchone()[0],'用户保存的方案')
        before=sha(target/'data/protocol_demo.db')
        with self.assertRaisesRegex(ValueError,'new or empty'):restore_backup(self.package,target,self.manifest,archive)
        self.assertEqual(sha(target/'data/protocol_demo.db'),before)

    def test_restore_rejects_traversal_and_tampering_before_publishing_database(self):
        self.database();archive=backup_data(self.data)
        for kind in ('path','sha'):
            corrupt=self.root/(kind+'.zip')
            with zipfile.ZipFile(str(archive)) as source,zipfile.ZipFile(str(corrupt),'w') as destination:
                for name in source.namelist():
                    raw=source.read(name)
                    if name=='data/protocol_demo.db' and kind=='sha':raw=b'tampered'
                    destination.writestr(name,raw)
                if kind=='path':destination.writestr('../escape.txt',b'no')
            target=self.root/('restore-'+kind)
            with self.assertRaises(ValueError):restore_backup(self.package,target,self.manifest,corrupt)
            self.assertFalse((target/'data/protocol_demo.db').exists())
            self.assertFalse((self.root/'escape.txt').exists())

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
