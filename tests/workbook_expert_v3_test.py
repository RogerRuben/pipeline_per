"""Keep the existing 1-5 score contract and carry workbook grouping to V3."""
import json
from pathlib import Path
import sys,tempfile,unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.xlsx_utils import write_workbook_bytes
from automl_pipeline.product_price_optuna_v3 import load_config
from automl_pipeline.product_price_optuna_v2 import workbook_config,apply_attribute_groups
from automl_pipeline.__main__ import inspect_input

class WorkbookExpertV3Tests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.source=Path(self.temp.name)/'products.xlsx'
    def tearDown(self):self.temp.cleanup()
    def workbook(self,rows):
        data=[['成品编号','成品名称','价格(万元)','尺寸(mm)'],['product_id','product_name','price_wan','size']]
        data += [['P%d'%i,'产品%d'%i,10+i,i+1] for i in range(8)]
        self.source.write_bytes(write_workbook_bytes([('成品数据',data),('价格属性评分',[
            ['英文代号','价格重要性评分','专家编号','属性类型','属性分组']]+rows)]))
    def test_workbook_priority_median_groups_and_inspection(self):
        self.workbook([['size',3,'A','number','功能'],['size',5,'B','number','功能']])
        base=load_config(overrides={'expert':{'scores':{'size':1},'attribute_groups':{'size':'环境'}}})
        cfg=workbook_config(self.source,base,{'size'},loader=load_config)
        self.assertEqual(cfg['expert']['scores']['size'],4.)
        self.assertEqual(len(cfg['expert']['records']),2)
        data={'parameters':[{'parameter_id':'size'}]};apply_attribute_groups(data,cfg)
        self.assertEqual(data['parameters'][0]['parameter_group'],'功能')
        result=inspect_input(self.source,'optuna_v3',base)
        self.assertEqual(result['expert_configuration']['explicit_scores'],1)
        self.assertEqual(result['expert_configuration']['attribute_groups']['size'],'功能')
    def test_conflicting_groups_are_rejected(self):
        self.workbook([['size',3,'A','number','功能'],['size',5,'B','number','环境']])
        with self.assertRaisesRegex(ValueError,'分组不一致'):workbook_config(self.source,load_config(),{'size'},loader=load_config)
    def test_duplicate_and_out_of_range_and_unknown_fields(self):
        for rows in ([['size',3,'A','',''],['size',4,'A','','']], [['size',100,'A','','']], [['typo',4,'A','','']]):
            with self.subTest(rows=rows):
                self.workbook(rows)
                with self.assertRaises(ValueError):workbook_config(self.source,load_config(),{'size'},loader=load_config)
    def test_group_only_rows_do_not_create_expert_scores(self):
        self.workbook([['size',None,'A','number','性能']])
        cfg=workbook_config(self.source,load_config(),{'size'},loader=load_config)
        self.assertFalse(cfg['expert']['scores']);self.assertEqual(cfg['expert']['attribute_groups']['size'],'性能')
    def test_other_engine_workbook_contract_is_unchanged(self):
        from automl_pipeline.product_price_optuna_v4 import load_config as v4_config
        self.workbook([['size',4,'A','number','功能']])
        cfg=workbook_config(self.source,v4_config(),{'size'},loader=v4_config)
        self.assertEqual(cfg['expert']['scores']['size'],4.)
        self.assertFalse(cfg['expert']['attribute_groups'])

if __name__=='__main__':unittest.main(verbosity=2)
