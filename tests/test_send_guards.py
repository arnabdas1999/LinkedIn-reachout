"""Boundary checks use virtual time; no live browser or external sends."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import time

from outreach import Campaign, day_start, invite_capacity, stamp
from outreach_errors import CyclePaused
from test_outreach import FakeBrowser, NOW, OutreachCase


class SendGuardTests(OutreachCase):
    def test_spacing_uses_confirmation_time_instead_of_attempt_start(self):
        self.invite(NOW - timedelta(seconds=120))
        self.store.db.execute('UPDATE actions SET sent_at=?,finished_at=? WHERE lead_id=1',
                              (stamp(NOW - timedelta(seconds=10)), stamp(NOW - timedelta(seconds=10))))
        self.store.db.commit()
        self.store.update(1, state='stopped')
        self.add_lead('next')
        self.cfg.update(campaign_invitation_limit=2, invitations_per_day=2)
        clock, waits = [NOW], []
        def pause(seconds):
            waits.append(seconds)
            clock[0] += timedelta(seconds=seconds)
        browser = FakeBrowser()
        Campaign(self.store, self.cfg, browser, now=lambda: clock[0], pause=pause).run()
        self.assertEqual(waits, [50])
        self.assertEqual(len(browser.sends), 1)

    def test_wait_that_crosses_closing_time_does_not_create_an_action(self):
        clock = [datetime(2026, 10, 21, 21, 59, 50, tzinfo=timezone.utc)]
        self.invite(clock[0] - timedelta(seconds=10))
        self.store.update(1, state='stopped')
        self.add_lead('next')
        self.cfg.update(campaign_invitation_limit=2, invitations_per_day=2)
        browser = FakeBrowser()
        with self.assertRaises(CyclePaused):
            Campaign(self.store, self.cfg, browser, now=lambda: clock[0],
                     pause=lambda seconds: clock.__setitem__(0, clock[0] + timedelta(seconds=seconds)), enforce_schedule=True).run()
        self.assertEqual(browser.sends, [])
        self.assertIsNone(self.store.db.execute('SELECT 1 FROM actions WHERE lead_id=2').fetchone())

    def test_preparation_that_crosses_closing_time_releases_only_unstarted_reservation(self):
        clock = [datetime(2026, 10, 21, 21, 59, 50, tzinfo=timezone.utc)]
        browser = FakeBrowser()
        def send(lead, message):
            clock[0] += timedelta(seconds=20)
            browser.before_action()
            browser.sends.append(lead['id'])
        browser.send_invitation = send
        with self.assertRaises(CyclePaused):
            Campaign(self.store, self.cfg, browser, now=lambda: clock[0], enforce_schedule=True).run()
        self.assertEqual(self.store.used('invite'), 0)
        self.assertEqual(self.store.lead(1)['state'], 'new')
        self.assertEqual(browser.sends, [])

    def test_stop_after_possible_connect_keeps_unknown_journal_without_retry(self):
        clock = [datetime(2026, 10, 21, 21, 59, 50, tzinfo=timezone.utc)]
        browser = FakeBrowser()
        def send(lead, message):
            browser.before_action()
            clock[0] += timedelta(seconds=20)
            browser.before_action()
        browser.send_invitation = send
        with self.assertRaises(CyclePaused):
            Campaign(self.store, self.cfg, browser, now=lambda: clock[0], enforce_schedule=True).run()
        self.assertEqual(self.store.unresolved(), 1)
        self.assertEqual(self.store.lead(1)['state'], 'needs_review')
        self.campaign(FakeBrowser(), clock[0] + timedelta(days=1)).run()
        self.assertEqual(self.store.unresolved(), 1)

    def test_quota_is_checked_again_at_the_click(self):
        self.add_lead('other')
        browser = FakeBrowser()
        def send(lead, message):
            self.store.begin(2, 'invite', 'Another confirmed reservation', NOW)
            self.store.sent(2, 'invite', NOW)
            browser.before_action()
            browser.sends.append(lead['id'])
        browser.send_invitation = send
        with self.assertRaises(CyclePaused):
            self.campaign(browser).run()
        self.assertEqual(browser.sends, [])
        self.assertEqual(self.store.used('invite'), 1)

    def test_midnight_during_preparation_reserves_the_new_calendar_day(self):
        clock = [datetime(2026, 10, 22, 3, 59, 50, tzinfo=timezone.utc)]
        self.cfg.update(working_days=list(range(7)), working_hours=[0, 24], campaign_invitation_limit=None)
        browser = FakeBrowser()
        def send(lead, message):
            clock[0] += timedelta(seconds=20)
            browser.before_action()
            browser.sends.append(lead['id'])
        browser.send_invitation = send
        Campaign(self.store, self.cfg, browser, now=lambda: clock[0], enforce_schedule=True).run()
        self.assertEqual(self.store.used('invite', day_start(clock[0], self.cfg)), 1)
        self.assertEqual(invite_capacity(self.store, self.cfg, clock[0]), 0)

    def test_repeated_dst_hour_does_not_reset_daily_capacity(self):
        from zoneinfo import ZoneInfo
        zone = ZoneInfo('America/New_York')
        first = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0).astimezone(timezone.utc)
        second = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1).astimezone(timezone.utc)
        self.invite(first)
        self.assertEqual(day_start(first, self.cfg), day_start(second, self.cfg))
        self.assertEqual(invite_capacity(self.store, {**self.cfg, 'campaign_invitation_limit': None}, second), 0)

    def test_os_termination_during_an_attempt_never_requeues_that_recipient(self):
        ready = Path(self.tmp.name) / 'child-ready'
        root = Path(__file__).resolve().parents[1]
        code = '''import os,sys,time
from pathlib import Path
from outreach import Store,utcnow
s=Store(sys.argv[1]);s.begin(1,'invite','Interrupted attempt',utcnow());s.click_started(1,'invite',utcnow())
Path(sys.argv[2]).write_text(str(os.getpid()))
time.sleep(300)
'''
        env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(root), sysconfig.get_path('purelib'))))
        child = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c', code, str(self.path), str(ready)],
                                 env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 15
            while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(ready.exists(), 'Owned test child did not reach committed journal barrier')
            self.assertEqual(int(ready.read_text()), child.pid)
            child.kill()
            child.wait(timeout=10)
            self.store.recover(NOW)
            self.assertEqual(self.store.lead(1)['state'], 'needs_review')
            self.assertEqual(self.store.unresolved(), 1)
            self.assertEqual(self.campaign(FakeBrowser()).run()['invitations_sent'], 0)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            child.stderr.close()
