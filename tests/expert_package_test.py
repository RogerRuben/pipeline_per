import io,json,sys,tempfile,unittest,zipfile,hashlib,subprocess,os
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from services.effectiveness_service import expert_packages as expert
from product.common import read_json
from product.model_library import ModelLibrary
from app.model_service_client import ModelServiceGateway
class ExpertTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.package=next((ROOT/'outputs/r12_pump_acceptance').glob('*.zip'));cls.raw=cls.package.read_bytes()
 def test_original_zip_preserves_model_bytes_and_never_imports_expert_into_web(self):
  before=expert._family;model=expert.read_export(self.raw)
  self.assertEqual(model,(ROOT/'outputs/r12_pump_acceptance/model.json').read_bytes());self.assertEqual(expert._family,before)
 def mutate(self,fn):
  stream=io.BytesIO()
  with zipfile.ZipFile(io.BytesIO(self.raw)) as z,zipfile.ZipFile(stream,'w') as dest:
   files={n:z.read(n) for n in z.namelist()};fn(files)
   for name,data in files.items():dest.writestr(name,data)
  return stream.getvalue()
 def test_corrupt_original_source_is_rejected(self):
  bad=self.mutate(lambda f:f.update({'source/project_excel.py':b'bad'}))
  with self.assertRaisesRegex(ValueError,'校验失败'):expert.read_export(bad)
 def test_changed_algorithm_even_with_rehashed_manifest_is_rejected(self):
  def change(f):
   key='source/project_excel.py';f[key]+=b'\n# altered runtime\n';name='system_effectiveness_runtime_manifest.json';d=json.loads(f[name].decode())
   for row in d['files']:
    if row['path']==key:row.update(sha256=hashlib.sha256(f[key]).hexdigest(),size=len(f[key]))
   f[name]=json.dumps(d).encode()
  with self.assertRaisesRegex(ValueError,'升级软件'):expert.read_export(self.mutate(change))
 def test_path_traversal_rejected_without_extracting(self):
  with self.assertRaisesRegex(ValueError,'路径不安全'):expert.read_export(self.mutate(lambda f:f.update({'../escape.py':b'x'})))
 def test_actual_frozen_expert_engine_loads_and_scores(self):
  with tempfile.TemporaryDirectory(dir=str(ROOT/'outputs')) as tmp:
   report=Path(tmp)/'report.json'
   engine=os.environ.get('PIPELINE_TEST_EXPERT_EXE',str(ROOT/'outputs/r12_expert_release/effect_worker.exe'))
   p=subprocess.run([engine,'--asset-check','effectiveness',str(ROOT/'outputs/r12_pump_acceptance/model.json'),'-',str(report)],timeout=60)
   self.assertEqual(p.returncode,0);d=read_json(report);self.assertEqual(d['status'],'pass');self.assertTrue(d['probe']['raw_expert_result'])
 def test_model_library_installs_export_data_without_python_source(self):
  with tempfile.TemporaryDirectory(dir=str(ROOT/'outputs')) as tmp:
   app=SimpleNamespace(root=Path(tmp));library=ModelLibrary(app)
   asset=library.install('effectiveness','原泵导出包',self.package.name,self.raw)
   folder=library.root/asset['id'];self.assertFalse(list(folder.rglob('*.py')));self.assertEqual(asset['health']['backend'],'original_pump_v13')
   exported=library.export(asset['id'])
   with zipfile.ZipFile(io.BytesIO(exported)) as z:self.assertEqual(z.namelist(),['model.json'])
 def test_lock_unknown_feasibility_is_not_fabricated_as_zero_or_one(self):
  effect={'evaluation':{'effectiveness_score':100,'physical_feasibility_evaluated':False,'feasibility_probability':None}}
  result=ModelServiceGateway._merge_core({'parameters':{}},effect,10,None,[],[],{})
  self.assertIsNone(result['feasibility_probability']);self.assertIsNone(result['physical_gate']['passed']);self.assertIsNone(result['anomaly_assessment']['score'])
if __name__=='__main__':unittest.main(verbosity=2)
