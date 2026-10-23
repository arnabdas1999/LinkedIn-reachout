"""Fault injection for contact isolation, journaling and worker recovery."""

from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from playwright.sync_api import Error as BrowserError

from outreach import Campaign, Store, browser_cycle, campaign_finished, stamp
from outreach_errors import AccountBlocked, ContactNotReady, RetryCycle
from outreach_worker import retry_delay, watch_worker
from test_outreach import FakeBrowser, NOW, OutreachCase


class ContactRecoveryTests(OutreachCase):
    def test_transient_profile_read_recovers_without_repeating_a_send(self):
        browser = FakeBrowser()
        calls = []
        def read(lead):
            calls.append(lead['id'])
            if len(calls) == 1:
                raise TimeoutError('Profile controls still loading')
            return 'new'
        browser.open_profile = read
        self.campaign(browser).run()
        self.assertEqual(calls, [1, 1])
        self.assertEqual(len(browser.sends), 1)
        self.assertEqual(self.store.unresolved(), 0)

    def test_bad_read_is_deferred_and_next_contact_is_processed(self):
        self.add_lead('healthy')
        browser = FakeBrowser()
        def read(lead):
            if lead['id'] == 1:
                raise ContactNotReady('Loading timeout')
            return 'new'
        browser.open_profile = read
        self.campaign(browser).run()
        self.assertEqual(browser.sends[0][0], 2)
        self.assertEqual(self.store.lead(1)['state'], 'new')
        self.assertEqual(self.store.lead(1)['failure_count'], 1)
        self.assertGreater(self.store.lead(1)['next_check_at'], stamp(NOW))
        self.assertEqual(self.store.used('invite'), 1)
        self.assertEqual(self.store.candidates(now=NOW), [])
        self.assertFalse(campaign_finished(self.store, {**self.cfg, 'campaign_invitation_limit': None}))

    def test_repeated_read_failure_survives_restart_and_eventually_holds_contact(self):
        browser = FakeBrowser()
        browser.open_profile = lambda lead: (_ for _ in ()).throw(TimeoutError('Still loading'))
        for attempt in range(3):
            self.campaign(browser, NOW + timedelta(hours=attempt)).run()
            self.store.close()
            self.store = Store(self.path)
        self.assertEqual(self.store.lead(1)['state'], 'needs_review')
        self.assertEqual(self.store.lead(1)['failure_count'], 3)
        self.assertEqual(browser.sends, [])
        self.assertEqual(self.store.used('invite'), 0)

    def test_unreadable_history_holds_one_sequence_and_allows_new_invites(self):
        self.invite(NOW - timedelta(days=2))
        self.add_lead('healthy')
        self.cfg.update(campaign_invitation_limit=2, invitations_per_day=2)
        browser = FakeBrowser()
        browser.open_profile = lambda lead: 'connected' if lead['id'] == 1 else 'new'
        browser.conversation = lambda lead, known: type('History', (), {'unknown': True, 'inbound': False})()
        self.campaign(browser).run()
        self.assertEqual(self.store.lead(1)['state'], 'needs_review')
        self.assertEqual(self.store.lead(2)['state'], 'invited')
        self.assertEqual([kind for _, kind, _ in browser.sends], ['invite'])

    def test_uncertain_send_quarantines_recipient_and_keeps_other_contacts_running(self):
        self.add_lead('healthy')
        self.add_lead('past-limit')
        self.cfg.update(campaign_invitation_limit=None, invitations_per_run=2, invitations_per_day=2)
        browser = FakeBrowser()
        original = browser.send_invitation
        def send(lead, message):
            original(lead, message)
            if lead['id'] == 1:
                raise TimeoutError('Disconnected after Send')
        browser.send_invitation = send
        result = self.campaign(browser).run()
        self.assertEqual(result['invitations_sent'], 1)
        self.assertEqual(self.store.unresolved(), 1)
        self.assertEqual(self.store.lead(2)['state'], 'invited')
        self.assertEqual(self.store.lead(3)['state'], 'new')
        self.assertEqual(self.store.used('invite'), 2)
        self.campaign(browser).run()
        self.assertEqual([item[0] for item in browser.sends], [1, 2])

    def test_recovered_crash_does_not_block_other_contacts_or_retry_uncertain_contact(self):
        self.add_lead('healthy')
        self.cfg.update(campaign_invitation_limit=2, invitations_per_day=2)
        self.store.begin(1, 'invite', 'Crash before outcome recorded', NOW)
        browser = FakeBrowser()
        self.campaign(browser).run()
        self.assertEqual(self.store.lead(1)['state'], 'needs_review')
        self.assertEqual(browser.sends[0][0], 2)
        with self.assertRaises(ValueError):
            self.store.begin(1, 'message_1', 'Forbidden', NOW)

    def test_three_consecutive_failures_open_circuit_before_scanning_entire_csv(self):
        for number in range(4):
            self.add_lead(f'broken-{number}')
        browser = FakeBrowser()
        browser.open_profile = lambda lead: (_ for _ in ()).throw(ContactNotReady('Changed UI'))
        with self.assertRaises(RetryCycle):
            self.campaign(browser).run()
        self.assertEqual(self.store.db.execute('SELECT sum(failure_count) FROM leads').fetchone()[0], 3)
        self.assertEqual(self.store.lead(4)['failure_count'], 0)
        self.assertEqual(browser.sends, [])

    def test_account_stop_during_send_is_global_and_journal_remains_uncertain(self):
        self.add_lead('next')
        self.cfg.update(campaign_invitation_limit=2, invitations_per_day=2)
        browser = FakeBrowser()
        browser.send_invitation = lambda lead, message: (_ for _ in ()).throw(AccountBlocked('Verification required'))
        with self.assertRaises(AccountBlocked):
            self.campaign(browser).run()
        self.assertEqual(self.store.unresolved(), 1)
        self.assertEqual(self.store.lead(2)['state'], 'new')

    def test_account_stop_during_read_has_no_retry(self):
        browser = FakeBrowser()
        browser.open_profile = lambda lead: (_ for _ in ()).throw(AccountBlocked('Weekly limit'))
        pauses = []
        with self.assertRaises(AccountBlocked):
            Campaign(self.store, self.cfg, browser, now=lambda: NOW, pause=pauses.append).run()
        self.assertEqual(pauses, [])
        self.assertEqual(self.store.used('invite'), 0)

    def test_uncertain_followup_does_not_block_other_followups_or_invitation_allowance(self):
        self.invite(NOW - timedelta(days=3))
        self.add_lead('second-accepted')
        self.add_lead('new-contact')
        self.store.begin(2, 'invite', 'Earlier note', NOW - timedelta(days=3))
        self.store.sent(2, 'invite', NOW - timedelta(days=3))
        for lead_id in (1, 2):
            self.store.update(lead_id, state='accepted', accepted_at=stamp(NOW - timedelta(days=2)))
        self.cfg.update(campaign_invitation_limit=None, invitations_per_day=25, messages_per_run=2, messages_per_day=2)
        browser = FakeBrowser()
        browser.open_profile = lambda lead: 'connected' if lead['id'] in (1, 2) else 'new'
        original = browser.send_message
        def send(lead, message, known):
            original(lead, message, known)
            if lead['id'] == 1:
                raise TimeoutError('Message outcome unknown')
        browser.send_message = send
        result = self.campaign(browser).run()
        self.assertEqual(result['messages_sent'], 1)
        self.assertEqual(result['invitations_sent'], 1)
        self.assertEqual(self.store.used('message'), 2)
        self.assertEqual(self.store.lead(1)['state'], 'needs_review')
        self.assertEqual(self.store.lead(2)['state'], 'message_1_sent')
        self.assertEqual(self.store.lead(3)['state'], 'invited')
        self.assertEqual(self.store.used('invite'), 3)

    def test_browser_crash_after_possible_send_preserves_hold_on_next_browser_cycle(self):
        self.add_lead('healthy')
        self.cfg.update(campaign_invitation_limit=2, invitations_per_day=2, invitations_per_run=2)
        broken = FakeBrowser()
        closed = False
        def read(lead):
            if closed:
                raise BrowserError('Target page, context or browser has been closed')
            return 'new'
        def send(lead, message):
            nonlocal closed
            broken.sends.append((lead['id'], 'invite', message))
            closed = True
            raise BrowserError('Target page, context or browser has been closed')
        broken.open_profile, broken.send_invitation = read, send
        with self.assertRaises(RetryCycle):
            self.campaign(broken).run()
        healthy = FakeBrowser()
        self.campaign(healthy).run()
        self.assertEqual([item[0] for item in broken.sends + healthy.sends], [1, 2])
        self.assertEqual(self.store.unresolved(), 1)
        self.assertEqual(self.store.used('invite'), 2)


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = {'timezone': 'UTC', 'working_days': list(range(7)), 'working_hours': [0, 24],
                    'watch_interval_minutes': 30, 'retry_initial_seconds': 60, 'retry_max_seconds': 1800}
        self.clock = NOW
        self.waits = []
        self.stop = False

    def tearDown(self):
        self.temp.cleanup()

    def run_worker(self, run, wait):
        return watch_worker(self.cfg, self.root, run_cycle=run, has_work=lambda: True,
                            is_finished=lambda: False, next_retry=lambda: None,
                            now=lambda: self.clock, wait=wait, stop_requested=lambda: self.stop)

    def test_browser_failure_backs_off_then_resumes_with_exponential_delays(self):
        calls = []
        def run():
            calls.append(1)
            if len(calls) <= 2:
                raise BrowserError('Browser transport disconnected')
            return {'invitations_sent': 1}
        def wait(seconds):
            self.waits.append(seconds)
            info = json.loads((self.root / 'worker-status.json').read_text())
            if len(calls) <= 2:
                self.assertEqual(info['phase'], 'backoff')
                self.assertTrue(info['next_retry_at'])
            self.clock += timedelta(seconds=seconds)
            return len(calls) == 3
        self.run_worker(run, wait)
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.waits[:2], [60, 120])
        self.assertEqual(json.loads((self.root / 'worker-status.json').read_text())['phase'], 'stopped')

    def test_account_stop_does_not_enter_recovery_loop(self):
        def run():
            raise AccountBlocked('Sign in manually')
        with self.assertRaises(AccountBlocked):
            self.run_worker(run, self.waits.append)
        self.assertEqual(self.waits, [])
        self.assertEqual(json.loads((self.root / 'worker-status.json').read_text())['phase'], 'blocked')

    def test_database_fault_is_not_silently_retried(self):
        def run():
            raise sqlite3.OperationalError('disk I/O error')
        with self.assertRaises(sqlite3.OperationalError):
            self.run_worker(run, self.waits.append)
        self.assertEqual(self.waits, [])
        self.assertEqual(json.loads((self.root / 'worker-status.json').read_text())['phase'], 'failed')

    def test_stop_during_backoff_exits_without_another_cycle(self):
        calls = []
        def run():
            calls.append(1)
            raise RetryCycle('Network unavailable')
        self.run_worker(run, lambda seconds: True)
        self.assertEqual(calls, [1])

    def test_backoff_is_capped(self):
        self.assertEqual(retry_delay(self.cfg, 10000), 1800)

    def test_cycle_relaunches_browser_and_preserves_journal_across_recovery(self):
        # Actual browser-cycle path, with a dead transport followed by a healthy session.
        from outreach_config import load_settings
        cfg = load_settings(Path(__file__).resolve().parents[1] / 'settings.example.json')
        store = Store(self.root / 'state.sqlite3')
        try:
            with store.db:
                store.db.execute("""INSERT INTO leads (url,first_name,full_name,company,title,city,region)
                    VALUES ('https://www.linkedin.com/in/fixture/', 'Alex','Alex Example','Example','','','')""")
            healthy = FakeBrowser()
            healthy.require_login = lambda: None
            from contextlib import nullcontext
            calls = []
            def factory(_cfg):
                calls.append(1)
                if len(calls) == 1:
                    raise BrowserError('Browser crashed')
                return nullcontext(healthy)
            # The worker and its real campaign must share the fixture clock;
            # otherwise this recovery test stops sending after business hours.
            with (patch('outreach.browser_session', side_effect=factory),
                  patch('outreach.Campaign', side_effect=lambda *args, **kwargs:
                        Campaign(*args, now=lambda: self.clock, **kwargs))):
                self.run_worker(lambda: browser_cycle(store, cfg, self.root / 'stop'),
                                lambda seconds: len(calls) >= 2)
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(healthy.sends), 1)
            self.assertEqual(store.used('invite'), 1)
            self.assertEqual(store.db.execute('SELECT started_at FROM actions').fetchone()[0], stamp(self.clock))
        finally:
            store.close()
