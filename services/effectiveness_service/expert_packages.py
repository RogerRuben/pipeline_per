"""Original one-click exports: verified data + bundled, unmodified expert runtime."""
import hashlib,importlib,importlib.abc,importlib.util,io,json,sys,zipfile
from pathlib import PurePosixPath,Path
from .expert_runtime.source_hashes import HASHES,NORM_HASHES,REQUIRED

FORMATS={
 'lock-product-reuse-model-17.0':('lock','frozen_lock_model','FrozenLockReuseRuntime'),
 'system-effectiveness-frozen-model-13.0':('pump','frozen_system_model','FrozenSystemRuntime'),
 'effectiveness-frozen-model-15.0':('pump','frozen_effectiveness_model','FrozenEffectivenessRuntime')}
PACKAGES={'lock-product-reuse-runtime-package-17.0':'lock-product-reuse-model-17.0',
 'system-effectiveness-frozen-runtime-package-13.0':'system-effectiveness-frozen-model-13.0',
 'effectiveness-frozen-runtime-package-15.0':'effectiveness-frozen-model-15.0'}
def kind(d):return d.get('model_format') or d.get('model_format_version')
_family=None
class AliasLoader(importlib.abc.Loader):
 def __init__(self,family,name):self.family,self.name=family,name
 def create_module(self,spec):return importlib.import_module('services.effectiveness_service.expert_runtime.'+self.family+'.'+self.name)
 def exec_module(self,module):pass
class AliasFinder(importlib.abc.MetaPathFinder):
 def __init__(self,family):self.family=family;self.names={Path(n).stem for n in HASHES if n.startswith(family+'/')}
 def find_spec(self,fullname,path=None,target=None):
  if fullname in self.names:return importlib.util.spec_from_loader(fullname,AliasLoader(self.family,fullname))
def module(family,name):
 global _family
 if _family and _family!=family:raise ValueError('不同专家运行时必须在独立服务进程中切换')
 if not _family:
  finder=AliasFinder(family)
  for key in finder.names:
   if key in sys.modules:raise ValueError('专家模块名称已被其他运行时占用：'+key)
  sys.meta_path.insert(0,finder);_family=family
 return importlib.import_module('services.effectiveness_service.expert_runtime.'+family+'.'+name)
def source_difference(family,rel,raw):
 """Compare one exported source file with the builtin runtime. Never executes it."""
 key=family+'/'+PurePosixPath(rel).name
 norm=hashlib.sha256(raw.replace(b'\r\n',b'\n')).hexdigest()
 if key not in NORM_HASHES:
  return {'path':rel,'status':'unknown_runtime_file','consistency':'unverified'}
 if NORM_HASHES[key]!=norm:
  return {'path':rel,'status':'source_differs_from_builtin','consistency':'unverified'}
 return {'path':rel,'status':'matches_builtin','consistency':'verified'}
def read_export(content):
 """Return raw frozen JSON, or None for a non-expert ZIP. No code is executed."""
 read_export.source_report=[]
 with zipfile.ZipFile(io.BytesIO(content)) as z:
  entries=z.infolist()
  if len(entries)>2000 or sum(e.file_size for e in entries)>400*1024*1024:raise ValueError('效能导出包体积超限')
  names={}
  for e in entries:
   n=e.filename.replace('\\','/');p=PurePosixPath(n)
   if p.is_absolute() or '..' in p.parts or ':' in n:raise ValueError('效能包路径不安全')
   if n.casefold() in names:raise ValueError('效能包存在重复路径')
   names[n.casefold()]=e
  manifests=[]
  for e in entries:
   if e.filename.endswith('_manifest.json'):
    d=json.loads(z.read(e).decode('utf-8-sig'))
    if d.get('format_version') in PACKAGES:manifests.append((e,d))
  if not manifests:return None
  if len(manifests)!=1:raise ValueError('一次只导入一个效能导出包')
  entry,manifest=manifests[0];parent=PurePosixPath(entry.filename).parent
  def read(rel):
   p=PurePosixPath(str(rel).replace('\\','/'))
   if p.is_absolute() or '..' in p.parts or ':' in str(rel):raise ValueError('效能清单路径不安全')
   return z.read(names[(parent/p).as_posix().casefold()])
  data=read(manifest['model']);document=json.loads(data.decode('utf-8-sig'));model_kind=PACKAGES[manifest['format_version']]
  if kind(document)!=model_kind:raise ValueError('效能模型格式与导出清单不一致')
  family=FORMATS[model_kind][0];files=manifest.get('files') or []
  declared=set();sources=set()
  for item in files:
   rel=item['path'];raw=read(rel)
   if hashlib.sha256(raw).hexdigest()!=item['sha256'] or item.get('size',len(raw))!=len(raw):raise ValueError('效能原包文件校验失败：'+rel)
   declared.add(rel)
   if rel.endswith('.py'):
    read_export.source_report.append(source_difference(family,rel,raw))
    sources.add(PurePosixPath(rel).name)
  if manifest['model'] not in declared:raise ValueError('效能清单未校验模型文件')
  if any(item.get('consistency')=='unverified' for item in read_export.source_report):
   read_export.consistency='unverified'
  else:read_export.consistency='verified'
  required=set(REQUIRED[model_kind])
  verified_names={PurePosixPath(rel).name for rel in declared}
  if not {n for n in required if n.endswith('.py')}<=sources or not required<=verified_names:raise ValueError('效能导出包缺少原运行时文件')
  for key in ('model_digest','model_version','algorithm_version'):
   if manifest.get(key) is not None and manifest[key]!=document.get(key):raise ValueError('效能导出清单标识不一致：'+key)
  return data

class ExpertBackend:
 name='original_frozen_expert'
 def __init__(self,path):
  self.model_path=Path(path);self.raw=json.loads(self.model_path.read_text(encoding='utf-8-sig'));self.kind=kind(self.raw)
  family,name,cls=FORMATS[self.kind];mod=module(family,name);self.runtime=getattr(mod,cls)(self.model_path)
  self.lock=self.kind.startswith('lock-');self.system=self.kind.startswith('system-')
  self.name='original_lock_v17' if self.lock else 'original_pump_v13' if self.system else 'original_product_v15'
  self.project=self.runtime.space.project if self.lock else self.runtime.system_project.product if self.system else self.runtime.app.project
  self.model_version=self.raw['model_version'];self.algorithm_version=self.raw['algorithm_version']
 def schema(self):
  profiles=(self.raw.get('evaluation_baselines') or [self.raw.get('baseline',{})]) if self.lock else self.raw.get('system_contract',{}).get('tasks',[]) if self.system else []
  reference=(self.raw.get('baseline') or {}).get('params',{}) if self.lock else (self.raw.get('product_schema') or self.raw.get('project_schema') or {}).get('reference_params',{})
  fields=[{'field_name':a.key,'field_label':a.label,'dtype':{'continuous':'number','integer':'integer','categorical':'enum'}.get(a.data_type,'number'),'required':True,'default_value':reference.get(a.key),'unit':a.unit,'generation_min':a.generation_min,'generation_max':a.generation_max,'missing_policy':'reject'} for a in self.project.attributes]
  from product.compatibility import effectiveness_schema_from_document
  contracts={f['field_name']:f for f in effectiveness_schema_from_document(self.raw)['fields']}
  for field in fields:
   for key in ('allowed_values','categories','category_mapping'):
    field[key]=contracts[field['field_name']][key]
  ids=[{'id':p.get('profile_id') or p.get('task_id'),'profile_id':p.get('profile_id') or p.get('task_id'),'profile_name':p.get('profile_name') or p.get('task_name'),'name':p.get('profile_name') or p.get('task_name'),'requirements':p.get('requirements',[])} for p in profiles]
  active=(self.raw.get('baseline') or {}).get('profile_id') if self.lock else self.runtime.state.get('active_system_task_id') if self.system else self.runtime.app.active_requirement_profile(self.runtime.state).id
  return {'product_code':self.project.product_code,'product_name':self.project.project_name,'model_version':self.model_version,'algorithm_version':self.algorithm_version,'profile_version':17 if self.lock else 13 if self.system else 15,'backend':self.name,'fields':fields,'reference_parameters':reference,'protocol_profiles':ids,'active_protocol':{'id':active,'profile_id':active},'capabilities':{'physical_feasibility':not self.lock,'dynamic_target_protocol':False,'counterfactual_improvement':False},'target_protocol_contract':{'supported':True,'mode':'exported_profiles_only','accepted':['profile_id','task_id'],'changes_learning_state':False},'state_sha256':self.raw['model_digest'],'training_summary':self.raw.get('training_summary',{}),'privacy':self.raw.get('privacy',{}),'learning_fingerprint':self.project.learning_fingerprint}
 def evaluate(self,params,target_protocol=None):
  selector=(target_protocol.get('task_id') or target_protocol.get('profile_id') or target_protocol.get('id')) if isinstance(target_protocol,dict) else target_protocol
  if isinstance(target_protocol,dict) and set(target_protocol)-{'task_id','profile_id','id','name','profile_name','requirement_overrides'}:raise ValueError('请使用包内已导出的评价基准或任务')
  if self.lock:raw=self.runtime.evaluate(params,profile_id=selector)
  elif self.system:raw=self.runtime.evaluate(params,task_id=selector,requirement_overrides=(target_protocol or {}).get('requirement_overrides') if isinstance(target_protocol,dict) else None)
  else:
   app=self.runtime.app;raw=app.evaluate(params,self.runtime.state,requirement_profile=app.project.requirement_profile_by_id().get(selector) if selector else None)
  score=raw['effectiveness_score'];interval=(raw.get('system_assessment') or {}).get('protocol_score_interval') if self.system else raw.get('effectiveness_interval') or (raw.get('requirement_assessment') or {}).get('protocol_score_interval')
  probability=None if self.lock else raw.get('learned_feasibility_probability')
  from .app import _physical_gate,_capability_contributors
  assessment=raw if self.lock else raw.get('system_assessment') if self.system else raw.get('requirement_assessment') or {}
  if self.system:
   assessment=dict(assessment)
   assessment['attributes']=[dict(a,attribute_key=a['system_key'],attribute_label=a['system_attribute'],current_value=a.get('value'),reference_value=a.get('requirement_value'),relative_score_percent=a.get('score'),score_delta=a.get('total_point_contribution'),weight=a.get('group_weight')) for a in assessment.get('direct_assessment',{}).get('attributes',[])]
  return {'parameters':dict(params),'effectiveness_score':score,'capability_score':score,'conservative_capability_score':interval[0] if interval else score,'protocol_score_interval':interval,'feasibility_probability':probability,'physical_feasibility_evaluated':not self.lock,'physical_gate':{'passed':None,'decision':'not_evaluated','probability':None,'reason':'原锁 V17 模型不评价物理可行性'} if self.lock else _physical_gate(probability,raw.get('status'),raw.get('hard_violations') or [],raw.get('learned_boundary_violations') or [],raw.get('coupling_assessments') or []),'feasibility_status':'not_evaluated' if self.lock else raw.get('status'),'effectiveness_source':self.algorithm_version,'requirement_assessment':assessment,'coupling_assessments':raw.get('coupling_assessments') or [],'hard_violations':raw.get('hard_violations') or [],'learned_boundary_violations':raw.get('learned_boundary_violations') or [],'experience_extrapolations':raw.get('experience_extrapolations') or [],'capability_contributors':_capability_contributors(assessment),'protocol':{'profile_id':selector or self.schema()['active_protocol']['id'],'source':'original_exported_expert_model'},'raw_expert_result':raw}

def load_backend(path):
 from .app import SnapshotBackend
 d=json.loads(Path(path).read_text(encoding='utf-8-sig'))
 return ExpertBackend(path) if kind(d) in FORMATS else SnapshotBackend(path)
