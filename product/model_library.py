"""Durable model assets for frozen and source deployments. Never modifies business data."""
import base64,hashlib,json,os,re,shutil,subprocess,sys,tempfile,threading,time,uuid,zipfile,io
from pathlib import Path
from product.common import read_json,write_json,sha,contained

LOCK=threading.RLock()

def library_root(data):return Path(data)/'models/library'
def active_paths(package,data,release,only_role=None):
    active=read_json(Path(data)/'config/active_models.json') if (Path(data)/'config/active_models.json').exists() else {}
    result={}
    for role,key in [('price','price_model'),('effectiveness','effectiveness_model')]:
        if only_role and role!=only_role:continue
        item=active.get(role)
        if item:
            if not re.fullmatch('[0-9a-f]{64}',item.get('id','')):raise ValueError('Invalid library ID')
            folder=library_root(data)/item['id'];meta=read_json(folder/'asset.json')
            if meta.get('role')!=role:raise ValueError('模型库类型不匹配')
            if meta.get('model_file') not in meta['files'] or (meta.get('metadata_file') and meta['metadata_file'] not in meta['files']):raise ValueError('模型库存在未校验资产')
            for name,digest in meta['files'].items():
                if sha(contained(folder,name))!=digest:raise ValueError('模型库文件校验失败：'+name)
            result[role]=contained(folder,meta['model_file']);result[role+'_metadata']=contained(folder,meta['metadata_file']) if meta.get('metadata_file') else None
        else:
            result[role]=contained(package,release[key]);result[role+'_metadata']=contained(package,release['evaluation_metadata']) if role=='price' and release.get('evaluation_metadata') else None
    return result

def inspect_asset(role,path,metadata,report):
    try:
        if role=='price':
            from services.price_service.ds_backend import load_price_service
            service=load_price_service(path,None,**({'evaluation_metadata':metadata} if metadata else {}))
        elif role=='effectiveness':
            from services.effectiveness_service.app import EffectivenessService,SnapshotBackend
            service=EffectivenessService(SnapshotBackend(path))
        else:raise ValueError('不支持的模型类型')
        schema=service.schema();health=service.health()
        write_json(report,{'status':'pass','schema':schema,'health':health})
        return 0
    except Exception as exc:
        write_json(report,{'status':'fail','message':str(exc)});return 1

class ModelLibrary:
    def __init__(self,app):
        self.app=app;self.data=Path(app.root);self.root=library_root(self.data);self.root.mkdir(parents=True,exist_ok=True)
        self.catalog=self.root/'catalog.json';self.active=self.data/'config/active_models.json'
    def entries(self):return read_json(self.catalog) if self.catalog.exists() else []
    def list(self):
        active=read_json(self.active) if self.active.exists() else {}
        from app.business_labels import overlay_labels
        items=overlay_labels(self.entries(),self.app.store.parameter_map())
        current={}
        for role in ('price','effectiveness'):
            try:current[role]={'schema':self.app.model_gateway.price_schema() if role=='price' else self.app.model_gateway.effectiveness_schema()}
            except Exception:current[role]={'schema':{},'status':'unavailable'}
        return {'current_metadata':current,'items':[dict(m,active=(active.get(m['role']) or {}).get('id')==m['id']) for m in items],
                'active':{r:(active.get(r) or {}).get('id','default') for r in ('price','effectiveness')},'storage':str(self.root)}
    def _prepare_ds(self,document,mapping):
        if document.get('format')=='pipeline-ds-release-1.0':return document
        if isinstance(document.get('model'),dict) and 'catalog' in document['model']:document=document['model']
        if 'catalog' not in document or not str(document.get('version','')).startswith('ds-price-'):return None
        from services.price_service.ds_engine import _DSModel
        specs=json.loads(_DSModel(document).schema_json())['required'];defs=self.app.store.parameter_map()
        mapping=mapping or {k:v['label'] for k,v in defs.items() if v['label'] in {s['name'] for s in specs}}
        if set(mapping)-set(defs) or set(mapping.values())!={s['name'] for s in specs} or len(mapping)!=len(specs):raise ValueError('DS映射不完整。模型要求：'+'、'.join(s['name'] for s in specs))
        fields=[]
        for key,name in mapping.items():
            spec=next(s for s in specs if s['name']==name);d=defs[key]
            kind={'正整数':'integer','数值':'number','布尔':'boolean'}.get(spec['type'],'enum' if spec.get('allowed_values') else 'string')
            fields.append({'field_name':key,'field_label':d['label'],'ds_name':name,'dtype':kind,'unit':d.get('unit') or '',
                           'required':True,'missing_policy':'reject','allowed_values':spec.get('allowed_values') or [],
                           'generation_min':1 if kind=='integer' else d.get('min_value'),'generation_max':d.get('max_value')})
        return {'format':'pipeline-ds-release-1.0','product_code':self.app.store.current_product_code(),
                'product_name':self.app.store.bootstrap()['product']['product_name'],'model':document,'fields':fields,
                'reference_parameters':(self.app.store.workbench_example() or {}).get('parameters',{})}
    def install(self,role,name,filename,content,mapping=None):
        if role not in ('price','effectiveness'):raise ValueError('模型类型不正确')
        if not isinstance(content,bytes) or len(content)>100*1024*1024:raise ValueError('模型文件过大')
        with LOCK,tempfile.TemporaryDirectory(prefix='install-',dir=str(self.root)) as temp:
            temp=Path(temp);files={}
            if filename.lower().endswith('.zip'):
                with zipfile.ZipFile(io.BytesIO(content)) as z:
                    if len(z.infolist())>2000 or sum(i.file_size for i in z.infolist())>400*1024*1024:raise ValueError('模型包解压体积过大')
                    for entry in z.infolist():
                        p=Path(entry.filename.replace('\\','/'))
                        if p.is_absolute() or '..' in p.parts or ':' in entry.filename:raise ValueError('模型包路径不安全')
                        if p.suffix.lower() in ('.json','.pkl'):
                            if p.name in files:raise ValueError('模型包存在重复文件名：'+p.name)
                            files[p.name]=z.read(entry)
            else:
                if Path(filename).suffix.lower() not in ('.json','.pkl'):raise ValueError('请选择模型JSON、标准pickle或模型ZIP')
                files[Path(filename).name]=content
            candidates=[]
            if role=='price':
                preferred=next((k for k in ('price_native_bundle.pkl','ds_release.json','DS_model.json') if k in files),None)
                if preferred:candidates=[preferred]
                else:
                    for k,v in files.items():
                        if k.endswith('.pkl'):candidates.append(k);continue
                        try:d=json.loads(v.decode('utf-8-sig'))
                        except Exception:continue
                        if isinstance(d,dict) and ('catalog' in d or (isinstance(d.get('model'),dict) and 'catalog' in d['model']) or 'recommendation_contract_version' in d or d.get('format')=='pipeline-ds-release-1.0'):candidates.append(k)
            else:
                preferred=next((k for k in ('effectiveness_bundle.json','effectiveness_model.json') if k in files),None)
                if preferred:candidates=[preferred]
                else:
                    for k,v in files.items():
                        if not k.endswith('.json'):continue
                        try:d=json.loads(v.decode('utf-8-sig'))
                        except Exception:continue
                        if isinstance(d,dict) and ('capability_model' in d or 'recommendation_contract_version' in d):candidates.append(k)
            if len(candidates)!=1:raise ValueError('需要一个明确的模型文件，找到：'+','.join(candidates))
            source_name=candidates[0];raw=files[source_name];model_name='model.pkl' if source_name.endswith('.pkl') else 'model.json'
            if role=='price' and model_name.endswith('.json'):
                d=json.loads(raw.decode('utf-8-sig'));wrapped=self._prepare_ds(d,mapping)
                if wrapped:raw=json.dumps(wrapped,ensure_ascii=False,allow_nan=False).encode('utf-8');model_name='ds_release.json'
            (temp/model_name).write_bytes(raw);metadata_name=None
            if role=='price' and model_name.endswith('.pkl') and 'evaluation_metadata.json' in files:
                d=json.loads(files['evaluation_metadata.json'].decode('utf-8-sig'))
                if d.get('price_model_sha256')!=sha(temp/model_name):raise ValueError('评价元数据不属于该模型')
                metadata_name='evaluation_metadata.json';(temp/metadata_name).write_bytes(files[metadata_name])
            if role=='price' and model_name.endswith('.pkl') and not metadata_name and 'training_report.json' in files:
                training=json.loads(files['training_report.json'].decode('utf-8-sig'))
                if training.get('engine')=='optuna_v3':
                    metadata_name='evaluation_metadata.json'
                    keys=('selected_training_metrics','full_refit_training_metrics','deployed_training_metrics','validation_metrics','validation_protocol','evaluation_scope','deployment_scope','training_rows','feature_count','trials_completed')
                    metadata={'schema_version':'price-model-evaluation-metadata-1.0','price_model_sha256':sha(temp/model_name),
                              'source_training_report_sha256':hashlib.sha256(files['training_report.json']).hexdigest(),
                              'metrics':{k:training[k] for k in keys if training.get(k) is not None},
                              'feature_importance':None,'metric_units':{'validation_metrics':{'mape':'ratio','mae':'wan_yuan','rmse':'wan_yuan'},'selected_training_metrics':{'mape_percent':'percent'},'full_refit_training_metrics':{'mape_percent':'percent'},'deployed_training_metrics':{'mape_percent':'percent'}}}
                    write_json(temp/metadata_name,metadata)
            output=temp/'check.json' 
            if getattr(sys,'frozen',False):command=[sys.executable,'--asset-check',role,str(temp/model_name),str(temp/metadata_name) if metadata_name else '-',str(output)]
            else:command=[sys.executable,'-B',str(Path(__file__).resolve().parents[1]/'product_entry.py'),'--asset-check',role,str(temp/model_name),str(temp/metadata_name) if metadata_name else '-',str(output)]
            with (temp/'check.log').open('wb') as log:
                p=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=90,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            result=read_json(output) if output.exists() else {'message':'模型依赖或格式无法加载，请使用与当前运行时兼容的导出包'}
            if p.returncode or result.get('status')!='pass':raise ValueError(result.get('message','模型检查失败'))
            asset_id=hashlib.sha256(role.encode()+raw).hexdigest();asset_files={model_name:sha(temp/model_name)}
            if metadata_name:asset_files[metadata_name]=sha(temp/metadata_name)
            meta={'id':asset_id,'role':role,'name':str(name or filename),'model_file':model_name,'metadata_file':metadata_name,
                  'files':asset_files,'schema':result['schema'],'health':result['health'],'bytes':sum((temp/n).stat().st_size for n in asset_files)}
            destination=self.root/asset_id
            with LOCK:
                destination.mkdir(exist_ok=True)
                for n in asset_files:shutil.copy2(str(temp/n),str(destination/n))
                write_json(destination/'asset.json',meta)
                entries=[e for e in self.entries() if e['id']!=asset_id];entries.append(meta);write_json(self.catalog,entries)
            return meta
    def _restart(self,role):
        from product.supervisor import fetch
        control=read_json(self.data/'runtime/control.json');op=fetch(control['url']+'/api/services/'+role+'/restart',{},control['token'])
        deadline=time.monotonic()+70
        while time.monotonic()<deadline:
            s=fetch(control['url']+'/api/status')
            operation=next((o for o in s.get('operations',[]) if o['id']==op['operation_id']),{})
            if operation.get('status')=='succeeded':
                if hasattr(self.app,'sync_desktop_addresses'):self.app.sync_desktop_addresses()
                return
            if operation.get('status')=='failed':raise ValueError(operation.get('error','服务未启动'))
            time.sleep(.2)
        raise ValueError('服务重启超时')
    def activate(self,role,asset_id):
        with LOCK:
            if role not in ('price','effectiveness'):raise ValueError('无效角色')
            old=read_json(self.active) if self.active.exists() else {};new=dict(old)
            if asset_id=='default':new.pop(role,None)
            else:
                entry=next((e for e in self.entries() if e['id']==asset_id and e['role']==role),None)
                if not entry:raise ValueError('模型不存在')
                folder=self.root/asset_id;sealed=read_json(folder/'asset.json')
                if any(sha(contained(folder,n))!=h for n,h in sealed['files'].items()):raise ValueError('模型资产已被改变，请重新检查导入')
                defs=self.app.store.parameter_map()
                required={f.get('field_name') or f.get('key') for f in entry['schema'].get('fields',[]) if f.get('required',True) and (f.get('field_name') or f.get('key'))}
                missing=required-set(defs)
                if missing:raise ValueError('当前数据缺少模型要求的英文字段：'+','.join(sorted(missing)))
                aliases={'毫米':'mm','厘米':'cm','米':'m','千克':'kg','公斤':'kg','兆帕':'MPa','帕':'Pa','摄氏度':'℃'}
                for f in entry['schema'].get('fields',[]):
                    key=f.get('field_name') or f.get('key');old_unit=(defs.get(key) or {}).get('unit') or '';unit=f.get('unit') or ''
                    if old_unit and unit and aliases.get(old_unit,old_unit)!=aliases.get(unit,unit):raise ValueError('模型单位不匹配：'+str(key)+'（当前 '+old_unit+'，新模型 '+unit+'）')
                new[role]={'id':asset_id}
            write_json(self.active,new)
            try:
                self._restart(role)
                from app.model_service_client import ServiceBackedRuntime
                self.app._bind_runtime(ServiceBackedRuntime(self.app.model_gateway,schemas=self.app.model_gateway.schemas(),local_runtime=getattr(self.app,'local_runtime',None)))
                self.app.on_business_data_changed()
            except Exception:
                write_json(self.active,old)
                try:self._restart(role)
                except Exception:pass
                raise
            self.app.store.audit_event('activate','model_library',asset_id,{'role':role})
            return self.list()
    def rename(self,asset_id,name):
        with LOCK:
            entries=self.entries()
            for e in entries:
                if e['id']==asset_id:e['name']=str(name).strip() or e['name'];write_json(self.root/asset_id/'asset.json',e)
            write_json(self.catalog,entries)
            return self.list()
    def export(self,asset_id):
        e=next((e for e in self.entries() if e['id']==asset_id),None)
        if not e:raise ValueError('模型不存在')
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as z:
            for name in e['files']:z.write(str(self.root/asset_id/name),name)
        return stream.getvalue()
