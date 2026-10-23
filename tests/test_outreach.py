from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from outreach import Campaign, Store, StopRequested, StopRun, campaign_finished, canonical_profile, day_start, due_message, invite_capacity, load_settings, render, stamp, working_time
from outreach_errors import InvitationHeld, ProfileNeedsReview

NOW = datetime(2026, 10, 21, 16, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


class FakeBrowser:
    def __init__(self, state="new", reply=False, uncertain=False):
        self.state, self.reply, self.uncertain = state, reply, uncertain
        self.sends = []

    def open_profile(self, lead):
        return self.state

    def conversation(self, lead, known):
        from types import SimpleNamespace
        return SimpleNamespace(inbound=self.reply, unknown=False)

    def send_invitation(self, lead, message):
        self.sends.append((lead["id"], "invite", message))
        if self.uncertain:
            raise TimeoutError("Browser closed after Send was clicked")

    def send_message(self, lead, message, known):
        self.sends.append((lead["id"], "message", message))


class OutreachCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "campaign.sqlite3"
        self.store = Store(self.path)
        self.cfg = load_settings(ROOT / "settings.example.json")
        self.cfg.update(campaign_invitation_limit=1, invitations_per_run=1, invitations_per_day=1)
        self.add_lead()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add_lead(self, suffix="alex", first="Alex", city="Boston"):
        with self.store.db:
            self.store.db.execute("""
                INSERT INTO leads (url,first_name,full_name,company,title,city,region)
                VALUES (?,?,?,?,?,?,?)
            """, (f"https://www.linkedin.com/in/{suffix}/", first, f"{first} Example", "Oracle", "Engineering Manager", city, ""))

    def campaign(self, browser, now=NOW):
        return Campaign(self.store, self.cfg, browser, now=lambda: now, pause=lambda _: None)

    def invite(self, now=NOW):
        self.store.begin(1, "invite", render(self.store.lead(1), self.cfg)["invite"], now)
        self.store.sent(1, "invite", now)


class OutreachTests(OutreachCase):
    def test_public_profile_urls_normalize_and_reject_external_domains(self):
        self.assertEqual(canonical_profile("https://linkedin.com/in/ALEX/?trk=abc"), "https://www.linkedin.com/in/alex/")
        for value in ("https://linkedin.com.evil.test/in/alex/", "https://www.linkedin.com/company/oracle/",
                      "file:///in/alex/", "https://www.linkedin.com/in/a%2Fb/", "https://name:pw@linkedin.com/in/a/"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                canonical_profile(value)

    def test_import_deduplicates_without_resetting_progress(self):
        path = Path(self.tmp.name) / "leads.csv"
        path.write_text("First Name,Full Name,Company,LinkedIn Profile\nAlex,Alex Example,Oracle,https://linkedin.com/in/alex/?trk=abc\nAlex,Alex Example,Oracle,https://www.linkedin.com/in/alex/\n,Morgan Example,Oracle,https://linkedin.com/in/missing/\nOther,Other Person,Oracle,https://evil.test/in/other/\n", encoding="utf-8")
        self.invite()
        counts = self.store.import_csv(path)
        self.assertEqual(counts, {"added": 1, "duplicates": 2, "invalid_urls": 1, "updated": 1, "restored": 0})
        self.assertEqual(self.store.lead(1)["state"], "invited")
        self.assertEqual(self.store.lead(2)["state"], "new")
        self.assertEqual(self.store.lead(2)["first_name"], "Morgan")
        self.assertEqual(self.store.used("invite"), 1)

    def test_repaired_import_only_releases_source_data_holds_without_actions(self):
        from lead_data import SOURCE_DATA_REASON
        self.add_lead("wing", first="")
        self.add_lead("unknown", first="")
        self.store.update(2, state="needs_review", reason=SOURCE_DATA_REASON)
        self.store.begin(3, "invite", "previous message", NOW)
        self.store.uncertain(3, "invite", SOURCE_DATA_REASON)
        path = Path(self.tmp.name) / "repaired.csv"
        path.write_text("First Name,Full Name,Company,LinkedIn Profile\n,Wing Lau,Workday,https://linkedin.com/in/wing/\n,Guru Example,Oracle,https://linkedin.com/in/unknown/\n", encoding="utf-8")
        counts = self.store.import_csv(path)
        self.assertEqual(counts["restored"], 1)
        self.assertEqual(self.store.lead(2)["first_name"], "Wing")
        self.assertEqual(self.store.lead(2)["state"], "new")
        self.assertEqual(self.store.lead(3)["state"], "needs_review")
        self.assertEqual(self.store.unresolved(), 1)
        self.assertEqual(self.store.sent_messages(3), {})
        self.assertEqual(self.store.import_csv(path)["restored"], 0)

    def test_import_preserves_stopped_and_sent_history(self):
        self.invite()
        self.add_lead("stopped")
        self.store.update(2, state="stopped", reason="Stopped by operator")
        before = [tuple(row) for row in self.store.db.execute("SELECT * FROM actions")]
        path = Path(self.tmp.name) / "changed.csv"
        path.write_text("Full Name,Company,City,LinkedIn Profile\nAlex Example,Oracle,Seattle,https://linkedin.com/in/alex/\nMorgan Example,Workday,Boston,https://linkedin.com/in/stopped/\n", encoding="utf-8")
        self.store.import_csv(path)
        self.assertEqual(self.store.lead(1)["state"], "invited")
        self.assertEqual(self.store.lead(1)["invited_at"], stamp(NOW))
        self.assertEqual(self.store.lead(1)["city"], "Seattle")
        self.assertEqual(self.store.lead(2)["state"], "stopped")
        self.assertEqual(before, [tuple(row) for row in self.store.db.execute("SELECT * FROM actions")])

    def test_repaired_source_hold_can_send_without_manual_state_changes(self):
        from lead_data import SOURCE_DATA_REASON
        self.store.update(1, state="stopped", reason="Stopped by operator")
        self.add_lead("wing", first="")
        self.store.update(2, state="needs_review", reason=SOURCE_DATA_REASON)
        path = Path(self.tmp.name) / "wing.csv"
        path.write_text("Full Name,Company,City,LinkedIn Profile\nWing Lau,Workday,San Francisco,https://linkedin.com/in/wing/\n", encoding="utf-8")
        self.store.import_csv(path)
        browser = FakeBrowser()
        self.assertEqual(self.campaign(browser).run()["invitations_sent"], 1)
        self.assertEqual(browser.sends[0][0], 2)
        self.assertTrue(browser.sends[0][2].startswith("Hi Wing,"))
        self.assertEqual(self.store.lead(2)["state"], "invited")

    def test_render_recovers_first_name_from_full_name(self):
        self.add_lead("guru", first="")
        self.store.db.execute("UPDATE leads SET full_name='Guru Huchachar' WHERE id=2")
        self.store.db.commit()
        self.assertTrue(render(self.store.lead(2), self.cfg)["invite"].startswith("Hi Guru,"))

    def test_missing_location_produces_complete_note(self):
        self.add_lead("no-city", city="")
        note = render(self.store.lead(2), self.cfg)["invite"]
        self.assertNotIn("around", note)
        self.assertIn("engineering leaders.", note)

    def test_overlong_note_does_not_silently_truncate(self):
        self.cfg["connection_note"] = "x" * 301
        with self.assertRaises(ValueError):
            render(self.store.lead(1), self.cfg)

    def test_live_invitation_is_saved_and_campaign_pilot_stops_at_one(self):
        self.add_lead("second")
        browser = FakeBrowser()
        result = self.campaign(browser).run()
        self.assertEqual(result["invitations_sent"], 1)
        self.assertEqual(self.store.lead(1)["state"], "invited")
        self.assertEqual(self.store.lead(2)["state"], "new")
        self.assertEqual(invite_capacity(self.store, self.cfg, NOW + timedelta(days=2)), 0)

    def test_uncertain_send_is_not_retried_after_restart(self):
        browser = FakeBrowser(uncertain=True)
        self.assertEqual(self.campaign(browser).run()['invitations_sent'], 0)
        self.assertEqual(self.store.lead(1)["state"], "needs_review")
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.campaign(browser).run()['invitations_sent'], 0)
        self.assertEqual(len(browser.sends), 1)

    def test_crash_with_prepared_action_requires_reconciliation(self):
        self.store.begin(1, "invite", "hello", NOW)
        self.assertEqual(self.store.recover(), 1)
        self.assertEqual(self.store.lead(1)["state"], "needs_review")
        self.assertEqual(self.campaign(FakeBrowser()).run()['invitations_sent'], 0)
        self.store.reconcile(1, "sent", NOW + timedelta(hours=1))
        self.assertEqual(self.store.lead(1)["state"], "invited")
        self.assertEqual(self.store.unresolved(), 0)

    def test_explicit_not_sent_reconciliation_allows_retry(self):
        self.store.begin(1, "invite", "hello", NOW)
        self.store.recover()
        self.store.reconcile(1, "not-sent", NOW)
        self.assertEqual(self.store.lead(1)["state"], "new")
        self.campaign(FakeBrowser()).run()
        self.assertEqual(self.store.lead(1)["state"], "invited")

    def test_stopping_uncertain_send_still_counts_against_pilot_limit(self):
        self.add_lead("second")
        self.store.begin(1, "invite", "hello", NOW)
        self.store.recover()
        self.store.reconcile(1, "stop", NOW)
        self.assertEqual(self.store.unresolved(), 0)
        self.assertEqual(invite_capacity(self.store, self.cfg, NOW), 0)

    def test_existing_connection_and_pending_request_are_skipped(self):
        for state in ("connected", "pending"):
            with self.subTest(state=state):
                self.store.update(1, state="new")
                browser = FakeBrowser(state=state)
                self.campaign(browser).run()
                self.assertEqual(self.store.lead(1)["state"], "skipped")
                self.assertEqual(browser.sends, [])

    def test_identity_mismatch_holds_one_lead_and_continues_verified_profiles(self):
        self.add_lead("verified")
        browser = FakeBrowser()
        def open_profile(lead):
            if lead["id"] == 1:
                raise ProfileNeedsReview("Profile name mismatch")
            return "new"
        browser.open_profile = open_profile
        result = self.campaign(browser).run()
        self.assertEqual(result["invitations_sent"], 1)
        self.assertEqual(self.store.lead(1)["state"], "needs_review")
        self.assertIsNone(self.store.db.execute('SELECT 1 FROM actions WHERE lead_id=1').fetchone())
        self.assertEqual(browser.sends[0][0], 2)

    def test_identity_mismatch_during_acceptance_check_preserves_sent_history(self):
        self.invite()
        self.add_lead("verified")
        self.cfg["campaign_invitation_limit"] = 2
        self.cfg["invitations_per_day"] = 2
        browser = FakeBrowser()
        def open_profile(lead):
            if lead["id"] == 1:
                raise ProfileNeedsReview("Profile name mismatch")
            return "new"
        browser.open_profile = open_profile
        self.campaign(browser).run()
        self.assertEqual(self.store.lead(1)["state"], "needs_review")
        self.assertEqual(self.store.sent_messages(1).keys(), {"invite"})
        self.assertEqual(self.store.lead(2)["state"], "invited")

    def test_account_or_verification_stops_are_not_treated_as_profile_holds(self):
        self.add_lead("next")
        browser = FakeBrowser()
        def open_profile(lead):
            raise StopRun("LinkedIn needs verification")
        browser.open_profile = open_profile
        with self.assertRaises(StopRun):
            self.campaign(browser).run()
        self.assertEqual(browser.sends, [])
        self.assertEqual(self.store.lead(1)["state"], "new")
        self.assertEqual(self.store.lead(2)["state"], "new")

    def test_identity_error_during_a_prepared_send_holds_that_action(self):
        self.add_lead("next")
        browser = FakeBrowser()
        def send_invitation(lead, message):
            raise ProfileNeedsReview("Identity changed while preparing")
        browser.send_invitation = send_invitation
        self.campaign(browser).run()
        self.assertEqual(self.store.unresolved(), 1)
        self.assertEqual(self.store.lead(2)["state"], "new")

    def test_verified_invitation_hold_continues_without_exceeding_limits(self):
        self.add_lead("next")
        self.add_lead("over-limit")
        self.cfg.update(campaign_invitation_limit=None, invitations_per_run=2, invitations_per_day=2)
        browser = FakeBrowser()
        original_send = browser.send_invitation
        def send_invitation(lead, message):
            if lead['id'] == 1:
                raise InvitationHeld("Refreshed profile still offers Connect")
            original_send(lead, message)
        browser.send_invitation = send_invitation
        result = self.campaign(browser).run()
        self.assertEqual(result['invitations_sent'], 1)
        self.assertEqual(self.store.lead(1)['state'], 'needs_review')
        self.assertIsNone(self.store.lead(1)['invited_at'])
        self.assertEqual(self.store.lead(2)['state'], 'invited')
        self.assertEqual(self.store.lead(3)['state'], 'new')
        self.assertEqual(self.store.unresolved(), 0)
        self.assertEqual(self.store.used('invite'), 2)
        self.assertEqual(invite_capacity(self.store, self.cfg, NOW), 0)
        self.store.close()
        self.store = Store(self.path)
        self.campaign(browser, NOW + timedelta(days=1)).run()
        self.assertEqual([item[0] for item in browser.sends], [2, 3])
        self.assertEqual(self.store.lead(1)['state'], 'needs_review')

    def test_invitation_hold_cannot_clear_a_preexisting_uncertain_send(self):
        self.store.begin(1, 'invite', 'hello', NOW)
        self.store.uncertain(1, 'invite', 'Outcome unknown')
        with self.assertRaises(ValueError):
            self.store.hold_invitation(1, 'Unverified attempt to clear it')
        self.assertEqual(self.store.unresolved(), 1)
        self.assertEqual(self.store.lead(1)['reason'], 'Outcome unknown')

    def test_followup_clock_starts_when_acceptance_is_observed(self):
        self.invite(NOW - timedelta(days=10))
        browser = FakeBrowser(state="connected")
        self.campaign(browser).run()
        lead = self.store.lead(1)
        self.assertEqual(lead["accepted_at"], stamp(NOW))
        self.assertEqual(lead["state"], "accepted")
        self.assertEqual(browser.sends, [])
        self.assertIsNone(due_message(lead, self.cfg, NOW + timedelta(hours=23, minutes=59)))
        self.assertEqual(due_message(lead, self.cfg, NOW + timedelta(days=1)), "message_1")

    def test_full_sequence_and_second_followup_delay(self):
        self.invite(NOW - timedelta(days=2))
        self.store.update(1, state="accepted", accepted_at=stamp(NOW - timedelta(days=1)))
        browser = FakeBrowser(state="connected")
        self.campaign(browser).run()
        self.assertEqual(self.store.lead(1)["state"], "message_1_sent")
        self.assertIsNone(due_message(self.store.lead(1), self.cfg, NOW + timedelta(days=3, seconds=-1)))
        self.campaign(browser, NOW + timedelta(days=3)).run()
        self.assertEqual(self.store.lead(1)["state"], "completed")
        self.assertEqual(len(browser.sends), 2)
        self.campaign(browser, NOW + timedelta(days=4)).run()
        self.assertEqual(len(browser.sends), 2)

    def test_reply_stops_due_message(self):
        self.invite(NOW - timedelta(days=2))
        self.store.update(1, state="accepted", accepted_at=stamp(NOW - timedelta(days=1)))
        browser = FakeBrowser(state="connected", reply=True)
        self.campaign(browser).run()
        self.assertEqual(self.store.lead(1)["state"], "replied")
        self.assertEqual(browser.sends, [])

    def test_rolling_week_and_local_calendar_day_limits(self):
        self.cfg.update(campaign_invitation_limit=100, invitations_per_day=1, invitations_per_7_days=1)
        self.invite(NOW)
        self.assertEqual(invite_capacity(self.store, self.cfg, NOW + timedelta(days=1)), 0)
        self.assertEqual(invite_capacity(self.store, self.cfg, NOW + timedelta(days=7, seconds=1)), 1)
        self.assertEqual(day_start(NOW, self.cfg).hour, 4)

    def test_ongoing_campaign_reaches_25_without_counting_followup_messages(self):
        self.cfg.update(campaign_invitation_limit=None, invitations_per_run=25,
                        invitations_per_day=25, invitations_per_7_days=175, acceptance_checks_per_run=100)
        for number in range(1, 26):
            self.add_lead(f"daily-{number}")
        result = self.campaign(FakeBrowser()).run()
        self.assertEqual(result["invitations_sent"], 25)
        self.assertEqual(invite_capacity(self.store, self.cfg, NOW), 0)
        self.store.begin(1, "message_1", "A separate follow-up", NOW)
        self.store.sent(1, "message_1", NOW)
        self.assertEqual(self.store.used("invite"), 25)
        self.assertEqual(self.store.used("message"), 1)
        self.assertEqual(invite_capacity(self.store, self.cfg, NOW + timedelta(days=1)), 25)
        self.assertFalse(campaign_finished(self.store, self.cfg))

    def test_ongoing_campaign_finishes_when_no_eligible_or_active_leads_remain(self):
        self.cfg["campaign_invitation_limit"] = None
        self.store.update(1, state="stopped", reason="Stopped by operator")
        self.assertTrue(campaign_finished(self.store, self.cfg))

    def test_scheduler_uses_eastern_weekdays_and_hours(self):
        self.assertTrue(working_time(NOW, self.cfg))
        self.assertFalse(working_time(NOW.replace(hour=0), self.cfg))
        self.assertFalse(working_time(NOW + timedelta(days=3), self.cfg))

    def test_stop_request_prevents_a_new_send(self):
        browser = FakeBrowser()
        campaign = Campaign(self.store, self.cfg, browser, now=lambda: NOW,
                            pause=lambda _: None, stop_requested=lambda: True)
        with self.assertRaises(StopRequested):
            campaign.run()
        self.assertEqual(browser.sends, [])
        self.assertEqual(self.store.used("invite"), 0)

    def test_readonly_status_can_read_without_changing_progress(self):
        self.invite()
        reader = Store(self.path, readonly=True)
        try:
            self.assertEqual(reader.lead(1)["state"], "invited")
            with self.assertRaises(sqlite3.OperationalError):
                reader.update(1, state="new")
        finally:
            reader.close()
        self.assertEqual(self.store.lead(1)["state"], "invited")

    def test_watcher_finishes_only_after_pilot_sequence_stops(self):
        self.invite()
        self.assertFalse(campaign_finished(self.store, self.cfg))
        self.store.update(1, state="replied")
        self.assertTrue(campaign_finished(self.store, self.cfg))


if __name__ == "__main__":
    unittest.main()
