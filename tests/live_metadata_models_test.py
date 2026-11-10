"""Regressions for historical pricing, live labels and durable model switching."""
import json,sqlite3,sys,tempfile,unittest,zipfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
import deep_price_contract_test as fixture
from app.store import Store
from app.business_labels import overlay_labels
from app.model_service_client import DegradedServiceRuntime
from product.model_library import ModelLibrary,active_paths
from product.common import write_json,read_json,sha
from product.recovery import backup_data,unpack_backup

class HistoricalTests(unittest.TestCase):
    setUp=fixture.DeepPriceContractTests.setUp
    tearDown=fixture.DeepPriceContractTests.tearDown
    request=fixture.DeepPriceContractTests.request
    get_schema=fixture.DeepPriceContractTests.get_schema
    http=fixture.DeepPriceContractTests.http
    def set_price(self):
        with closing(sqlite3.connect(str(self.db))) as c,c:
            c.execute('ALTER TABLE agreements ADD COLUMN historical_price_wan REAL')
            c.execute("UPDATE agreements SET historical_price_wan=123.45 WHERE agreement_id='A1'")
    def test_unchanged_historical_product_uses_stored_price_but_model_route_is_pure(self):
        self.set_price();request=self.request();request['reference_product_id']='A1'
        status,result=self.http('/reference-price',request)
        self.assertEqual(status,200,result);self.assertEqual(result['predicted_price_wan'],123.45)
        self.assertEqual(result['price_source'],'historical');self.assertIn('result_id',result)
        status,pure=self.http('/predict',self.request());self.assertEqual(pure['predicted_price_wan'],50)
        request['parameters']['size']=6
        status,changed=self.http('/reference-price',request)
        self.assertEqual(status,200,changed);self.assertEqual(changed['predicted_price_wan'],52)
        self.assertEqual(changed['price_source'],'model')
    def test_stale_history_request_cannot_return_a_price_after_business_change(self):
        self.set_price();request=dict(self.request(),reference_product_id='A1')
        with closing(sqlite3.connect(str(self.db))) as c,c:c.execute("UPDATE agreements SET historical_price_wan=999 WHERE agreement_id='A1'")
        self.assertEqual(self.http('/reference-price',request)[0],409)
    def test_deep_http_labels_use_current_database_without_modifying_model(self):
        before=self.model_path.read_bytes()
        with closing(sqlite3.connect(str(self.db))) as c,c:c.execute("UPDATE parameter_definitions SET label='新尺寸中文' WHERE parameter_id='size'")
        status,schema=self.http('/schema');self.assertEqual(status,200,schema)
        self.assertEqual(next(f for f in schema['fields'] if f['field_id']=='size')['label_zh'],'新尺寸中文')
        self.assertEqual(self.model_path.read_bytes(),before)

class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=str(ROOT/'outputs'));self.root=Path(self.temp.name)
        self.store=Store(self.root/'data/protocol_demo.db',self.root/'absent.csv',DegradedServiceRuntime(None,'P','产品'))
        self.store.admin_upsert('parameters',{'parameter_id':'size','label':'尺寸','unit':'mm','value_type':'number','required':1,'enabled':1,'min_value':0,'max_value':100})
        self.app=SimpleNamespace(root=self.root,store=self.store,model_gateway=SimpleNamespace())
        self.library=ModelLibrary(self.app)
    def tearDown(self):self.temp.cleanup()
    def asset(self,asset_id='a'*64):
        folder=self.library.root/asset_id;folder.mkdir()
        write_json(folder/'model.json',{'version':'test'})
        meta={'id':asset_id,'name':'价格模型','role':'price','model_file':'model.json','metadata_file':None,'files':{'model.json':sha(folder/'model.json')},'schema':{'fields':[{'field_name':'size','unit':'mm'}]},'health':{}}
        write_json(folder/'asset.json',meta);write_json(self.library.catalog,[meta]);return meta
    def test_partial_admin_rename_preserves_every_other_column_byte_for_byte(self):
        self.store.admin_upsert('parameters',{'parameter_id':'kind','label':'类别','value_type':'enum','allowed_values_json':'["A", "B"]','required':1,'enabled':1,'search_type':'discrete'})
        before=self.store.parameter_map()['kind'];self.store.admin_upsert('parameters',{'parameter_id':'kind','label':'配置类别新名称'})
        after=self.store.parameter_map()['kind'];self.assertEqual(after.pop('label'),'配置类别新名称');before.pop('label');self.assertEqual(after,before)
    def test_live_label_overlay_changes_names_only(self):
        data={'fields':[{'field_name':'size','field_label':'旧名','unit':'mm','value':42}],'explanation':[{'field_id':'size','label':'旧名','score':5}]}
        result=overlay_labels(data,self.store.parameter_map());self.assertEqual(result['fields'][0]['field_label'],'尺寸')
        self.assertEqual(result['fields'][0]['value'],42);self.assertEqual(data['fields'][0]['field_label'],'旧名')
    def test_model_advisory_strings_follow_field_renames_without_rewriting_values(self):
        data={'fields':[{'field_name':'size','field_label':'旧尺寸'}],'domain_status':{'warnings':['旧尺寸高于训练范围上限100']},'product_name':'旧尺寸','parameters':{'kind':'旧尺寸'}}
        result=overlay_labels(data,self.store.parameter_map())
        self.assertEqual(result['domain_status']['warnings'],['尺寸高于训练范围上限100'])
        self.assertEqual(result['product_name'],'旧尺寸');self.assertEqual(result['parameters']['kind'],'旧尺寸')
    def test_failed_switch_restores_previous_config_even_when_units_present(self):
        self.asset();old={'price':{'id':'b'*64},'effectiveness':{'id':'c'*64}};write_json(self.library.active,old)
        with patch.object(self.library,'_restart',side_effect=ValueError('failure')) as restart:
            with self.assertRaisesRegex(ValueError,'failure'):self.library.activate('price','a'*64)
        self.assertEqual(read_json(self.library.active),old);self.assertEqual(restart.call_count,2)
    def test_tampered_model_is_rejected_before_stopping_service(self):
        self.asset();(self.library.root/('a'*64)/'model.json').write_text('{}')
        with patch.object(self.library,'_restart') as restart:
            with self.assertRaisesRegex(ValueError,'改变'):self.library.activate('price','a'*64)
        restart.assert_not_called();self.assertFalse(self.library.active.exists())
    def test_export_and_backup_restore_preserve_model_library(self):
        self.asset();write_json(self.library.active,{'price':{'id':'a'*64}})
        from io import BytesIO
        with zipfile.ZipFile(BytesIO(self.library.export('a'*64))) as z:self.assertEqual(z.namelist(),['model.json'])
        backup=backup_data(self.root);stage=self.root/'restore';stage.mkdir();records=unpack_backup(backup,stage)
        self.assertIn('models/library/catalog.json',records)
        self.assertEqual((stage/'models/library'/('a'*64)/'model.json').read_bytes(),(self.library.root/('a'*64)/'model.json').read_bytes())
        self.assertEqual(read_json(stage/'config/active_models.json'),{'price':{'id':'a'*64}})
    def test_invalid_role_does_not_touch_active_config(self):
        with self.assertRaisesRegex(ValueError,'角色'):self.library.activate('unknown','default')
        self.assertFalse(self.library.active.exists())

if __name__=='__main__':unittest.main(verbosity=2)
