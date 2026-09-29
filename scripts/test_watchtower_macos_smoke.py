"""Regression coverage for the macOS smoke's real CLI status contract."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

import watchtower_smoke_macos as smoke
from watchtower_smoke_windows import SmokeFailure


class MacSmokeStartupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.instance = smoke.Instance(self.root / 'watchtower', self.root / 'home', 'a')
        self.addCleanup(self.instance.log.close)
        self.status = {
            'running': True,
            'session': 'smoke-a',
            'socket': str(self.instance.home / 'config/sessions/smoke-a/herdr.sock'),
        }
        self.process = Mock()
        self.process.poll.return_value = None
        self.run = Mock(side_effect=self.response)
        self.instance.run = self.run
        self.popen = self.enterContext(patch.object(smoke.subprocess, 'Popen', return_value=self.process))
        self.wait = self.enterContext(patch.object(smoke, 'wait_until', side_effect=self.check_once))

    @staticmethod
    def check_once(check, description, **kwargs):
        smoke.require(check(), description)

    def response(self, *args, **kwargs):
        if args[:2] == ('status', 'server'):
            return self.status
        if args[:2] == ('workspace', 'create'):
            return {'result': {'root_pane': {'pane_id': 'w1:p1'}}}
        return {}

    def test_start_accepts_status_without_pid_and_owned_relay(self):
        record_path = self.instance.home / 'state/radio/session/relay-runtime-1.json'
        record_path.parent.mkdir(parents=True)
        record = {'socket': self.status['socket'], 'supervisor_pid': 100, 'relay_pid': 101}
        record_path.write_text(json.dumps(record))

        self.instance.start()

        self.assertNotIn('pid', self.status)
        self.assertIs(self.instance.process, self.process)
        self.assertIs(self.instance.status, self.status)
        self.assertEqual(self.instance.pane, 'w1:p1')
        self.assertEqual(self.instance.relay_path, record_path)
        self.assertEqual(self.instance.relay, record)
        self.assertEqual(self.wait.call_count, 2)
        self.assertEqual(self.popen.call_args.kwargs['env']['WATCHTOWER_HOME'], str(self.instance.home))
        self.assertEqual(self.popen.call_args.kwargs['env']['WATCHTOWER_SESSION'], 'smoke-a')
        self.assertTrue(self.popen.call_args.kwargs['start_new_session'])

    def assert_start_rejected(self, message):
        with self.assertRaisesRegex(SmokeFailure, message):
            self.instance.start()
        self.assertEqual(self.wait.call_count, 1)
        self.assertFalse(any(call.args[:2] == ('workspace', 'create') for call in self.run.call_args_list))
        self.assertFalse(hasattr(self.instance, 'relay'))

    def test_start_rejects_socket_outside_disposable_home(self):
        self.status['socket'] = str(self.root / 'foreign/herdr.sock')
        self.assert_start_rejected('Server socket escaped disposable home')

    def test_start_rejects_foreign_session(self):
        self.status['session'] = 'smoke-other'
        self.assert_start_rejected('Wrong runtime session')

    def test_start_rejects_exited_retained_process_before_status_lookup(self):
        self.process.poll.return_value = 1
        self.assert_start_rejected('server exited during startup')
        self.assertFalse(any(call.args[:2] == ('status', 'server') for call in self.run.call_args_list))


if __name__ == '__main__':
    unittest.main()
