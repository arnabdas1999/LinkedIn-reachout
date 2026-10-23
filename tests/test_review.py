"""Automatic review never guesses that an absent message was not delivered."""
from datetime import timedelta
from pathlib import Path
import sqlite3

from playwright.sync_api import Error as BrowserError

from outreach import Store, campaign_finished, due_message, stamp
from outreach_errors import AccountBlocked, ContactNeedsReview, RetryCycle
from outreach_review import ReviewResult, review_contacts, review_work
from outreach_state import meta
from test_outreach import FakeBrowser, NOW, OutreachCase


class ReviewBrowser(FakeBrowser):
    def __init__(self, result):
        super().__init__()
        self.result, self.inspected = result, []

    def review_contact(self, lead, action, known):
        self.inspected.append(lead['id'])
        if callable(self.result):
            return self.result(lead, action, known)
        return self.result


class AutomaticReviewTests(OutreachCase):
    def hold(self, *, kind='invite', guarded=False, clicked=False, when=NOW):
        self.store.begin(1, kind, 'Recorded personalized text', when, guarded=guarded)
        if clicked:
            self.store.click_started(1, kind, when)
        self.store.uncertain(1, kind, 'Outcome uncertain', when)

    def run_review(self, result, now=NOW):
        browser = ReviewBrowser(result)
        counts = review_contacts(self.store, self.cfg, browser, lambda: now)
        self.assertEqual(browser.sends, [])
        return counts, browser

    def test_pending_confirms_legacy_invite_without_repeating_or_moving_quota(self):
        earlier = NOW - timedelta(days=1)
        self.hold(when=earlier)
        counts, _ = self.run_review(ReviewResult('sent', 'Verified Pending', 'pending'))
        action = self.store.db.execute('SELECT * FROM actions').fetchone()
        self.assertEqual(action['status'], 'sent')
        self.assertEqual(action['quota_at'], stamp(earlier))
        self.assertEqual(self.store.lead(1)['state'], 'invited')
        self.assertEqual(counts['confirmed'], 1)
        self.assertEqual(self.store.used('invite'), 1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM review_events').fetchone()[0], 1)

    def test_followup_confirmation_uses_observation_time_for_next_delay(self):
        self.invite(NOW - timedelta(days=5))
        self.store.update(1, state='accepted', accepted_at=stamp(NOW - timedelta(days=4)))
        self.hold(kind='message_1', when=NOW - timedelta(days=3))
        self.run_review(ReviewResult('sent', 'Exact outbound message', 'connected'))
        lead = self.store.lead(1)
        self.assertEqual(lead['message_1_at'], stamp(NOW))
        self.assertIsNone(due_message(lead, self.cfg, NOW + timedelta(days=2)))
        self.assertEqual(due_message(lead, self.cfg, NOW + timedelta(days=3)), 'message_2')

    def test_absent_message_and_legacy_null_click_marker_never_authorize_retry(self):
        self.hold()
        counts, _ = self.run_review(ReviewResult('ready', 'No message visible', 'new'))
        self.assertEqual(counts['deferred'], 1)
        self.assertEqual(self.store.unresolved(), 1)
        self.assertEqual(self.store.lead(1)['state'], 'needs_review')

    def test_new_guard_proves_no_dispatch_and_archives_before_requeue(self):
        self.hold(guarded=True)
        counts, _ = self.run_review(ReviewResult('ready', 'Identity and current state checked', 'new'))
        self.assertEqual(counts['resumed'], 1)
        self.assertEqual(self.store.used('invite'), 0)
        self.assertEqual(self.store.lead(1)['automatic_resumes'], 1)
        record = self.store.db.execute("SELECT action_snapshot FROM review_events WHERE outcome='not_dispatched'").fetchone()[0]
        self.assertIn('Recorded personalized text', record)
        self.assertIn('unknown', record)

    def test_click_marker_prevents_requeue_even_with_a_new_guard(self):
        self.hold(guarded=True, clicked=True)
        self.run_review(ReviewResult('ready', 'Connect is visible again', 'new'))
        self.assertEqual(self.store.unresolved(), 1)
        self.assertEqual(self.store.lead(1)['automatic_resumes'], 0)

    def test_reply_stops_uncertain_sequence_and_recovery_does_not_reopen_it(self):
        self.hold()
        self.run_review(ReviewResult('replied', 'Reply observed', 'connected'))
        self.store.recover(NOW)
        self.assertEqual(self.store.lead(1)['state'], 'replied')
        self.assertEqual(self.store.used('invite'), 1)
        self.assertEqual(review_work(self.store, self.cfg), [])

    def test_confirmed_send_with_reply_ends_sequence(self):
        self.hold()
        self.run_review(ReviewResult('sent', 'Confirmed plus reply', 'connected', replied=True))
        self.assertEqual(self.store.lead(1)['state'], 'replied')
        self.assertEqual(self.store.unresolved(), 0)

    def test_confirmation_does_not_resume_unreadable_history(self):
        self.hold()
        self.run_review(ReviewResult('sent', 'Confirmed but history incomplete', 'connected', history_safe=False))
        self.assertEqual(self.store.lead(1)['state'], 'needs_review')
        self.assertEqual(self.store.lead(1)['review_attempts'], 1)
        self.assertEqual(self.store.unresolved(), 0)

    def test_explicit_stop_survives_recovery_and_automatic_review(self):
        self.hold()
        self.store.update(1, state='stopped')
        self.store.recover(NOW)
        counts, browser = self.run_review(ReviewResult('sent', 'Pending', 'pending'))
        self.assertEqual(counts['checked'], 0)
        self.assertEqual(browser.inspected, [])
        self.assertEqual(self.store.lead(1)['state'], 'stopped')

    def test_failed_reviews_back_off_persist_and_eventually_park(self):
        self.hold()
        self.assertFalse(campaign_finished(self.store, self.cfg))
        self.run_review(ReviewResult('unresolved', 'Not enough evidence'))
        self.assertEqual(review_work(self.store, self.cfg, NOW), [])
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.lead(1)['review_attempts'], 1)
        self.run_review(ReviewResult('unresolved', 'Still insufficient'), NOW + timedelta(hours=1))
        counts, _ = self.run_review(ReviewResult('unresolved', 'Still insufficient'), NOW + timedelta(hours=3))
        self.assertEqual(counts['parked'], 1)
        self.assertEqual(self.store.lead(1)['review_exhausted'], 1)
        self.assertEqual(review_work(self.store, self.cfg), [])
        self.assertTrue(campaign_finished(self.store, self.cfg))
        self.assertEqual(self.store.used('invite'), 1)

    def test_repeated_automatic_resumes_have_a_persistent_limit(self):
        self.hold(guarded=True)
        self.store.update(1, automatic_resumes=self.cfg['automatic_resume_limit'])
        counts, _ = self.run_review(ReviewResult('ready', 'Readable now', 'new'))
        self.assertEqual(counts['parked'], 1)
        self.assertEqual(self.store.unresolved(), 1)

    def test_ordinary_read_only_hold_resumes_without_losing_prior_stage(self):
        self.invite(NOW - timedelta(days=2))
        self.store.update(1, state='needs_review', accepted_at=stamp(NOW - timedelta(days=1)))
        self.run_review(ReviewResult('ready', 'Verified history', 'connected'))
        self.assertEqual(self.store.lead(1)['state'], 'accepted')
        self.assertEqual(self.store.used('invite'), 1)

    def test_unavailable_unsent_contact_is_excluded_automatically(self):
        self.store.update(1, state='needs_review')
        counts, _ = self.run_review(ReviewResult('unavailable', 'Profile removed', 'unavailable'))
        self.assertEqual(counts['stopped'], 1)
        self.assertEqual(self.store.lead(1)['state'], 'skipped')

    def test_existing_connection_without_tracked_invite_is_not_enrolled(self):
        self.store.update(1, state='needs_review')
        self.run_review(ReviewResult('ready', 'Already connected', 'connected'))
        self.assertEqual(self.store.lead(1)['state'], 'skipped')

    def test_verified_company_enrichment_survives_csv_reimport(self):
        self.store.db.execute("UPDATE leads SET state='needs_review',company='' WHERE id=1")
        self.store.db.commit()
        self.run_review(ReviewResult('ready', 'Single visible employer', 'new', company='Verified Company'))
        csv = Path(self.tmp.name) / 'source.csv'
        csv.write_text('Full Name,Company,LinkedIn Profile\nAlex Example,,https://www.linkedin.com/in/alex/\n')
        self.store.import_csv(csv)
        self.assertEqual(self.store.lead(1)['company'], 'Verified Company')
        self.assertEqual(self.store.lead(1)['state'], 'new')

    def test_one_bad_profile_does_not_block_a_readable_contact(self):
        self.add_lead('healthy')
        for lead_id in (1, 2):
            self.store.update(lead_id, state='needs_review')
        def inspect(lead, action, known):
            if lead['id'] == 1:
                raise ContactNeedsReview('Name remains ambiguous')
            return ReviewResult('ready', 'Verified', 'new')
        counts, browser = self.run_review(inspect)
        self.assertEqual(browser.inspected, [1, 2])
        self.assertEqual(counts['resumed'], 1)
        self.assertEqual(self.store.lead(1)['state'], 'needs_review')

    def test_review_budget_limits_reads_in_a_cycle(self):
        self.add_lead('second')
        for lead_id in (1, 2):
            self.store.update(lead_id, state='needs_review')
        self.cfg['review_checks_per_run'] = 1
        counts, browser = self.run_review(ReviewResult('unresolved', 'Unknown'))
        self.assertEqual(counts['checked'], 1)
        self.assertEqual(browser.inspected, [1])

    def test_account_block_still_requires_login_and_is_not_retried(self):
        self.hold()
        def inspect(*args):
            raise AccountBlocked('Verification required')
        with self.assertRaises(AccountBlocked):
            self.run_review(inspect)
        self.assertEqual(self.store.lead(1)['review_attempts'], 0)

    def test_dead_browser_recovers_without_exhausting_contact_reviews(self):
        self.hold()
        def inspect(*args):
            raise BrowserError('Target page, context or browser has been closed')
        with self.assertRaises(RetryCycle):
            self.run_review(inspect)
        self.assertEqual(self.store.lead(1)['review_attempts'], 0)

    def test_restored_database_audit_cannot_be_bypassed(self):
        self.hold()
        meta(self.store.db, 'restore_review_required', '1')
        with self.assertRaises(AccountBlocked):
            self.run_review(ReviewResult('sent', 'Pending', 'pending'))
        self.assertEqual(self.store.unresolved(), 1)

    def test_audit_write_failure_cannot_release_a_reservation(self):
        self.hold(guarded=True)
        self.store.db.execute("CREATE TRIGGER reject_review BEFORE INSERT ON review_events BEGIN SELECT RAISE(ABORT,'Fixture audit failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.run_review(ReviewResult('ready', 'Never dispatched', 'new'))
        self.assertEqual(self.store.unresolved(), 1)
        self.assertEqual(self.store.lead(1)['state'], 'needs_review')

    def test_review_can_be_disabled(self):
        self.hold()
        self.cfg['automatic_review'] = False
        counts, browser = self.run_review(ReviewResult('sent', 'Pending', 'pending'))
        self.assertEqual(counts['checked'], 0)
        self.assertEqual(browser.inspected, [])

    def test_template_change_reopens_parked_reviews_without_authorizing_a_resend(self):
        self.hold()
        self.run_review(ReviewResult('unresolved', 'Unknown'))
        self.store.update(1, review_exhausted=1, review_attempts=3)
        self.assertTrue(campaign_finished(self.store, self.cfg))
        self.cfg['message_1'] = 'Updated message for {first_name}'
        self.assertFalse(campaign_finished(self.store, self.cfg))
        self.run_review(ReviewResult('ready', 'Still no send confirmation', 'new'))
        self.assertEqual(self.store.lead(1)['review_attempts'], 1)
        self.assertEqual(self.store.unresolved(), 1)

    def test_corrected_csv_reopens_an_identity_hold_without_erasing_send_history(self):
        self.hold()
        self.store.update(1, review_exhausted=1, review_attempts=3)
        csv = Path(self.tmp.name) / 'fixed.csv'
        csv.write_text('Full Name,Company,LinkedIn Profile\nAlex Example Corrected,Oracle,https://www.linkedin.com/in/alex/\n')
        self.store.import_csv(csv)
        self.assertEqual(self.store.lead(1)['review_exhausted'], 0)
        self.assertEqual(self.store.lead(1)['state'], 'needs_review')
        self.assertEqual(self.store.unresolved(), 1)
