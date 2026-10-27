# -*- coding: utf-8 -*-
import copy,hashlib,json,os,socket,subprocess,sys,tempfile,time,unittest,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import app
app.__path__.insert(0,str(ROOT/'app'))
from automl_pipeline.product_pipeline import build
from automl_pipeline.product_effectiveness_import import install,safe_extract
from product_optuna_v6_test import data_fixture,options
from product_optuna_v2_test import workbook


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class RuntimeIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name);cls.out=cls.root/'outputs/demo'
        source=cls.root/'products.xlsx';source.write_bytes(workbook(data_fixture(4,8)))
        cls.manifest=build(source,cls.out,product_code='V6_TEST_PRODUCT',product_name='V6运行验证',virtual_data=True,
                           price_engine='optuna_v6',price_overrides=options(3))
        cls.original=json.loads((cls.out/'prototype_manifest.json').read_text(encoding='utf-8'))
        cls.effect=cls.out/cls.original['effectiveness_model'];cls.price=cls.out/cls.original['price_model'];cls.price_sha=sha(cls.price)
        cls.raw=json.loads(cls.effect.read_text(encoding='utf-8'))
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def test_pipeline_exports_three_parts_and_v6_test_scope(self):
        self.assertEqual(self.manifest['price_engine'],'optuna_v6')
        for key in ('price_model','effectiveness_model','business_package'):self.assertTrue((self.out/self.manifest[key]).is_file())
    def test_import_expert_snapshot_updates_effect_only(self):
        raw=copy.deepcopy(self.raw)
        if raw.get('manifest'):raw['manifest']['model_version']='expert-approved-test-2'
        else:raw['model_version']='expert-approved-test-2'
        path=self.root/'expert.json';path.write_text(json.dumps(raw,ensure_ascii=False),encoding='utf-8')
        result=install(path,self.root,self.out)
        self.assertEqual(result['status'],'IMPORTED');self.assertFalse(result['price_retrained']);self.assertEqual(sha(self.price),self.price_sha)
        current=json.loads((self.out/'prototype_manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(current['effectiveness_backend'],'expert_snapshot');self.assertTrue(current['effectiveness_integration']['validation'])
        self.assertEqual(sha(self.out/current['effectiveness_model']),current['artifact_sha256']['effectiveness_model'])
    def test_invalid_product_does_not_replace_existing_effect(self):
        before=(self.out/'prototype_manifest.json').read_bytes();raw=copy.deepcopy(self.raw)
        if raw.get('manifest'):raw['manifest']['product_code']='WRONG_PRODUCT'
        if raw.get('schema'):raw['schema']['product_code']='WRONG_PRODUCT'
        raw['product_code']='WRONG_PRODUCT';path=self.root/'wrong.json';path.write_text(json.dumps(raw),encoding='utf-8')
        with self.assertRaises(ValueError):install(path,self.root,self.out)
        self.assertEqual(before,(self.out/'prototype_manifest.json').read_bytes());self.assertEqual(sha(self.price),self.price_sha)
    def test_zip_path_escape_is_rejected(self):
        path=self.root/'bad.zip'
        with zipfile.ZipFile(path,'w') as z:z.writestr('../escape.json','{}')
        with self.assertRaises(ValueError):safe_extract(path,self.root/'unpacked')
    def test_portal_and_analysis_assets_have_real_links_and_api(self):
        text=(ROOT/'app/static/portal.html').read_text(encoding='utf-8')
        self.assertIn('/portal.js',text)
        self.assertIn('cost_effectiveness_analysis',(ROOT/'app/static/portal.js').read_text(encoding='utf-8'))
        js=(ROOT/'cost_effectiveness_analysis/static/app.js').read_text(encoding='utf-8')
        self.assertIn('/api/analyze',js)
        index=(ROOT/'app/static/index.html').read_text(encoding='utf-8')
        self.assertIn('V21.6',index);self.assertIn('优化目标',index)
        self.assertIn('initRailResize',(ROOT/'app/static/app.js').read_text(encoding='utf-8'))
    @unittest.skipUnless(os.name=='nt','Windows console-close process ownership test')
    def test_forced_parent_exit_releases_both_owned_model_ports(self):
        from automl_pipeline.process_group import process_alive
        ports=[]
        for _ in range(2):
            s=socket.socket();s.bind(('127.0.0.1',0));ports.append(s.getsockname()[1]);s.close()
        helper=self.root/'process_owner.py';ready=self.root/'process_ready.json'
        helper.write_text('import sys,time,json,subprocess\nfrom pathlib import Path\nsys.path.insert(0,'+repr(str(ROOT))+')\nfrom automl_pipeline.process_group import OwnedServices\nm=OwnedServices()\nps=[]\nfor port in '+repr(ports)+':\n c=m.spawn([sys.executable,"-c","from http.server import HTTPServer,BaseHTTPRequestHandler;HTTPServer((\'127.0.0.1\',"+str(port)+"),BaseHTTPRequestHandler).serve_forever()"],creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))\n ps.append(c.pid)\nPath('+repr(str(ready))+').write_text(json.dumps(ps))\nwhile True:time.sleep(.2)\n',encoding='utf-8')
        process=subprocess.Popen([sys.executable,str(helper)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        handles=[]
        import ctypes
        from ctypes import wintypes as w
        kernel=ctypes.WinDLL('kernel32',use_last_error=True);kernel.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];kernel.OpenProcess.restype=w.HANDLE
        kernel.CloseHandle.argtypes=[w.HANDLE];kernel.TerminateProcess.argtypes=[w.HANDLE,w.UINT]
        try:
            deadline=time.monotonic()+15
            while not ready.is_file():
                if process.poll() is not None:raise AssertionError(process.communicate()[1].decode(errors='replace'))
                if time.monotonic()>deadline:raise AssertionError('ownership helper timed out')
                time.sleep(.05)
            pids=json.loads(ready.read_text());handles=[kernel.OpenProcess(0x0001|0x1000,False,p) for p in pids]
            for port in ports:
                while True:
                    s=socket.socket();s.settimeout(.2)
                    try:s.connect(('127.0.0.1',port));break
                    except OSError:
                        if time.monotonic()>deadline:raise
                        time.sleep(.05)
                    finally:s.close()
            process.terminate();process.wait(timeout=5)
            deadline=time.monotonic()+8
            while any(process_alive(p) for p in pids) and time.monotonic()<deadline:time.sleep(.05)
            self.assertFalse(any(process_alive(p) for p in pids))
            for port in ports:
                while True:
                    with socket.socket() as s:
                        s.settimeout(.2);closed=s.connect_ex(('127.0.0.1',port))!=0
                    if closed:break
                    if time.monotonic()>deadline:raise AssertionError('Owned model port still listening after forced parent exit: '+str(port))
                    time.sleep(.05)
        finally:
            if process.poll() is None:process.terminate();process.wait(timeout=5)
            for handle in handles:
                if handle:kernel.TerminateProcess(handle,1);kernel.CloseHandle(handle)
            if process.stdout:process.stdout.close()
            if process.stderr:process.stderr.close()


if __name__=='__main__':unittest.main(verbosity=2)
