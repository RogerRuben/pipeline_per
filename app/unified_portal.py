"""Same-origin facade for the supplied front-gate; no process management copy."""
import json

def handle_portal(handler,path):
    app=handler.app
    if path=='/app-config.js':
        config={'pages':{'costPre':'/price/deep','costPreEval1':'/effectiveness','costPreEval2':'/cost-effectiveness/','intelligent':'/recommendation'},
                'modelSources':[{'name':'价格模型','baseUrl':'','endpoint':'/api/portal/models/price','type':'price'},
                                {'name':'效能模型','baseUrl':'','endpoint':'/api/portal/models/effectiveness','type':'effectiveness'},
                                {'name':'推荐系统','baseUrl':'','endpoint':'/api/bootstrap','type':'recommend'}]}
        # Front-gate validates baseUrl as nonempty. Same origin follows configured web port.
        for item in config['modelSources']:item['baseUrl']='/'
        handler._bytes(('window.__APP_CONFIG__ = '+json.dumps(config,ensure_ascii=False)+';window.__APP_CONFIG__.modelSources.forEach(function(s){s.baseUrl=location.origin;});').encode('utf-8'),'application/javascript; charset=utf-8');return True
    if path=='/api/portal':
        product=app.store.bootstrap()['product']
        control=((app.portal_config.get('services') or {}).get('desktop_control') or {}).get('url')
        handler._json({'product':product,'control_url':control,'pages':{'recommendation':'/recommendation','price':'/price/deep','effectiveness':'/effectiveness','cost':'/cost-effectiveness/'}});return True
    if path.startswith('/api/portal/models/'):
        kind=path.rsplit('/',1)[-1];manifest=app.runtime.manifest();m=manifest.get(kind) or {}
        health=app.model_gateway.health() if getattr(app,'model_gateway',None) else {}
        active=(health.get(kind) or {}).get('status')=='ok'
        if kind=='price':
            schema=app.model_gateway.price_schema() if getattr(app,'model_gateway',None) else {}
            metadata=schema.get('analysis_metadata') or {};recorded=metadata.get('metrics') or {}
            selected={};scope='未提供评价指标'
            for key,title in [('validation_metrics','开发验证集'),('deployed_training_metrics','部署模型训练集'),('full_refit_training_metrics','全数据重拟合训练集'),('training_metrics','训练集')]:
                if isinstance(recorded.get(key),dict):selected=recorded[key];scope=title;break
            metrics={k:selected[k] for k in ('r2','mae','rmse') if selected.get(k) is not None}
            units=(metadata.get('metric_units') or {}).get(next((k for k in ('validation_metrics','deployed_training_metrics','full_refit_training_metrics','training_metrics') if recorded.get(k) is selected),''),{})
            if selected.get('mape_percent') is not None:metrics['mape']=str(round(selected['mape_percent'],4))+'%'
            elif selected.get('mape') is not None:
                unit=units.get('mape');v=selected['mape']
                metrics['mape']=(str(round(v*100 if unit=='ratio' else v,4))+'%') if unit in ('ratio','percent') else str(v)+'（单位未声明）'
            info=health.get('price') or {}
            handler._json({'model_sets':[{'name':(m.get('model_version') or '当前价格模型')+' · '+scope,'active':active,'valid':bool(m),'metrics':metrics,'raw_metrics':recorded,'metric_scope':scope,'model_count':info.get('model_count'),'model_names':info.get('model_names') or []}]})
        elif kind=='effectiveness':handler._json({'models':[{'name':m.get('model_version') or '当前效能模型','status':{'package_valid':True},'native':m,'is_demo':m.get('backend')=='snapshot'}]})
        else:handler._json({'error':'not_found'},404)
        return True
    return False
