"""Peer disconnects are quiet; unrelated application/IO errors remain visible."""
import errno
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from services.common.http_service import make_handler, ServiceApplication


class Echo(ServiceApplication):
    def handle_post(self,path,payload):
        return {'success':True}


class DisconnectTests(unittest.TestCase):
    def handler(self):
        handler=object.__new__(make_handler(Echo()))
        handler.close_connection=False
        return handler

    def test_posix_and_windows_disconnects_do_not_trigger_error_responses(self):
        for exc in (BrokenPipeError(),ConnectionResetError(),ConnectionAbortedError(),
                    OSError(errno.EPIPE,'closed'),OSError(errno.ECONNRESET,'reset'),OSError(errno.ECONNABORTED,'aborted')):
            with self.subTest(error=type(exc).__name__):
                h=self.handler()
                with patch.object(h,'_write_response',side_effect=exc) as write:
                    h._send(200,'application/json',b'{}')
                    self.assertEqual(write.call_count,1)
                    self.assertTrue(h.close_connection)

    def test_unrelated_io_errors_are_not_swallowed(self):
        h=self.handler()
        with patch.object(h,'_write_response',side_effect=PermissionError(errno.EACCES,'denied')):
            with self.assertRaises(PermissionError):h._send(200,'application/json',b'{}')

    def test_peer_reset_before_request_dispatch_is_quiet(self):
        h=self.handler()
        with patch.object(BaseHTTPRequestHandler,'handle',side_effect=ConnectionResetError()):
            h.handle()
        self.assertTrue(h.close_connection)

    def test_unrelated_handler_errors_remain_visible(self):
        h=self.handler()
        with patch.object(BaseHTTPRequestHandler,'handle',side_effect=RuntimeError('fixture defect')):
            with self.assertRaises(RuntimeError):h.handle()

    def test_application_exception_still_returns_500(self):
        app=Echo()
        h=object.__new__(make_handler(app))
        h.headers={'Content-Length':'2'};h.rfile=io.BytesIO(b'{}');h.path='/api/test'
        with patch.object(app,'handle_post',side_effect=RuntimeError('fixture failure')),patch.object(h,'_send') as send:
            h.do_POST()
            self.assertEqual(send.call_args[0][0],500)
            self.assertEqual(json.loads(send.call_args[0][2])['error'],'internal_error')


if __name__=='__main__':unittest.main(verbosity=2)
