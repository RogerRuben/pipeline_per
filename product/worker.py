"""Service roles run in the bundled, pinned model interpreter, never retrain."""
import argparse
from contextlib import closing
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from product.common import contained, read_json, write_json, verify_package
from product.windows import watch_parent
from product.compatibility import load_contract,validate_data,accept_initial,fingerprint,check_data_root


def initialize_data(package, data, manifest):
    """Seed only an absent database; an incomplete existing DB requires recovery."""
    import shutil
    import sqlite3
    import uuid
    check_data_root(data)
    contract=load_contract(package,manifest)
    database=data/'data/protocol_demo.db'
    if database.exists():
        validate_data(package,data,manifest,contract)
        return False
    for name in ('data','config','backups','exports','uploads','logs','runtime','models'):
        (data/name).mkdir(parents=True,exist_ok=True)
    defaults = package/'defaults'
    if defaults.is_dir():
        for path in defaults.glob('*.json'):
            target = data/'config'/path.name
            if not target.exists():
                shutil.copy2(str(path),str(target))
    from app.store import Store
    from app.product_releases import ProductReleaseService
    from app.model_service_client import DegradedServiceRuntime
    release = manifest['release']
    runtime = DegradedServiceRuntime(None,release['product_code'],release['product_name'])
    temporary = data/('runtime/initial-'+uuid.uuid4().hex+'.db')
    try:
        store = Store(temporary,data/'unused.csv',runtime,data/'backups')
        releases = ProductReleaseService(store,runtime)
        imported = releases.import_package(contained(package,release['business_package']).read_bytes())
        releases.activate(imported['release_id'])
        # SQLite backup makes the handover independent of WAL mode.
        snapshot = data/'runtime/initial-ready.db'
        with closing(sqlite3.connect(str(temporary))) as source, closing(sqlite3.connect(str(snapshot))) as destination:
            source.backup(destination)
        os.replace(str(snapshot),str(database))
        write_json(data/'runtime/initialized.json',{'package_version':manifest['version'],
                   'business_package_sha256':manifest['files'][release['business_package']],
                   'delivery_contract_sha256':fingerprint(contract)})
    finally:
        if temporary.exists():
            temporary.unlink()
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--role',choices=['price','effectiveness','web'],required=True)
    parser.add_argument('--package',required=True)
    parser.add_argument('--data-dir',required=True)
    parser.add_argument('--ready',required=True)
    parser.add_argument('--generation',required=True)
    parser.add_argument('--parent',type=int,required=True)
    parser.add_argument('--price-url')
    parser.add_argument('--effectiveness-url')
    parser.add_argument('--port',type=int,default=0)
    args = parser.parse_args()
    watch_parent(args.parent)
    package,data = Path(args.package).resolve(),Path(args.data_dir).resolve()
    manifest = verify_package(package)
    release = manifest['release']
    from http.server import ThreadingHTTPServer
    if args.role == 'web':
        initialize_data(package,data,manifest)
        os.environ.update(IPDEMO_MODEL_EXECUTION_MODE='services',IPDEMO_PRICE_SERVICE_URL=args.price_url,
                          IPDEMO_EFFECT_SERVICE_URL=args.effectiveness_url,IPDEMO_MODEL_SERVICE_FALLBACK='0',
                          IPDEMO_DEMO_READ_ONLY='0')
        from app.server import Application,Handler
        app = Application(data)
        app.static_dir = ROOT/'app/static'
        # Capture the post-sync binding state only after a successful startup.
        write_json(data/'delivery_state.json',accept_initial(data,manifest,load_contract(package,manifest)))
        # Runtime-only links: persisted configuration and prices remain untouched.
        app.portal_config['services']['cost_effectiveness_analysis'].update(visible=False,enabled=False)
        app.portal_config['services']['desktop_control'] = {
            'label':'软件运行管理','description':'查看运行状态、重启或停止软件',
            'url':read_json(data/'runtime/control.json')['url'],'visible':True,'enabled':True}
        app.portal_config['services']['effectiveness']['label']='试用效能评价'
        class WebHandler(Handler):
            def do_GET(self):
                from urllib.parse import urlparse
                path=urlparse(self.path).path
                if path=='/api/desktop-status':
                    if self._auth_required(path):return
                    try:
                        from product.supervisor import fetch
                        import sqlite3
                        status=fetch(read_json(data/'runtime/control.json')['url']+'/api/status')
                        with closing(sqlite3.connect((data/'data/protocol_demo.db').as_uri()+'?mode=ro',uri=True)) as connection:
                            row=connection.execute('SELECT product_code,product_name FROM products WHERE enabled=1').fetchone()
                            status['business']={'product_code':row[0],'product_name':row[1],
                               'historical_count':connection.execute('SELECT COUNT(*) FROM agreements WHERE enabled=1').fetchone()[0],
                               'field_count':connection.execute('SELECT COUNT(*) FROM parameter_definitions WHERE enabled=1').fetchone()[0],
                               'saved_count':connection.execute('SELECT COUNT(*) FROM saved_schemes').fetchone()[0]}
                        self._json(status)
                    except (OSError,ValueError,KeyError):self._json({'error':'desktop_controller_unavailable'},503)
                    return
                super().do_GET()
        WebHandler.app = app
        handler = WebHandler
    else:
        from services.common.http_service import make_handler
        if args.role == 'price':
            from services.price_service.app import PriceService
            app = PriceService(contained(package,release['price_model']),None,
                               evaluation_metadata=contained(package,release['evaluation_metadata']))
        else:
            from services.effectiveness_service.app import EffectivenessService,SnapshotBackend
            app = EffectivenessService(SnapshotBackend(contained(package,release['effectiveness_model'])))
        handler = make_handler(app)
    server = ThreadingHTTPServer(('127.0.0.1',args.port),handler)
    server.daemon_threads = True
    write_json(args.ready,{'pid':os.getpid(),'role':args.role,'generation':args.generation,
                          'url':'http://127.0.0.1:%d'%server.server_address[1]})
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
