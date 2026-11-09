import json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.ds_examples import workbook
from tools.build_ds_release import build
from services.price_service.ds_backend import DSPriceService
from services.common.http_service import JsonServiceError
from product.common import read_json

class DSIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=self.root/'products.xlsx';self.source.write_bytes(workbook(ROOT/'examples/ds/DEMO_model.json'))
        self.config={'model_file':str(ROOT/'examples/ds/DEMO_model.json'),'field_map':{}}
    def tearDown(self):self.temp.cleanup()
    def test_import_without_training_and_excel_identity(self):
        with patch('automl_pipeline.product_price_optuna_v2.train_price_optuna_v2',side_effect=AssertionError('must not train')):
            m=build(self.source,self.root/'release','DS_TEST','JSON名称',self.config,True)
        self.assertEqual(m['product_name'],'三层价格演练')
        svc=DSPriceService(self.root/'release'/m['price_model'])
        params={'component_type':'型号1','component_count':1,'alarm':False,'interface_count':0,'accuracy':'一级','communication':'标准','enhanced':False}
        result=svc.handle_post('/api/v1/predict',{'parameters':params})
        self.assertEqual(result['prediction']['predicted_price_wan'],4.5)
        self.assertEqual(result['ds_result'],dict(read_json(ROOT/'examples/ds/DEMO_output.json'),sample_id='单条预测'))
        self.assertEqual(sum(result['ds_result']['layers'].values()),45000.)
        batch=svc.handle_post('/api/v1/predict/batch',{'items':[{'parameters':params},{'parameters':dict(params,component_count=2)}]})
        self.assertEqual(len(batch['items']),2)
        with self.assertRaises(JsonServiceError):svc.handle_post('/api/v1/predict',{'parameters':dict(params,alarm=None)})
        with self.assertRaises(JsonServiceError):svc.handle_post('/api/v1/predict',{'parameters':params,'expected_model_sha256':'old'})
    def test_inspection_is_read_only_and_mapping_complete(self):
        result=build(self.source,None,'DS_TEST','fallback',self.config,True,inspect_only=True)
        self.assertFalse(result['training']);self.assertEqual(result['records'],8)
        self.assertEqual([p.name for p in self.root.iterdir()],['products.xlsx'])
        self.config['field_map']={'component_count':'部件数量'}
        with self.assertRaises(ValueError):build(self.source,None,'DS_TEST','fallback',self.config,True,inspect_only=True)

if __name__=='__main__':unittest.main(verbosity=2)
