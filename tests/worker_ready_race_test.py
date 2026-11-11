"""A live worker must survive transient Windows readiness-file sharing locks."""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from product.supervisor import Supervisor
from product.common import write_json


class ReadyRaceTests(unittest.TestCase):
    def run_start(self, read_failures=(), locked_cleanup=False):
        with tempfile.TemporaryDirectory(dir=str(ROOT / 'outputs')) as tmp:
            root = Path(tmp)
            supervisor = Supervisor.__new__(Supervisor)
            supervisor.data = supervisor.package = root
            supervisor.logs = {}
            supervisor.lock = threading.RLock()
            supervisor.stopping = threading.Event()
            supervisor.ports = {'web': 12345, 'price': 12346, 'effectiveness': 12347, 'control': 12348}
            supervisor.config = {'ports': dict(supervisor.ports)}
            supervisor.generation = 'current-generation'
            supervisor.workers = {}
            supervisor.job = Mock()
            supervisor.errors = {'web': 'previous failure'}
            supervisor.health_failures = {}
            supervisor.web_started = False
            supervisor.no_browser = True
            supervisor.record_status = Mock()
            process = Mock(pid=123)
            process.poll.return_value = None
            ready_value = {'pid': 123, 'generation': supervisor.generation, 'url': 'http://127.0.0.1:12345'}

            def start(args, **kwargs):
                write_json(Path(args[args.index('--ready') + 1]), ready_value)
                return process

            try:
                with patch('product.supervisor.subprocess.Popen', side_effect=start), \
                     patch('product.supervisor.read_json', side_effect=list(read_failures) + [ready_value]) as read, \
                     patch('product.supervisor.fetch', return_value={'status': 'ok'}) as health:
                    if locked_cleanup:
                        with patch.object(Path, 'unlink', side_effect=PermissionError('temporary sharing lock')):
                            url = supervisor.start_worker('web')
                    else:
                        url = supervisor.start_worker('web')
                    self.assertEqual(read.call_count, len(read_failures) + 1)
                    health.assert_called_once()
                self.assertEqual(url, ready_value['url'])
                self.assertTrue(supervisor.alive('web'))
                self.assertNotIn('web', supervisor.errors)
                supervisor.record_status.assert_called_once()
            finally:
                for stream in supervisor.logs.values():
                    stream.close()

    def test_read_lock_and_incomplete_json_retry_until_same_worker_is_healthy(self):
        self.run_start([PermissionError('temporary sharing lock'), json.JSONDecodeError('incomplete', '', 0)])

    def test_cleanup_sharing_lock_does_not_fail_a_healthy_worker(self):
        self.run_start(locked_cleanup=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
