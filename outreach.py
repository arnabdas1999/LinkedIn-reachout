"""Local LinkedIn campaign. Preview is offline; only --live can send anything."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from zoneinfo import ZoneInfo
from playwright.sync_api import Error as BrowserError

from outreach_config import VERSION, load_settings, read_json, resolve_path, state_path
from outreach_errors import (AccountBlocked, ContactNeedsReview, ContactNotReady, CyclePaused, InvitationHeld,
                             RetryCycle, StopRequested, StopRun)

from lead_data import (SOURCE_DATA_REASON, SOURCE_DATA_REASONS, canonical_profile,
                       name_parts, read_clean_csv)

ROOT = Path(__file__).resolve().parent
UTC = timezone.utc
LOG = logging.getLogger("outreach")
ACTIVE = ("invited", "accepted", "message_1_sent")


def utcnow():
    return datetime.now(UTC)


def stamp(value):
    return value.astimezone(UTC).isoformat(timespec="seconds")


def parsed(value):
    return datetime.fromisoformat(value).astimezone(UTC) if value else None


def render(lead, cfg):
    first = lead["first_name"]
    if not first:
        parts = name_parts(lead["full_name"])
        first = parts[0] if parts else ""
    values = {
        "first_name": first, "company": lead["company"],
        "location": lead["city"] or lead["region"],
    }
    key = "connection_note" if values["location"] else "connection_note_without_location"
    messages = {
        "invite": cfg[key].format(**values),
        "message_1": cfg["message_1"].format(**values),
        "message_2": cfg["message_2"].format(**values),
    }
    if not values["first_name"] or not values["company"]:
        raise ValueError("Missing first name or company; review this lead")
    if len(messages["invite"]) > 300:
        raise ValueError("Personalized invitation exceeds 300 characters")
    return messages


class Store:
    def __init__(self, path, readonly=False):
        existed = Path(path).is_file()
        target = Path(path).resolve().as_uri() + "?mode=ro" if readonly else str(path)
        self.db = sqlite3.connect(target, timeout=10, uri=readonly)
        try:
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            if readonly:
                return
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE,
                    first_name TEXT NOT NULL, full_name TEXT NOT NULL,
                    company TEXT NOT NULL, title TEXT NOT NULL,
                    city TEXT NOT NULL, region TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'new', reason TEXT NOT NULL DEFAULT '',
                    invited_at TEXT, accepted_at TEXT, message_1_at TEXT, message_2_at TEXT,
                    last_checked_at TEXT, next_check_at TEXT
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL REFERENCES leads(id),
                    kind TEXT NOT NULL, status TEXT NOT NULL,
                    started_at TEXT NOT NULL, sent_at TEXT, message TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '', UNIQUE(lead_id, kind)
                );
            """)
            from outreach_state import migrate
            migrate(self.db, path, existed)
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def lead(self, lead_id):
        return self.db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()

    def update(self, lead_id, **values):
        allowed = {"state", "reason", "invited_at", "accepted_at", "message_1_at",
                   "message_2_at", "last_checked_at", "next_check_at", "failure_count"}
        if not values or not set(values) <= allowed:
            raise ValueError("Invalid lead update")
        with self.db:
            self.db.execute(
                "UPDATE leads SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?",
                (*values.values(), lead_id),
            )

    def import_csv(self, path):
        _, rows, report = read_clean_csv(path)
        added = updated = restored = 0
        duplicate, invalid = report["duplicates_removed"], len(report["invalid_url_rows"])
        with self.db:
            for row in rows:
                try:
                    url = canonical_profile(row["LinkedIn Profile"])
                except ValueError:
                    continue
                first, company = row["First Name"].strip(), row["Company"].strip()
                state = "new" if first and company else "needs_review"
                reason = "" if state == "new" else SOURCE_DATA_REASON
                fields = (first, row["Full Name"], company, row.get("Job Title", ""),
                          row.get("City", ""), row.get("State or Province", ""))
                cursor = self.db.execute("""
                    INSERT OR IGNORE INTO leads
                    (url, first_name, full_name, company, title, city, region, state, reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (url, *fields, state, reason))
                if cursor.rowcount:
                    added += 1
                else:
                    duplicate += 1
                    existing = self.db.execute("SELECT * FROM leads WHERE url=?", (url,)).fetchone()
                    if fields != tuple(existing[key] for key in ("first_name", "full_name", "company", "title", "city", "region")):
                        self.db.execute("""UPDATE leads SET first_name=?,full_name=?,company=?,title=?,city=?,region=?
                                           WHERE id=?""", (*fields, existing["id"]))
                        updated += 1
                    # Only source-data holds can be repaired automatically. A
                    # possible send or stopped sequence must never be requeued.
                    if state == "new" and existing["state"] == "needs_review" and existing["reason"] in SOURCE_DATA_REASONS:
                        action = self.db.execute("SELECT 1 FROM actions WHERE lead_id=? LIMIT 1", (existing["id"],)).fetchone()
                        if not action and not any(existing[key] for key in ("invited_at", "accepted_at", "message_1_at", "message_2_at")):
                            self.db.execute("UPDATE leads SET state='new',reason='' WHERE id=?", (existing["id"],))
                            restored += 1
        return {"added": added, "duplicates": duplicate, "invalid_urls": invalid,
                "updated": updated, "restored": restored}

    def candidates(self, company="", now=None, include_deferred=False):
        sql, args = "SELECT * FROM leads WHERE state='new'", []
        if not include_deferred:
            sql += " AND (next_check_at IS NULL OR next_check_at<=?)"
            args.append(stamp(now or utcnow()))
        if company:
            sql += " AND lower(company)=lower(?)"
            args.append(company)
        return self.db.execute(sql + " ORDER BY id", args).fetchall()

    def active_due(self, now, limit):
        return self.db.execute("""
            SELECT * FROM leads WHERE state IN ('invited','accepted','message_1_sent')
            AND (next_check_at IS NULL OR next_check_at<=?)
            ORDER BY coalesce(next_check_at, ''), id LIMIT ?
        """, (stamp(now), limit)).fetchall()

    def begin(self, lead_id, kind, message, now):
        # Commit BEFORE clicking anything that might send. A crash never retries it.
        with self.db:
            if self.db.execute("SELECT 1 FROM actions WHERE lead_id=? AND status IN ('prepared','unknown')", (lead_id,)).fetchone():
                raise ValueError('This contact has an unresolved send; reconcile it before another action')
            self.db.execute("""
                INSERT INTO actions (lead_id,kind,status,started_at,quota_at,message)
                VALUES (?,?,'prepared',?,?,?)
            """, (lead_id, kind, stamp(now), stamp(now), message))

    def sent(self, lead_id, kind, now):
        state, column = {
            "invite": ("invited", "invited_at"),
            "message_1": ("message_1_sent", "message_1_at"),
            "message_2": ("completed", "message_2_at"),
        }[kind]
        with self.db:
            cursor = self.db.execute("""
                UPDATE actions SET status='sent',sent_at=?,finished_at=?,quota_at=? WHERE lead_id=? AND kind=?
                AND status IN ('prepared','unknown','cancelled')
            """, (stamp(now), stamp(now), stamp(now), lead_id, kind))
            if cursor.rowcount != 1:
                raise ValueError("No pending action to confirm")
            self.db.execute(
                f"UPDATE leads SET state=?,{column}=?,reason='',next_check_at=NULL WHERE id=?",
                (state, stamp(now), lead_id),
            )

    def uncertain(self, lead_id, kind, detail, now=None):
        with self.db:
            self.db.execute("""
                UPDATE actions SET status='unknown',detail=?,finished_at=?
                WHERE lead_id=? AND kind=? AND status='prepared'
            """, (detail, stamp(now or utcnow()), lead_id, kind))
            self.db.execute("UPDATE leads SET state='needs_review',reason=? WHERE id=?",
                            (detail, lead_id))

    def hold_invitation(self, lead_id, detail, now=None):
        # Keep the attempt in the journal and quota, without requeueing it.
        # Only a freshly verified profile still offering Connect qualifies.
        with self.db:
            cursor = self.db.execute("""
                UPDATE actions SET status='cancelled',detail=?,finished_at=?
                WHERE lead_id=? AND kind='invite' AND status='prepared'
            """, (detail, stamp(now or utcnow()), lead_id))
            if cursor.rowcount != 1:
                raise ValueError("No prepared invitation to hold")
            self.db.execute("UPDATE leads SET state='needs_review',reason=? WHERE id=?",
                            (detail, lead_id))

    def recover(self, now=None):
        rows = self.db.execute("SELECT * FROM actions WHERE status='prepared'").fetchall()
        for row in rows:
            self.uncertain(row["lead_id"], row["kind"],
                           "Previous run stopped during a send. Check LinkedIn before reconciling.", now)
        with self.db:
            self.db.execute("""UPDATE leads SET state='needs_review',reason=(
                SELECT detail FROM actions WHERE lead_id=leads.id AND status='unknown' ORDER BY id DESC LIMIT 1)
                WHERE id IN (SELECT lead_id FROM actions WHERE status='unknown')""")
        return len(rows)

    def next_retry(self):
        value = self.db.execute("""SELECT min(next_check_at) FROM leads
            WHERE state IN ('new','invited','accepted','message_1_sent') AND next_check_at IS NOT NULL""").fetchone()[0]
        return parsed(value)

    def unresolved(self):
        return self.db.execute("SELECT count(*) FROM actions WHERE status IN ('prepared','unknown')").fetchone()[0]

    def used(self, kind, since=None, exclude=None):
        sql = "SELECT count(*) FROM actions WHERE status IN ('prepared','unknown','sent','cancelled')"
        args = []
        sql += " AND " + ("kind='invite'" if kind == "invite" else "kind IN ('message_1','message_2')")
        if since:
            sql += " AND coalesce(quota_at,started_at)>=?"
            args.append(stamp(since))
        if exclude:
            sql += ' AND NOT (lead_id=? AND kind=?)'
            args.extend(exclude)
        return self.db.execute(sql, args).fetchone()[0]

    def last_send(self, exclude=None):
        sql = "SELECT max(coalesce(finished_at,sent_at,started_at)) FROM actions WHERE status IN ('prepared','unknown','sent','cancelled')"
        args = ()
        if exclude:
            sql += ' AND NOT (lead_id=? AND kind=?)'
            args = exclude
        value = self.db.execute(sql, args).fetchone()[0]
        return parsed(value)

    def click_started(self, lead_id, kind, now):
        with self.db:
            cursor = self.db.execute("UPDATE actions SET click_started_at=coalesce(click_started_at,?),quota_at=? WHERE lead_id=? AND kind=? AND status='prepared'",
                            (stamp(now), stamp(now), lead_id, kind))
            if cursor.rowcount != 1:
                raise ValueError('No journal reservation for the requested click')

    def release_unstarted(self, lead_id, kind):
        with self.db:
            row = self.db.execute("DELETE FROM actions WHERE lead_id=? AND kind=? AND status='prepared' AND click_started_at IS NULL", (lead_id, kind))
            return row.rowcount == 1

    def sent_messages(self, lead_id):
        return {row["kind"]: row["message"] for row in self.db.execute(
            "SELECT kind,message FROM actions WHERE lead_id=? AND status='sent'", (lead_id,)
        )}

    def reconcile(self, lead_id, outcome, now):
        row = self.db.execute("""
            SELECT * FROM actions WHERE lead_id=? AND status IN ('unknown','cancelled')
            ORDER BY id DESC LIMIT 1
        """, (lead_id,)).fetchone()
        if row is None:
            raise ValueError("This lead has no uncertain or cancelled send to reconcile")
        if outcome == "sent":
            self.sent(lead_id, row["kind"], parsed(row["started_at"]))
        elif outcome == "not-sent":
            # Explicit operator reconciliation is the only path that permits a retry.
            state = {"invite": "new", "message_1": "accepted", "message_2": "message_1_sent"}[row["kind"]]
            with self.db:
                self.db.execute("DELETE FROM actions WHERE id=?", (row["id"],))
                self.db.execute("UPDATE leads SET state=?,reason='',failure_count=0,next_check_at=NULL WHERE id=?", (state, lead_id))
        else:
            with self.db:
                self.db.execute("UPDATE actions SET status='cancelled',detail='Stopped by operator' WHERE id=?", (row["id"],))
                self.db.execute("UPDATE leads SET state='stopped',reason='Stopped by operator' WHERE id=?", (lead_id,))


def due_message(lead, cfg, now):
    if lead["state"] == "accepted" and parsed(lead["accepted_at"]):
        if now >= parsed(lead["accepted_at"]) + timedelta(days=cfg["message_1_delay_days"]):
            return "message_1"
    if lead["state"] == "message_1_sent" and parsed(lead["message_1_at"]):
        if now >= parsed(lead["message_1_at"]) + timedelta(days=cfg["message_2_delay_days"]):
            return "message_2"
    return None


def day_start(now, cfg):
    return now.astimezone(ZoneInfo(cfg["timezone"])).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def invite_capacity(store, cfg, now):
    limits = [
        cfg["invitations_per_run"],
        cfg["invitations_per_day"] - store.used("invite", day_start(now, cfg)),
        cfg["invitations_per_7_days"] - store.used("invite", now - timedelta(days=7)),
    ]
    if cfg["campaign_invitation_limit"] is not None:
        limits.append(cfg["campaign_invitation_limit"] - store.used("invite"))
    return max(0, min(limits))


def working_time(now, cfg):
    local = now.astimezone(ZoneInfo(cfg["timezone"]))
    return local.weekday() in cfg["working_days"] and cfg["working_hours"][0] <= local.hour < cfg["working_hours"][1]


class Campaign:
    def __init__(self, store, cfg, browser, now=utcnow, pause=time.sleep, stop_requested=None, enforce_schedule=False):
        self.store, self.cfg, self.browser = store, cfg, browser
        self.now, self.pause = now, pause
        self.stop_requested = stop_requested or (lambda: False)
        self.consecutive_failures = 0
        self.enforce_schedule = enforce_schedule

    def check_stop(self):
        if self.stop_requested():
            raise StopRequested("Stop requested. Progress is saved.")
        from outreach_monitor import progress
        progress(state_path(self.cfg), self.now()) if self.cfg.get('_progress_enabled') else None

    def preflight(self, lead, kind, mark_click=False):
        from outreach_state import meta
        if meta(self.store.db, 'restore_review_required') == '1':
            raise AccountBlocked('Restored state requires an explicit audit acknowledgment before sending')
        exclude = (lead['id'], kind)
        last = self.store.last_send(exclude)
        if last:
            remaining = self.cfg['minimum_seconds_between_sends'] - (self.now() - last).total_seconds()
            if remaining > 0:
                self.pause(remaining)
        self.check_stop()
        now = self.now()
        if self.enforce_schedule and not working_time(now, self.cfg):
            raise CyclePaused('Working window closed before a send; waiting for the next scheduled cycle')
        category = 'invite' if kind == 'invite' else 'message'
        daily = self.cfg['invitations_per_day'] if kind == 'invite' else self.cfg['messages_per_day']
        if self.store.used(category, day_start(now, self.cfg), exclude) >= daily:
            raise CyclePaused('Daily send allowance exhausted before click')
        if kind == 'invite':
            total = self.cfg['campaign_invitation_limit']
            if (self.store.used('invite', now - timedelta(days=7), exclude) >= self.cfg['invitations_per_7_days'] or
                    (total is not None and self.store.used('invite', exclude=exclude) >= total)):
                raise CyclePaused('Invitation allowance exhausted before click')
        if mark_click:
            self.store.click_started(lead['id'], kind, now)

    def failed(self, lead, detail, *, temporary=False):
        count = self.store.lead(lead['id'])['failure_count'] + 1
        values = dict(reason=detail, failure_count=count)
        if temporary and count < self.cfg['contact_retry_limit']:
            from outreach_worker import retry_delay
            values['next_check_at'] = stamp(self.now() + timedelta(seconds=retry_delay(self.cfg, count)))
            LOG.warning('DEFERRED #%s %s until %s: %s', lead['id'], lead['full_name'], values['next_check_at'], detail)
        else:
            values.update(state='needs_review', next_check_at=None)
            LOG.warning('HELD #%s %s; other contacts can continue: %s', lead['id'], lead['full_name'], detail)
        self.store.update(lead['id'], **values)
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.cfg['max_consecutive_failures']:
            raise RetryCycle('Consecutive contact failures; cooling down before a fresh browser session')

    def succeeded(self, lead):
        self.consecutive_failures = 0
        self.store.update(lead['id'], failure_count=0, reason='')

    def read(self, lead, operation):
        # Only read-only navigation/history is retried. Send methods never enter here.
        for attempt in range(self.cfg['read_attempts']):
            self.check_stop()
            try:
                return operation()
            except ContactNeedsReview as exc:
                self.failed(lead, str(exc))
                return None
            except (ContactNotReady, BrowserError, TimeoutError, ConnectionError) as exc:
                text = str(exc)
                if any(token in text.casefold() for token in ('browser has been closed', 'target page, context or browser has been closed',
                                                              'connection closed', 'net::err_internet_disconnected',
                                                              'net::err_name_not_resolved', 'net::err_network_changed',
                                                              'net::err_connection_reset', 'net::err_connection_refused',
                                                              'net::err_proxy_connection_failed', 'net::err_address_unreachable')):
                    raise RetryCycle(text) from exc
                if attempt + 1 < self.cfg['read_attempts']:
                    LOG.warning('Read retry #%s for %s: %s', attempt + 1, lead['full_name'], text)
                    self.pause(min(2 ** attempt, 5))
                else:
                    self.failed(lead, text, temporary=True)
        return None

    def send(self, lead, kind):
        self.preflight(lead, kind)
        message = render(lead, self.cfg)[kind]
        self.store.begin(lead['id'], kind, message, self.now())
        previous_guard = getattr(self.browser, 'before_action', None)
        self.browser.before_action = lambda: self.preflight(lead, kind, mark_click=True)
        try:
            if kind == 'invite':
                self.browser.send_invitation(lead, message)
            else:
                self.browser.send_message(lead, message, self.store.sent_messages(lead['id']))
            self.store.sent(lead['id'], kind, self.now())
        except InvitationHeld as exc:
            if kind != 'invite':
                self.store.uncertain(lead['id'], kind, str(exc), self.now())
            else:
                self.store.hold_invitation(lead['id'], str(exc), self.now())
            self.failed(lead, str(exc))
            return False
        except CyclePaused as exc:
            if not self.store.release_unstarted(lead['id'], kind):
                self.store.uncertain(lead['id'], kind, str(exc), self.now())
            raise
        except (AccountBlocked, StopRequested, KeyboardInterrupt, SystemExit) as exc:
            self.store.uncertain(lead['id'], kind, str(exc) or type(exc).__name__, self.now())
            raise
        except (ContactNeedsReview, ContactNotReady, StopRun, BrowserError, TimeoutError, ConnectionError) as exc:
            self.store.uncertain(lead['id'], kind, str(exc) or type(exc).__name__, self.now())
            self.failed(lead, 'Send outcome unknown; never automatically retry: ' + str(exc))
            return False
        except BaseException as exc:
            self.store.uncertain(lead['id'], kind, str(exc) or type(exc).__name__, self.now())
            raise
        finally:
            self.browser.before_action = previous_guard
        self.succeeded(lead)
        LOG.info('SENT %s to %s at %s', kind, lead['full_name'], lead['company'])
        return True

    def run(self):
        self.store.recover(self.now())
        messages = message_attempts = 0
        for lead in self.store.active_due(self.now(), self.cfg['acceptance_checks_per_run']):
            state = self.read(lead, lambda: self.browser.open_profile(lead))
            if state is None:
                continue
            next_check = stamp(self.now() + timedelta(minutes=self.cfg['check_interval_minutes']))
            self.store.update(lead['id'], last_checked_at=stamp(self.now()), next_check_at=next_check)
            if lead['state'] == 'invited' and state == 'pending':
                self.succeeded(lead)
                continue
            if state != 'connected':
                self.failed(lead, 'Tracked connection is no longer connected/pending')
                continue
            history = self.read(lead, lambda: self.browser.conversation(lead, self.store.sent_messages(lead['id'])))
            if history is None:
                continue
            if history.inbound:
                self.store.update(lead['id'], state='replied', reason='Recipient has sent a message; sequence stopped', failure_count=0)
                self.consecutive_failures = 0
                LOG.info('REPLIED: %s; follow-ups stopped', lead['full_name'])
                continue
            if history.unknown:
                self.failed(lead, 'Conversation sender/history could not be verified; no message sent')
                continue
            if lead['state'] == 'invited':
                self.store.update(lead['id'], state='accepted', accepted_at=stamp(self.now()))
                LOG.info('ACCEPTED: %s; message 1 due in %s day(s)', lead['full_name'], self.cfg['message_1_delay_days'])
                lead = self.store.lead(lead['id'])
            action = due_message(lead, self.cfg, self.now())
            if (action and message_attempts < self.cfg['messages_per_run'] and
                    self.store.used('message', day_start(self.now(), self.cfg)) < self.cfg['messages_per_day']):
                message_attempts += 1
                if self.send(lead, action):
                    messages += 1
            else:
                self.succeeded(lead)

        sent = checked = attempts = 0
        capacity = invite_capacity(self.store, self.cfg, self.now())
        for lead in self.store.candidates(self.cfg.get('company_filter', ''), self.now()):
            self.check_stop()
            if (attempts >= capacity or invite_capacity(self.store, self.cfg, self.now()) <= 0 or
                    checked >= self.cfg['acceptance_checks_per_run']):
                break
            checked += 1
            try:
                render(lead, self.cfg)
            except ValueError as exc:
                self.failed(lead, str(exc))
                continue
            state = self.read(lead, lambda: self.browser.open_profile(lead))
            if state is None:
                continue
            if state in ('pending', 'connected', 'unavailable'):
                self.store.update(lead['id'], state='skipped', reason='Already ' + state + '; not enrolled in this sequence', failure_count=0)
                self.consecutive_failures = 0
                LOG.info('SKIPPED %s: %s', lead['full_name'], state)
                continue
            if state != 'new':
                self.failed(lead, 'Profile state is ambiguous; no invitation sent')
                continue
            attempts += 1
            if self.send(lead, 'invite'):
                sent += 1
        return {'invitations_sent': sent, 'messages_sent': messages, 'new_profiles_checked': checked}


class ProcessLock:
    """OS-owned lock: released even after a crash; protects browser and SQLite state."""
    def __init__(self, path=None):
        self.path = Path(path or Path.cwd() / ".local" / "process.lock")

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0)
        self.file.write(b"0")
        self.file.flush()
        self.file.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            self.file.close()
            raise StopRun("Another outreach process is running. Close it first.") from None
        return self

    def __exit__(self, *args):
        self.file.close()


def profile_directory(channel="chromium", workspace=None, profile_path=None):
    workspace = Path(workspace or Path.cwd()).resolve()
    if profile_path:
        return resolve_path(profile_path, workspace)
    identity = hashlib.sha256(str(workspace).casefold().encode()).hexdigest()[:12]
    name = "linkedin-outreach-" + identity + "-" + channel
    legacy = Path(tempfile.gettempdir()) / name
    if legacy.is_dir():
        return legacy  # Keep existing signed-in sessions working after upgrade.
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / "LinkedInOutreach" / "profiles" / name


def campaign_finished(store, cfg):
    active = store.db.execute("SELECT count(*) FROM leads WHERE state IN ('invited','accepted','message_1_sent')").fetchone()[0]
    cap = cfg["campaign_invitation_limit"]
    cap_reached = cap is not None and store.used("invite") >= cap
    return not active and (cap_reached or not store.candidates(cfg.get("company_filter", ""), include_deferred=True))


def wait_for_stop(path, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    return path.exists()


def show_status(store):
    for row in store.db.execute("SELECT state,count(*) n FROM leads GROUP BY state ORDER BY state"):
        print(f"{row['state']:20} {row['n']:6}")
    tracked = store.db.execute("SELECT * FROM leads WHERE state NOT IN ('new','skipped') ORDER BY id LIMIT 30").fetchall()
    for row in tracked:
        print(f"#{row['id']} {row['full_name']} | {row['company']} | {row['state']} | {row['reason']}")


def browser_session(cfg, *, visible=False):
    from linkedin_browser import LinkedInBrowser
    executable = cfg["browser_executable"]
    if executable:
        executable = str(resolve_path(executable, Path(cfg["_workspace"])))
    return LinkedInBrowser(profile_directory(cfg["browser_channel"], cfg["_workspace"], cfg["browser_profile_directory"]),
                           cfg["browser_channel"], headless=False if visible else cfg["headless"], executable_path=executable)


def browser_cycle(store, cfg, stop_path):
    from outreach_state import meta
    if meta(store.db, 'restore_review_required') == '1':
        raise AccountBlocked('Restored state requires independent audit acknowledgment before sending')
    with browser_session(cfg) as browser:
        browser.require_login()
        return Campaign(store, cfg, browser, pause=lambda seconds: wait_for_stop(stop_path, seconds),
                        stop_requested=stop_path.exists, enforce_schedule=True).run()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--settings", type=Path, default=Path.cwd() / "settings.json")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create local settings with a guided setup or command-line flags")
    init.add_argument("--csv", type=Path)
    init.add_argument("--messages", type=Path)
    init.add_argument("--browser", choices=("chromium", "chrome", "msedge", "brave"))
    init.add_argument("--timezone", default="America/New_York")
    init.add_argument('--state-directory', default='auto')
    sub.add_parser("doctor", help="Validate CSV, templates, paths and browser installation offline")
    sub.add_parser("install-browser", help="Install bundled Chromium if selected; keep branded browsers unchanged")
    start = sub.add_parser("start", help="Start a detached background watcher")
    start.add_argument("--live", action="store_true", help="Required to allow invitations and follow-ups")
    preview = sub.add_parser("preview", help="Offline preview; never opens LinkedIn")
    preview.add_argument("--limit", type=int, default=3)
    sub.add_parser("status")
    sub.add_parser("clean-csv", help="Clean the source CSV and sync repairs, without opening LinkedIn")
    sub.add_parser("stop-watch", help="Stop the watcher after its current action; never force-kill a send")
    sub.add_parser('stop-supervisor', help='Pause outreach and stop its process supervisor gracefully')
    sub.add_parser("login")
    inspect = sub.add_parser("inspect", help="Check a profile in browser; do not connect or send")
    inspect.add_argument("--lead-id", type=int, help="Inspect a specific tracked lead without changing its state")
    for name in ("run", "watch"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--live", action="store_true", help="Allow invitations and messages")
    reconcile = sub.add_parser("reconcile", help="Resolve an uncertain send after checking LinkedIn manually")
    reconcile.add_argument("lead_id", type=int)
    reconcile.add_argument("outcome", choices=("sent", "not-sent", "stop"))
    stop = sub.add_parser("stop", help="Stop the sequence for a lead")
    stop.add_argument("lead_id", type=int)
    sub.add_parser('backup', help='Create and validate a consistent SQLite snapshot')
    restore = sub.add_parser('restore', help='Restore a backup with a mandatory audit latch')
    restore.add_argument('backup_file', type=Path)
    acknowledge = sub.add_parser('acknowledge-restore', help='Confirm independent review of restored send history')
    acknowledge.add_argument('--confirm-audited', action='store_true')
    resume = sub.add_parser('resume', help='Resume a held read-only check; refuses possible/cancelled sends')
    resume.add_argument('lead_id', type=int)
    report = sub.add_parser('report', help='Write a private daily report')
    report.add_argument('--date')
    sub.add_parser('health')
    sub.add_parser('test-notification')
    for name in ('supervise', 'install-startup'):
        item = sub.add_parser(name)
        item.add_argument('--live', action='store_true')
    sub.add_parser('uninstall-startup')
    move = sub.add_parser('migrate-state', help='Copy campaign state to a new unsynced directory and update settings')
    move.add_argument('--destination', type=Path)
    args = parser.parse_args(argv)
    from outreach_setup import doctor, initialize, install_browser, prepare_source, start_worker, worker_running
    store = None
    watch_started = False
    directory = None
    log_handlers = []
    try:
        if args.command == "init":
            return initialize(args)
        if args.command in ('stop-watch', 'stop-supervisor'):
            # Stopping must still work if a template file was moved or edited
            # incorrectly while the running worker uses its cached settings.
            settings_path = args.settings.expanduser().resolve()
            raw = read_json(settings_path) if settings_path.is_file() else {}
            directory = state_path({'state_directory': raw.get('state_directory', '.local'), '_workspace': str(settings_path.parent)})
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "stop-watch.request").write_text(stamp(utcnow()), encoding="utf-8")
            from outreach_service import control
            control(directory, 'paused')
            if args.command == 'stop-supervisor':
                (directory / 'stop-supervisor.request').write_text(stamp(utcnow()))
            print("Stop requested. A current browser action may finish first. Saved progress is kept.")
            return 0
        cfg = load_settings(args.settings)
        directory = state_path(cfg)
        directory.mkdir(parents=True, exist_ok=True)
        log_handlers = [logging.StreamHandler(), RotatingFileHandler(directory / "outreach.log", encoding="utf-8", maxBytes=2_000_000, backupCount=3)]
        LOG.setLevel(logging.INFO)
        LOG.propagate = False
        for handler in log_handlers:
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            LOG.addHandler(handler)
        stop_path = directory / "stop-watch.request"
        if args.command in ('health', 'report', 'test-notification'):
            from outreach_monitor import daily_report, desktop_notification, read_status
            if args.command == 'test-notification':
                print('Notification requested:', desktop_notification('LinkedIn Outreach', 'Desktop alerts are configured. This is a setup check.'))
            elif args.command == 'report':
                from datetime import date
                print(json.dumps(daily_report(directory, cfg, date.fromisoformat(args.date) if args.date else None), indent=2))
            else:
                details = {name: read_status(directory / (name + '.json')) for name in
                           ('worker-status', 'heartbeat', 'progress', 'backup-status', 'supervisor-status', 'control')}
                details.update(state_directory=str(directory), worker_running=worker_running(directory),
                               supervisor_running=worker_running(directory, lock_name='supervisor.lock'))
                print(json.dumps(details, indent=2))
            return 0
        if args.command in ('supervise', 'install-startup', 'uninstall-startup'):
            from outreach_service import control, install_startup, start_supervisor, uninstall_startup
            if args.command == 'uninstall-startup':
                uninstall_startup(cfg)
                (directory / 'stop-supervisor.request').write_text(stamp(utcnow()))
                print('Login startup removed; current worker can be stopped with stop-watch.')
            else:
                if not args.live:
                    raise ValueError('Supervision/startup requires --live')
                if args.command == 'install-startup':
                    print('Installed current-user logon task:', install_startup(cfg))
                control(directory, 'running')
                start_supervisor(cfg)
                print('Process supervision enabled; account blocks and explicit pauses remain latched.')
            return 0
        if args.command == 'backup':
            from outreach_state import backup_database
            print(backup_database(directory / 'outreach.sqlite3'))
            return 0
        if args.command == 'migrate-state':
            from outreach_state import migrate_directory
            print('New campaign state:', migrate_directory(cfg, args.destination))
            return 0
        if args.command == 'restore':
            from outreach_state import restore_database
            from outreach_service import control
            with ProcessLock(directory / 'process.lock'):
                control(directory, 'paused')
                restore_database(args.backup_file, directory / 'outreach.sqlite3')
            print('Restored. Sending is latched off until acknowledge-restore --confirm-audited after independent review.')
            return 0
        if args.command == "status":
            print("Worker:", "running" if worker_running(directory) else "stopped")
            print('State directory:', directory)
            print('Process supervisor:', 'running' if worker_running(directory, lock_name='supervisor.lock') else 'stopped')
            health = directory / 'worker-status.json'
            if health.is_file():
                try:
                    info = read_json(health)
                    print('Worker phase:', info.get('phase', 'unknown'))
                    if info.get('last_error'):
                        print('Last error:', info['last_error'])
                    if info.get('next_retry_at') and worker_running(directory):
                        print('Next check/retry:', parsed(info['next_retry_at']).astimezone(ZoneInfo(cfg['timezone'])).isoformat(timespec='seconds'))
                except (ValueError, OSError):
                    print('Worker health details unavailable; see the private log')
            if not (directory / "outreach.sqlite3").is_file():
                print("No campaign imported yet. Run preview first.")
                return 0
            store = Store(directory / "outreach.sqlite3", readonly=True)
            show_status(store)
            print('Held uncertain sends:', store.unresolved(), '(these contacts are never retried automatically)')
            print("Invitation capacity this run:", invite_capacity(store, cfg, utcnow()))
            return 0
        if args.command == "doctor":
            return doctor(cfg)
        if args.command == "install-browser":
            return install_browser(cfg)
        if args.command == "start":
            if not args.live:
                raise ValueError("Starting automation requires --live. Use preview to review messages first.")
            return start_worker(cfg)
        with ProcessLock(directory / "process.lock"):
            if args.command == "login":
                with browser_session(cfg, visible=True) as browser:
                    browser.login()
                print("Sign-in saved; the visible browser closed normally. Routine runs use headless mode when enabled.")
                return 0
            store = Store(directory / "outreach.sqlite3")
            # State-only commands also work when the source CSV has been moved.
            if args.command in ("stop", "reconcile", 'resume', 'acknowledge-restore'):
                cleanup = None
            else:
                cleaned, cleanup = prepare_source(cfg, directory)
                if cleanup["changes"] or cleanup["duplicates_removed"]:
                    LOG.info("CSV normalized: %s field repairs, %s duplicates removed; original unchanged",
                             len(cleanup["changes"]), cleanup["duplicates_removed"])
                imported = store.import_csv(cleaned)
                LOG.info("CSV: %s", imported)
            store.recover()
            if args.command == 'resume':
                from outreach_state import resume_contact
                resume_contact(store, args.lead_id)
                print('Held contact requeued for a fresh identity/history check.')
            elif args.command == 'acknowledge-restore':
                from outreach_state import meta
                if not args.confirm_audited:
                    raise ValueError('Review send history independently, then pass --confirm-audited')
                meta(store.db, 'restore_review_required', '0')
                print('Restore audit acknowledged. Use start --live to resume explicitly.')
            elif args.command == "clean-csv":
                print(json.dumps({"rows": cleanup["output_rows"], "field_repairs": len(cleanup["changes"]),
                                  "duplicates_removed": cleanup["duplicates_removed"], "cleaned_file": cleanup["cleaned_file"], **imported}, indent=2))
                show_status(store)
            elif args.command == "stop":
                if store.lead(args.lead_id) is None:
                    raise ValueError("Unknown lead ID")
                if store.db.execute("SELECT 1 FROM actions WHERE lead_id=? AND status IN ('prepared','unknown')", (args.lead_id,)).fetchone():
                    raise StopRun("Resolve this uncertain send with reconcile LEAD_ID stop first")
                store.update(args.lead_id, state="stopped", reason="Stopped by operator")
            elif args.command == "reconcile":
                store.reconcile(args.lead_id, args.outcome, utcnow())
                print("Reconciled. Run status to review.")
            elif args.command == "preview" or (args.command in ("run", "watch") and not args.live):
                print("PREVIEW ONLY. No browser activity or messages.\n")
                print("Invitation capacity this run:", invite_capacity(store, cfg, utcnow()))
                for lead in store.candidates(cfg.get("company_filter", ""))[:getattr(args, "limit", 3)]:
                    print(f"\n#{lead['id']} {lead['full_name']} | {lead['title']} | {lead['company']}\n{lead['url']}")
                    try:
                        messages = render(lead, cfg)
                    except ValueError as exc:
                        print(f"Needs review: {exc}")
                        continue
                    for kind, text in messages.items():
                        print(f"\n{kind} ({len(text)} characters):\n{text}")
            else:
                if args.command == "watch":
                    from outreach_worker import watch_worker
                    stop_path.unlink(missing_ok=True)
                    LOG.info("Watching weekdays/hours %s/%s, timezone %s; headless=%s. stop-watch requests shutdown.", cfg["working_days"], cfg["working_hours"], cfg["timezone"], cfg["headless"])
                    (directory / "watcher-ready.json").write_text(json.dumps({"pid": os.getpid(), "started_at": stamp(utcnow())}), encoding="utf-8")
                    watch_started = True
                    from outreach_monitor import Monitor
                    from outreach_worker import worker_status
                    from outreach_monitor import progress
                    cfg['_progress_enabled'] = True
                    worker_status(directory, 'starting', utcnow())
                    progress(directory, utcnow())
                    with Monitor(cfg, directory):
                        watch_worker(cfg, directory, run_cycle=lambda: browser_cycle(store, cfg, stop_path),
                                 has_work=lambda: bool(store.active_due(utcnow(), 1)) or (
                                     invite_capacity(store, cfg, utcnow()) > 0 and bool(store.candidates(cfg.get('company_filter', '')))),
                                 is_finished=lambda: campaign_finished(store, cfg), next_retry=store.next_retry,
                                 now=utcnow, wait=lambda seconds: wait_for_stop(stop_path, seconds),
                                 stop_requested=stop_path.exists)
                    return 0
                with browser_session(cfg, visible=args.command == 'inspect') as browser:
                    browser.require_login()
                    if args.command == 'inspect':
                        if args.lead_id is not None:
                            selected = store.lead(args.lead_id)
                            if selected is None:
                                raise ValueError('Unknown lead ID')
                            candidates = [selected]
                        else:
                            candidates = store.candidates(cfg.get('company_filter', ''))
                        if not candidates:
                            print('No eligible profiles')
                            return 0
                        lead = candidates[0]
                        try:
                            state = browser.open_profile(lead)
                        except Exception:
                            browser.profile_diagnostic(lead, directory / 'profile-diagnostic.json')
                            raise
                        print(json.dumps({'lead_id': lead['id'], 'name': lead['full_name'], 'company': lead['company'], 'profile_state': state}, indent=2))
                        browser.profile_diagnostic(lead, directory / 'profile-diagnostic.json')
                        browser.screenshot(directory / 'profile-inspection.png')
                    else:
                        print(json.dumps(Campaign(store, cfg, browser).run()), flush=True)
        return 0
    except KeyboardInterrupt:
        print("Stopped. Progress is saved.")
        return 130
    except StopRequested as exc:
        LOG.info("%s", exc)
        return 0
    except CyclePaused as exc:
        LOG.info('%s', exc)
        return 0
    except Exception as exc:
        if log_handlers:
            # Keep traceback details in the private log; console errors stay readable.
            LOG.handlers.remove(log_handlers[0])
            print(f"STOPPED: {exc}", file=sys.stderr)
            LOG.error("STOPPED: %s", exc, exc_info=True)
        else:
            print(f"STOPPED: {exc}", file=sys.stderr)
        return 1
    finally:
        if store:
            store.close()
        if watch_started:
            (directory / "watcher-ready.json").unlink(missing_ok=True)
        for handler in log_handlers:
            if handler in LOG.handlers:
                LOG.removeHandler(handler)
            handler.close()


if __name__ == "__main__":
    raise SystemExit(main())
