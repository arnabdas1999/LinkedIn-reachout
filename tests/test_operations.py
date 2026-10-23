"""Local operations use synthetic state, fake notifications and no live outreach."""
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import xml.etree.ElementTree as ET
from unittest.mock import patch

from outreach import Campaign, Store
from outreach_errors import AccountBlocked
from outreach_monitor import Monitor, alert, atomic_json, daily_report
from outreach_service import supervise, task_xml
from outreach_state import SCHEMA_VERSION, automatic_backup, backup_database, meta, migrate_directory, restore_database, resume_contact
from test_outreach import FakeBrowser, NOW, OutreachCase


class OperationsTests(OutreachCase):
    def setUp(self):
        super().setUp()
        self.folder = Path(self.tmp.name)
        self.cfg.update(desktop_notifications=False, state_directory=str(self.folder))

    def test_backup_restores_exact_journal_but_sending_requires_audit(self):
        self.invite()
        backup = backup_database(self.path)
        target = self.folder / 'restored.sqlite3'
        restore_database(backup, target)
        restored = Store(target)
        try:
            self.assertEqual(restored.sent_messages(1), self.store.sent_messages(1))
            self.assertEqual(meta(restored.db, 'restore_review_required'), '1')
            restored.update(1, state='stopped')
            self.add_lead('new')
            # Guard is also checked directly, before any prepared action exists.
            with self.assertRaises(AccountBlocked):
                Campaign(restored, self.cfg, FakeBrowser(), now=lambda: NOW).preflight(restored.lead(1), 'message_1')
        finally:
            restored.close()

    def test_restore_refuses_to_lose_newer_send_history(self):
        backup = backup_database(self.path)
        self.invite()
        with self.assertRaisesRegex(ValueError, 'newer or changed'):
            restore_database(backup, self.path)
        self.assertEqual(self.store.lead(1)['state'], 'invited')

    def test_restore_preserves_a_damaged_database_and_still_requires_audit(self):
        backup = backup_database(self.path)
        target = self.folder / 'damaged.sqlite3'
        target.write_bytes(b'damaged database fixture')
        restore_database(backup, target)
        preserved = list(self.folder.glob('damaged.sqlite3.*.damaged'))
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].read_bytes(), b'damaged database fixture')
        restored = Store(target)
        try:
            self.assertEqual(meta(restored.db, 'restore_review_required'), '1')
        finally:
            restored.close()

    def test_restore_refuses_a_different_campaign(self):
        other = self.folder / 'other.sqlite3'
        store = Store(other)
        store.close()
        with self.assertRaisesRegex(ValueError, 'different campaign'):
            restore_database(other, self.path)

    def test_future_schema_is_not_opened_by_an_older_application(self):
        self.store.db.execute(f'PRAGMA user_version={SCHEMA_VERSION + 1}')
        self.store.close()
        with self.assertRaisesRegex(ValueError, 'newer schema'):
            Store(self.path)
        # Restore a supported version for fixture teardown.
        db = sqlite3.connect(self.path)
        db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
        db.close()
        self.store = Store(self.path)

    def test_resume_read_only_hold_and_refuse_possible_send(self):
        self.store.update(1, state='needs_review', reason='Page never loaded', failure_count=3)
        resume_contact(self.store, 1)
        self.assertEqual(self.store.lead(1)['state'], 'new')
        self.store.begin(1, 'invite', 'Uncertain note', NOW)
        self.store.uncertain(1, 'invite', 'Connection lost', NOW)
        with self.assertRaises(ValueError):
            resume_contact(self.store, 1)

    def test_cancelled_invitation_can_only_be_requeued_by_explicit_reconciliation(self):
        self.store.begin(1, 'invite', 'Note', NOW)
        self.store.hold_invitation(1, 'Form held before final send', NOW)
        with self.assertRaises(ValueError):
            resume_contact(self.store, 1)
        self.store.reconcile(1, 'not-sent', NOW)
        self.assertEqual(self.store.lead(1)['state'], 'new')
        self.assertEqual(self.store.used('invite'), 0)

    def test_cancelled_invitation_can_be_confirmed_after_operator_review(self):
        self.store.begin(1, 'invite', 'Note', NOW)
        self.store.hold_invitation(1, 'Form held for review', NOW)
        self.store.reconcile(1, 'sent', NOW)
        self.assertEqual(self.store.lead(1)['state'], 'invited')
        self.assertEqual(self.store.db.execute('SELECT status FROM actions').fetchone()[0], 'sent')

    def test_daily_report_separates_invites_messages_and_reserved_attempts(self):
        path = self.folder / 'outreach.sqlite3'
        self.invite()
        self.store.begin(1, 'message_1', 'Follow-up', NOW)
        self.store.uncertain(1, 'message_1', 'Unknown outcome', NOW)
        backup_database(self.path, path)
        report = daily_report(self.folder, self.cfg, now=NOW)
        self.assertEqual(report['confirmed']['invite'], 1)
        self.assertEqual(report['confirmed']['message_1'], 0)
        self.assertEqual(report['reserved_attempts']['message_1'], 1)
        self.assertEqual(report['shortfall'], 0)

    def test_alerts_are_deduplicated_and_persisted_before_desktop_delivery(self):
        self.cfg['desktop_notifications'] = True
        with patch('outreach_monitor.desktop_notification') as notify:
            self.assertTrue(alert(self.folder, self.cfg, 'blocked', 'Manual login needed', NOW))
            self.assertFalse(alert(self.folder, self.cfg, 'blocked', 'Manual login needed', NOW + timedelta(seconds=30)))
            notify.assert_called_once()
        self.assertEqual(len((self.folder / 'alerts.jsonl').read_text().splitlines()), 1)

    def test_stalled_progress_alerts_even_though_heartbeat_thread_is_alive(self):
        atomic_json(self.folder / 'worker-status.json', {'phase': 'working', 'updated_at': (NOW - timedelta(minutes=10)).isoformat()})
        atomic_json(self.folder / 'progress.json', {'updated_at': (NOW - timedelta(minutes=10)).isoformat()})
        Monitor(self.cfg, self.folder, now=lambda: NOW).tick()
        self.assertEqual(json.loads((self.folder / 'heartbeat.json').read_text())['at'], NOW.isoformat())
        self.assertIn('stalled', (self.folder / 'alerts.jsonl').read_text())

    def test_auto_backup_retention_keeps_valid_completed_snapshots(self):
        backup_database(self.path, self.folder / 'outreach.sqlite3')
        self.cfg.update(backup_interval_hours=1, backup_retention=2)
        for day in range(3):
            automatic_backup(self.folder, self.cfg, NOW + timedelta(days=day))
        snapshots = list((self.folder / 'backups').glob('outreach-*.sqlite3'))
        # Includes the manual fixture snapshot; all snapshots are valid and bounded.
        self.assertEqual(len(snapshots), 2)
        for path in snapshots:
            db = sqlite3.connect(path)
            try:
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            finally:
                db.close()

    def test_moving_state_preserves_old_copy_and_only_changes_state_setting(self):
        old = self.folder / 'old'
        old.mkdir()
        backup_database(self.path, old / 'outreach.sqlite3')
        settings = self.folder / 'settings.json'
        raw = {'csv_file': 'private.csv', 'state_directory': str(old), 'invitations_per_day': 25}
        settings.write_text(json.dumps(raw))
        cfg = {**self.cfg, '_settings_path': str(settings), '_workspace': str(self.folder), 'state_directory': str(old)}
        new = self.folder / 'unsynced'
        migrate_directory(cfg, new)
        self.assertTrue((old / 'outreach.sqlite3').exists())
        self.assertTrue((new / 'outreach.sqlite3').exists())
        self.assertEqual(json.loads(settings.read_text()), {**raw, 'state_directory': str(new)})

    def test_supervisor_restarts_a_dead_worker_without_resetting_intent(self):
        atomic_json(self.folder / 'control.json', {'desired': 'running', 'requested_at': (NOW - timedelta(minutes=1)).isoformat()})
        stopped = [False]
        with patch('outreach_setup.worker_running', return_value=False), patch('outreach_setup.start_worker') as start:
            supervise(self.cfg, wait=lambda seconds: stopped.__setitem__(0, True), stop=lambda: stopped[0], now=lambda: NOW, refresh=lambda: self.cfg)
            start.assert_called_once_with(self.cfg, record_intent=False)

    def test_supervisor_does_not_restart_an_account_block_in_the_same_second(self):
        atomic_json(self.folder / 'control.json', {'desired': 'running', 'requested_at': NOW.replace(microsecond=1000).isoformat()})
        atomic_json(self.folder / 'worker-status.json', {'phase': 'blocked', 'updated_at': NOW.replace(microsecond=2000).isoformat(), 'last_error': 'Verification required'})
        stopped = [False]
        with patch('outreach_setup.worker_running', return_value=False), patch('outreach_setup.start_worker') as start:
            supervise(self.cfg, wait=lambda seconds: stopped.__setitem__(0, True), stop=lambda: stopped[0], now=lambda: NOW, refresh=lambda: self.cfg)
            start.assert_not_called()

    def test_supervisor_honors_an_explicit_pause(self):
        atomic_json(self.folder / 'control.json', {'desired': 'paused', 'requested_at': NOW.isoformat()})
        stopped = [False]
        with patch('outreach_setup.worker_running', return_value=False), patch('outreach_setup.start_worker') as start:
            supervise(self.cfg, wait=lambda seconds: stopped.__setitem__(0, True), stop=lambda: stopped[0], now=lambda: NOW, refresh=lambda: self.cfg)
            start.assert_not_called()

    def test_logon_task_is_current_user_hidden_and_has_no_password_or_elevation(self):
        xml = task_xml(self.cfg, 'S-1-5-21-EXAMPLE', Path('C:/Python Space/pythonw.exe'))
        root = ET.fromstring(xml)
        ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        self.assertEqual(root.find('t:Principals/t:Principal/t:LogonType', ns).text, 'InteractiveToken')
        self.assertEqual(root.find('t:Principals/t:Principal/t:RunLevel', ns).text, 'LeastPrivilege')
        self.assertEqual(root.find('t:Settings/t:Hidden', ns).text, 'true')
        self.assertIn('outreach_service', root.find('t:Actions/t:Exec/t:Arguments', ns).text)
