"""Bind preferred ports atomically, falling back only when unavailable."""
import errno
import logging

def bind_server(server_type,port,handler,reserved=()):
    try:return server_type(('127.0.0.1',port),handler)
    except OSError as exc:
        if not port or not (exc.errno==errno.EADDRINUSE or getattr(exc,'winerror',None) in (10048,10013)):raise
        logging.warning('Port %s unavailable; allocating another loopback port',port)
    excluded=set(reserved)
    for _ in range(128):
        server=server_type(('127.0.0.1',0),handler)
        if server.server_address[1] not in excluded:return server
        server.server_close()
    raise OSError('Unable to allocate an unreserved loopback port')

def sync_addresses(app,data):
    """Refresh web clients after a model service moves; never edit user config."""
    from product.common import read_json
    path=data/'runtime/status.json'
    if not path.exists():return
    try:status=read_json(path)
    except (OSError,ValueError):return
    changed=False
    for role,attr,key in [('price','price_url','price_service_url'),('effectiveness','effectiveness_url','effectiveness_service_url')]:
        url=(status.get('services',{}).get(role) or {}).get('url')
        if url:
            app.model_config[key]=url
            if url!=getattr(app.model_gateway,attr):
                setattr(app.model_gateway,attr,url);changed=True
    if changed:
        app.model_data_sync_error='服务地址已更新，等待重新连接'
        app._invalidate_runtime_caches()
