import sys,socket,unittest,tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from product.ports import bind_server,sync_addresses
from services.common.http_service import ExclusiveThreadingHTTPServer
from http.server import BaseHTTPRequestHandler
from product.common import write_json
class PortTests(unittest.TestCase):
 def test_available_preferred_port_is_preserved(self):
  server=bind_server(ExclusiveThreadingHTTPServer,0,BaseHTTPRequestHandler);port=server.server_address[1];server.server_close()
  server=bind_server(ExclusiveThreadingHTTPServer,port,BaseHTTPRequestHandler)
  try:self.assertEqual(server.server_address[1],port)
  finally:server.server_close()
 def test_occupied_port_falls_back_and_does_not_touch_occupier(self):
  with socket.socket() as occupier:
   occupier.bind(('127.0.0.1',0));occupier.listen();port=occupier.getsockname()[1]
   server=bind_server(ExclusiveThreadingHTTPServer,port,BaseHTTPRequestHandler)
   try:self.assertNotEqual(server.server_address[1],port);self.assertEqual(occupier.getsockname()[1],port)
   finally:server.server_close()
 def test_ephemeral_choice_cannot_steal_other_configured_port(self):
  class Server:
   def __init__(self,p):self.server_address=('127.0.0.1',p);self.closed=False
   def server_close(self):self.closed=True
  rejected,accepted=Server(18002),Server(18003)
  with patch('builtins.print'):
   factory=unittest.mock.Mock(side_effect=[OSError(98,'occupied'),rejected,accepted])
   # errno differs on Windows, construct the platform EADDRINUSE explicitly.
   import errno;factory.side_effect=[OSError(errno.EADDRINUSE,'occupied'),rejected,accepted]
   self.assertIs(bind_server(factory,18001,None,[18002]),accepted);self.assertTrue(rejected.closed)
 def test_unrelated_bind_error_is_not_hidden(self):
  with self.assertRaises(OSError):bind_server(unittest.mock.Mock(side_effect=OSError(123,'bad address')),18001,None)
 def test_web_clients_follow_new_addresses_without_overwriting_config(self):
  with tempfile.TemporaryDirectory(dir=str(ROOT/'outputs')) as folder:
   data=Path(folder);write_json(data/'runtime/status.json',{'services':{'price':{'url':'http://127.0.0.1:20001'},'effectiveness':{'url':'http://127.0.0.1:20002'}}})
   app=SimpleNamespace(model_gateway=SimpleNamespace(price_url='old',effectiveness_url='old'),model_config={},_invalidate_runtime_caches=unittest.mock.Mock())
   sync_addresses(app,data);self.assertEqual(app.model_gateway.price_url,'http://127.0.0.1:20001');self.assertEqual(app.model_config['effectiveness_service_url'],'http://127.0.0.1:20002')
   self.assertFalse((data/'config/desktop.json').exists())
if __name__=='__main__':unittest.main(verbosity=2)
