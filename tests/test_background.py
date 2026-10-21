"""Exercise a real detached worker during a closed working window: no browser traffic."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from zoneinfo import ZoneInfo

from outreach import Store, main, working_time, utcnow
from outreach_config import load_settings
from outreach_setup import worker_running
from scripts.check_release import private


class BackgroundTests(unittest.TestCase):
    def test_detached_worker_starts_and_stops_with_zero_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv = root / 'contacts.csv'
            csv.write_text('Full Name,Company,LinkedIn Profile\nAlex Example,Example Company,https://linkedin.com/in/example-alex-not-real/\n', encoding='utf-8')
            today = datetime.now(ZoneInfo('UTC')).weekday()
            settings = root / 'settings.json'
            settings.write_text(json.dumps({'csv_file': str(csv), 'timezone': 'UTC',
                                             'working_days': [(today + 2) % 7]}), encoding='utf-8')
            cfg = load_settings(settings)
            self.assertFalse(working_time(utcnow(), cfg))
            output = io.StringIO()
            state = root / '.local'
            try:
                with redirect_stdout(output), redirect_stderr(output):
                    result = main(['--settings', str(settings), 'start', '--live'])
                self.assertEqual(result, 0, output.getvalue())
                self.assertTrue(worker_running(state))
                with redirect_stdout(output), redirect_stderr(output):
                    self.assertEqual(main(['--settings', str(settings), 'start', '--live']), 1)
                    self.assertEqual(main(['--settings', str(settings), 'stop-watch']), 0)
                deadline = time.monotonic() + 15
                while worker_running(state) and time.monotonic() < deadline:
                    time.sleep(0.2)
                self.assertFalse(worker_running(state))
                store = Store(state / 'outreach.sqlite3', readonly=True)
                try:
                    self.assertEqual(store.used('invite'), 0)
                    self.assertEqual(store.db.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
                finally:
                    store.close()
                self.assertFalse((state / 'watcher-ready.json').exists())
            finally:
                if state.exists():
                    (state / 'stop-watch.request').write_text('test cleanup', encoding='utf-8')
                    deadline = time.monotonic() + 15
                    while worker_running(state) and time.monotonic() < deadline:
                        time.sleep(0.2)

    def test_release_guard_rejects_private_inputs_and_state(self):
        for path in ('settings.json', 'my/contacts.csv', '.local/outreach.sqlite3',
                     'profiles/Default/Cookies', 'messages.private.json', '.env', 'debug.log', 'session.png',
                     'examples/contacts.example.csv', 'examples/messages.example.json',
                     'contacts.xlsx', 'contacts.XLSX', 'contacts.xlsm', 'inputs/leads.json',
                     'backups/source.csv.bak', 'storage_state.json'):
            with self.subTest(path=path):
                self.assertTrue(private(path))
        for path in ('outreach.py', 'settings.example.json', 'README.md'):
            with self.subTest(path=path):
                self.assertFalse(private(path))


if __name__ == '__main__':
    unittest.main()
