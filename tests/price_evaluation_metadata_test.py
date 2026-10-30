import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from services.price_service.app import PriceService


class MetadataTests(unittest.TestCase):
    def setUp(self):
        (ROOT/'outputs').mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=str(ROOT/'outputs'))
        self.path=Path(self.temp.name)/'evaluation.json'
        self.model=ROOT/'models/price_bundle.json'
        self.metadata={'schema_version':'price-model-evaluation-metadata-1.0',
                       'price_model_sha256':hashlib.sha256(self.model.read_bytes()).hexdigest(),
                       'source_training_report_sha256':'a'*64,
                       'metrics':{'validation_protocol':'joint_searched_split','validation_metrics':{'r2':.9},'business_accuracy_certified':False}}
    def tearDown(self):self.temp.cleanup()
    def save(self):self.path.write_text(json.dumps(self.metadata),encoding='utf-8')
    def test_bound_report_preserves_actual_metric_names_and_scope(self):
        self.save();service=PriceService(None,self.model,evaluation_metadata=self.path)
        meta=service.analysis_metadata()
        self.assertEqual(meta['source'],'bound_training_report')
        self.assertEqual(meta['metrics']['validation_protocol'],'joint_searched_split')
        self.assertEqual(meta['metrics']['validation_metrics']['r2'],.9)
        self.assertFalse(meta['metrics']['business_accuracy_certified'])
        self.assertIsNone(meta['feature_importance'])
    def test_wrong_model_binding_is_rejected(self):
        self.metadata['price_model_sha256']='0'*64;self.save()
        with self.assertRaises(ValueError):PriceService(None,self.model,evaluation_metadata=self.path)
    def test_missing_provenance_is_rejected(self):
        del self.metadata['source_training_report_sha256'];self.save()
        with self.assertRaises(ValueError):PriceService(None,self.model,evaluation_metadata=self.path)


if __name__=='__main__':unittest.main(verbosity=2)
