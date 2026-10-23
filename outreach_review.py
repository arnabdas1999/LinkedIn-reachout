"""Bounded automatic contact review; observations never click Connect or Send."""

from dataclasses import dataclass
from datetime import timedelta
import json
import logging
import hashlib

from playwright.sync_api import Error as BrowserError

from outreach_errors import AccountBlocked, ContactNeedsReview, ContactNotReady, RetryCycle, StopRequested, StopRun

LOG = logging.getLogger('outreach')


@dataclass(frozen=True)
class ReviewResult:
    outcome: str
    detail: str
    state: str = ''
    company: str = ''
    replied: bool = False
    history_safe: bool = True


def policy_fingerprint(cfg):
    from outreach_config import TEMPLATE_KEYS, VERSION
    return hashlib.sha256(json.dumps([VERSION, cfg['automatic_resume_limit'],
        *[cfg[key] for key in TEMPLATE_KEYS]], ensure_ascii=True).encode()).hexdigest()


def review_work(store, cfg, now=None):
    if not cfg.get('automatic_review', False):
        return []
    if 'review_attempts' not in {row[1] for row in store.db.execute('PRAGMA table_info(leads)')}:
        return []  # Read-only supervisor may inspect the database before migration.
    from outreach import stamp
    from outreach_state import meta
    prior = meta(store.db, 'review_policy_fingerprint')
    changed = prior is not None and prior != policy_fingerprint(cfg)
    sql, args = "SELECT * FROM leads WHERE state='needs_review'", []
    if not changed:
        sql += ' AND review_exhausted=0 AND review_attempts<?'
        args.append(cfg['review_retry_limit'])
    if now is not None and not changed:
        sql += ' AND (review_next_at IS NULL OR review_next_at<=?)'
        args.append(stamp(now))
    return store.db.execute(sql + " ORDER BY coalesce(review_next_at,''),id LIMIT ?",
                            (*args, cfg['review_checks_per_run'])).fetchall()


def event(store, lead, outcome, detail, now, action=None):
    from outreach import stamp
    store.db.execute('INSERT INTO review_events (lead_id,at,outcome,detail,action_snapshot) VALUES (?,?,?,?,?)',
                     (lead['id'], stamp(now), outcome, detail, json.dumps(dict(action)) if action else '{}'))


def defer(store, cfg, lead, detail, now):
    from outreach import stamp
    count = lead['review_attempts'] + 1
    exhausted = count >= cfg['review_retry_limit']
    due = None if exhausted else stamp(now + timedelta(minutes=min(1440, cfg['review_interval_minutes'] * 2 ** (count - 1))))
    with store.db:
        event(store, lead, 'parked' if exhausted else 'deferred', detail, now)
        store.db.execute('''UPDATE leads SET review_attempts=?,review_next_at=?,review_exhausted=?,
            review_last_result=? WHERE id=?''', (count, due, int(exhausted), detail, lead['id']))
    LOG.info('AUTO REVIEW %s #%s: %s', 'PARKED' if exhausted else 'DEFERRED', lead['id'], detail)
    return 'parked' if exhausted else 'deferred'


def resolved(store, lead, state, detail, now, *, resumed=False):
    with store.db:
        event(store, lead, state, detail, now)
        store.db.execute('''UPDATE leads SET state=?,reason=?,failure_count=0,next_check_at=NULL,
            review_next_at=NULL,review_exhausted=0,review_last_result=?,automatic_resumes=automatic_resumes+?
            WHERE id=?''', (state, '' if state in ('new','invited','accepted','message_1_sent') else detail,
                           detail, int(resumed), lead['id']))


def confirm(store, lead, action, result, now):
    from outreach import stamp
    kind = action['kind']
    state, column = {'invite': ('invited', 'invited_at'), 'message_1': ('message_1_sent', 'message_1_at'),
                     'message_2': ('completed', 'message_2_at')}[kind]
    if result.replied:
        state = 'replied'
    elif not result.history_safe:
        state = 'needs_review'
    with store.db:
        event(store, lead, 'confirmed_' + kind, result.detail, now, action)
        # Confirmation is an observation, not another send. Keep the original
        # quota reservation and completion time; use observation time for a
        # conservative delay before the next follow-up.
        store.db.execute("UPDATE actions SET status='sent',sent_at=?,detail=? WHERE id=?",
                         (stamp(now), result.detail, action['id']))
        store.db.execute(f'''UPDATE leads SET state=?,{column}=?,reason=?,failure_count=0,
            next_check_at=NULL,review_next_at=NULL,review_last_result=? WHERE id=?''',
                         (state, stamp(now), result.detail if state == 'needs_review' else '', result.detail, lead['id']))
    return state


def review_contacts(store, cfg, browser, now, check_stop=lambda: None):
    counts = dict(checked=0, confirmed=0, resumed=0, stopped=0, deferred=0, parked=0)
    inspect = getattr(browser, 'review_contact', None)
    if not cfg.get('automatic_review', False) or not callable(inspect):
        return counts
    from outreach import render
    from outreach_state import meta
    if meta(store.db, 'restore_review_required') == '1':
        raise AccountBlocked('Restored state requires an audit before automatic review')
    fingerprint = policy_fingerprint(cfg)
    previous = meta(store.db, 'review_policy_fingerprint')
    if previous and previous != fingerprint:
        with store.db:
            store.db.execute("""UPDATE leads SET review_attempts=0,review_exhausted=0,review_next_at=NULL,
                review_last_result='Templates or review policy changed; verify again' WHERE state='needs_review'""")
    if previous != fingerprint:
        meta(store.db, 'review_policy_fingerprint', fingerprint)
    for lead in review_work(store, cfg, now()):
        check_stop()
        action = store.db.execute("SELECT * FROM actions WHERE lead_id=? AND status IN ('prepared','unknown','cancelled') ORDER BY id DESC LIMIT 1",
                                  (lead['id'],)).fetchone()
        counts['checked'] += 1
        try:
            result = inspect(dict(lead), dict(action) if action else None, store.sent_messages(lead['id']))
        except (AccountBlocked, StopRequested):
            raise
        except BrowserError as exc:
            if any(token in str(exc).casefold() for token in ('browser has been closed', 'target page, context or browser has been closed',
                    'connection closed', 'net::err_internet_disconnected', 'net::err_name_not_resolved', 'net::err_connection_reset')):
                raise RetryCycle(str(exc)) from exc
            counts[defer(store, cfg, lead, str(exc), now())] += 1
            continue
        except (ContactNeedsReview, ContactNotReady, StopRun, TimeoutError, ConnectionError) as exc:
            counts[defer(store, cfg, lead, str(exc), now())] += 1
            continue
        if not isinstance(result, ReviewResult):
            raise TypeError('Browser review must return verified ReviewResult evidence')
        if result.outcome == 'sent' and action:
            state = confirm(store, lead, action, result, now())
            counts['confirmed'] += 1
            if state == 'needs_review':
                counts[defer(store, cfg, store.lead(lead['id']), 'Send confirmed; conversation history remains unreadable', now())] += 1
        elif result.outcome == 'replied':
            resolved(store, lead, 'replied', result.detail, now())
            counts['stopped'] += 1
        elif result.outcome == 'unavailable' and not action:
            resolved(store, lead, 'skipped', result.detail, now())
            counts['stopped'] += 1
        elif result.outcome == 'ready':
            if action and not (action['guard_version'] == 1 and action['click_started_at'] is None):
                counts[defer(store, cfg, lead, 'No positive send confirmation; absence alone cannot authorize a resend', now())] += 1
                continue
            if lead['automatic_resumes'] >= cfg['automatic_resume_limit']:
                store.update(lead['id'], review_exhausted=1, review_next_at=None,
                             review_last_result='Automatic requeue limit reached; contact remains excluded')
                with store.db:
                    event(store, lead, 'parked', 'Automatic requeue limit reached', now(), action)
                counts['parked'] += 1
                continue
            if not action and ((result.state in ('pending','connected') and not lead['invited_at']) or
                               (result.state == 'new' and lead['invited_at'])):
                resolved(store, lead, 'skipped', 'Connection changed or already exists outside the tracked sequence', now())
                counts['stopped'] += 1
                continue
            enriched = dict(lead)
            if not enriched['company'] and result.company:
                enriched['company'] = result.company
            try:
                render(enriched, cfg)
            except ValueError as exc:
                counts[defer(store, cfg, lead, str(exc), now())] += 1
                continue
            with store.db:
                if result.company and not lead['company']:
                    store.db.execute('UPDATE leads SET company=?,observed_company=? WHERE id=?',
                                     (result.company, result.company, lead['id']))
                if action:
                    # Archive before releasing only a proven never-dispatched
                    # reservation. A crash cannot lose the prior attempt record.
                    event(store, lead, 'not_dispatched', result.detail, now(), action)
                    store.db.execute('DELETE FROM actions WHERE id=?', (action['id'],))
                state = 'message_1_sent' if lead['message_1_at'] else 'accepted' if lead['accepted_at'] else 'invited' if lead['invited_at'] else 'new'
                resolved(store, lead, state, result.detail, now(), resumed=True)
            counts['resumed'] += 1
        else:
            counts[defer(store, cfg, lead, result.detail, now())] += 1
    if counts['checked']:
        LOG.info('AUTO REVIEW summary: %s', counts)
    return counts
