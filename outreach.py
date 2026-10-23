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

from outreach_config import VERSION, load_settings, read_json, resolve_path, state_path
from outreach_errors import InvitationHeld, ProfileNeedsReview, StopRequested, StopRun

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
        target = Path(path).resolve().as_uri() + "?mode=ro" if readonly else str(path)
        self.db = sqlite3.connect(target, timeout=10, uri=readonly)
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

    def close(self):
        self.db.close()

    def lead(self, lead_id):
        return self.db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()

    def update(self, lead_id, **values):
        allowed = {"state", "reason", "invited_at", "accepted_at", "message_1_at",
                   "message_2_at", "last_checked_at", "next_check_at"}
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

    def candidates(self, company=""):
        sql, args = "SELECT * FROM leads WHERE state='new'", []
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
            self.db.execute("""
                INSERT INTO actions (lead_id,kind,status,started_at,message)
                VALUES (?,?,'prepared',?,?)
            """, (lead_id, kind, stamp(now), message))

    def sent(self, lead_id, kind, now):
        state, column = {
            "invite": ("invited", "invited_at"),
            "message_1": ("message_1_sent", "message_1_at"),
            "message_2": ("completed", "message_2_at"),
        }[kind]
        with self.db:
            cursor = self.db.execute("""
                UPDATE actions SET status='sent',sent_at=? WHERE lead_id=? AND kind=?
                AND status IN ('prepared','unknown')
            """, (stamp(now), lead_id, kind))
            if cursor.rowcount != 1:
                raise ValueError("No pending action to confirm")
            self.db.execute(
                f"UPDATE leads SET state=?,{column}=?,reason='',next_check_at=NULL WHERE id=?",
                (state, stamp(now), lead_id),
            )

    def uncertain(self, lead_id, kind, detail):
        with self.db:
            self.db.execute("""
                UPDATE actions SET status='unknown',detail=?
                WHERE lead_id=? AND kind=? AND status='prepared'
            """, (detail, lead_id, kind))
            self.db.execute("UPDATE leads SET state='needs_review',reason=? WHERE id=?",
                            (detail, lead_id))

    def hold_invitation(self, lead_id, detail):
        # Keep the attempt in the journal and quota, without requeueing it.
        # Only a freshly verified profile still offering Connect qualifies.
        with self.db:
            cursor = self.db.execute("""
                UPDATE actions SET status='cancelled',detail=?
                WHERE lead_id=? AND kind='invite' AND status='prepared'
            """, (detail, lead_id))
            if cursor.rowcount != 1:
                raise ValueError("No prepared invitation to hold")
            self.db.execute("UPDATE leads SET state='needs_review',reason=? WHERE id=?",
                            (detail, lead_id))

    def recover(self):
        rows = self.db.execute("SELECT * FROM actions WHERE status='prepared'").fetchall()
        for row in rows:
            self.uncertain(row["lead_id"], row["kind"],
                           "Previous run stopped during a send. Check LinkedIn before reconciling.")
        return len(rows)

    def unresolved(self):
        return self.db.execute("SELECT count(*) FROM actions WHERE status IN ('prepared','unknown')").fetchone()[0]

    def used(self, kind, since=None):
        sql = "SELECT count(*) FROM actions WHERE status IN ('prepared','unknown','sent','cancelled')"
        args = []
        sql += " AND " + ("kind='invite'" if kind == "invite" else "kind IN ('message_1','message_2')")
        if since:
            sql += " AND started_at>=?"
            args.append(stamp(since))
        return self.db.execute(sql, args).fetchone()[0]

    def last_send(self):
        value = self.db.execute("SELECT max(started_at) FROM actions WHERE status IN ('prepared','unknown','sent','cancelled')").fetchone()[0]
        return parsed(value)

    def sent_messages(self, lead_id):
        return {row["kind"]: row["message"] for row in self.db.execute(
            "SELECT kind,message FROM actions WHERE lead_id=? AND status='sent'", (lead_id,)
        )}

    def reconcile(self, lead_id, outcome, now):
        row = self.db.execute("""
            SELECT * FROM actions WHERE lead_id=? AND status='unknown'
            ORDER BY id DESC LIMIT 1
        """, (lead_id,)).fetchone()
        if row is None:
            raise ValueError("This lead has no uncertain send to reconcile")
        if outcome == "sent":
            self.sent(lead_id, row["kind"], parsed(row["started_at"]))
        elif outcome == "not-sent":
            # Explicit operator reconciliation is the only path that permits a retry.
            state = {"invite": "new", "message_1": "accepted", "message_2": "message_1_sent"}[row["kind"]]
            with self.db:
                self.db.execute("DELETE FROM actions WHERE id=?", (row["id"],))
                self.db.execute("UPDATE leads SET state=?,reason='',next_check_at=NULL WHERE id=?", (state, lead_id))
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
    def __init__(self, store, cfg, browser, now=utcnow, pause=time.sleep, stop_requested=None):
        self.store, self.cfg, self.browser = store, cfg, browser
        self.now, self.pause = now, pause
        self.stop_requested = stop_requested or (lambda: False)

    def check_stop(self):
        if self.stop_requested():
            raise StopRequested("Stop requested. Progress is saved.")

    def send(self, lead, kind):
        self.check_stop()
        last = self.store.last_send()
        if last:
            remaining = self.cfg["minimum_seconds_between_sends"] - (self.now() - last).total_seconds()
            if remaining > 0:
                self.pause(remaining)
        self.check_stop()
        message = render(lead, self.cfg)[kind]
        self.store.begin(lead["id"], kind, message, self.now())
        try:
            if kind == "invite":
                self.browser.send_invitation(lead, message)
            else:
                self.browser.send_message(lead, message, self.store.sent_messages(lead["id"]))
            self.store.sent(lead["id"], kind, self.now())
        except InvitationHeld as exc:
            if kind != "invite":
                self.store.uncertain(lead["id"], kind, str(exc))
                raise StopRun("Unexpected invitation error during a message; check the send outcome.") from exc
            self.store.hold_invitation(lead["id"], str(exc))
            LOG.warning("HELD %s; invitation attempt counts toward limits, no automatic retry: %s", lead["full_name"], exc)
            return False
        except BaseException as exc:
            self.store.uncertain(lead["id"], kind, str(exc) or type(exc).__name__)
            raise StopRun("Send outcome needs review; nothing will be retried automatically.") from exc
        LOG.info("SENT %s to %s at %s", kind, lead["full_name"], lead["company"])
        return True

    def run(self):
        self.store.recover()
        if self.store.unresolved():
            raise StopRun("There is an uncertain send. Use status, check LinkedIn, then reconcile it.")
        messages = 0
        for lead in self.store.active_due(self.now(), self.cfg["acceptance_checks_per_run"]):
            self.check_stop()
            try:
                state = self.browser.open_profile(lead)
            except ProfileNeedsReview as exc:
                self.store.update(lead["id"], state="needs_review", reason=str(exc))
                LOG.warning("HELD %s for identity review; other profiles can continue: %s", lead["full_name"], exc)
                continue
            next_check = stamp(self.now() + timedelta(minutes=self.cfg["check_interval_minutes"]))
            self.store.update(lead["id"], last_checked_at=stamp(self.now()), next_check_at=next_check)
            if lead["state"] == "invited" and state == "pending":
                continue
            if state != "connected":
                self.store.update(lead["id"], state="needs_review", reason="Tracked connection is no longer connected/pending")
                continue
            history = self.browser.conversation(lead, self.store.sent_messages(lead["id"]))
            if history.inbound:
                self.store.update(lead["id"], state="replied", reason="Recipient has sent a message; sequence stopped")
                LOG.info("REPLIED: %s; follow-ups stopped", lead["full_name"])
                continue
            if history.unknown:
                raise StopRun("Conversation sender/history could not be verified. No message sent.")
            if lead["state"] == "invited":
                self.store.update(lead["id"], state="accepted", accepted_at=stamp(self.now()))
                LOG.info("ACCEPTED: %s; message 1 due in %s day(s)", lead["full_name"], self.cfg["message_1_delay_days"])
                lead = self.store.lead(lead["id"])
            action = due_message(lead, self.cfg, self.now())
            if action and messages < self.cfg["messages_per_run"]:
                if self.store.used("message", day_start(self.now(), self.cfg)) < self.cfg["messages_per_day"]:
                    self.send(lead, action)
                    messages += 1

        sent = checked = 0
        capacity = invite_capacity(self.store, self.cfg, self.now())
        for lead in self.store.candidates(self.cfg.get("company_filter", "")):
            self.check_stop()
            if sent >= capacity or checked >= self.cfg["acceptance_checks_per_run"]:
                break
            checked += 1
            try:
                render(lead, self.cfg)
            except ValueError as exc:
                self.store.update(lead["id"], state="needs_review", reason=str(exc))
                continue
            try:
                state = self.browser.open_profile(lead)
            except ProfileNeedsReview as exc:
                self.store.update(lead["id"], state="needs_review", reason=str(exc))
                LOG.warning("HELD %s for identity review; no invitation sent: %s", lead["full_name"], exc)
                continue
            if state in ("pending", "connected", "unavailable"):
                self.store.update(lead["id"], state="skipped", reason="Already " + state + "; not enrolled in this sequence")
                LOG.info("SKIPPED %s: %s", lead["full_name"], state)
                continue
            if state != "new":
                raise StopRun("Profile state is ambiguous; no invitation sent")
            if self.send(lead, "invite"):
                sent += 1
            else:
                # The held attempt uses one slot even though no send was confirmed.
                capacity -= 1
        return {"invitations_sent": sent, "messages_sent": messages, "new_profiles_checked": checked}


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
    return not active and (cap_reached or not store.candidates(cfg.get("company_filter", "")))


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
    sub.add_parser("doctor", help="Validate CSV, templates, paths and browser installation offline")
    sub.add_parser("install-browser", help="Install bundled Chromium if selected; keep branded browsers unchanged")
    start = sub.add_parser("start", help="Start a detached background watcher")
    start.add_argument("--live", action="store_true", help="Required to allow invitations and follow-ups")
    preview = sub.add_parser("preview", help="Offline preview; never opens LinkedIn")
    preview.add_argument("--limit", type=int, default=3)
    sub.add_parser("status")
    sub.add_parser("clean-csv", help="Clean the source CSV and sync repairs, without opening LinkedIn")
    sub.add_parser("stop-watch", help="Stop the watcher after its current action; never force-kill a send")
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
    args = parser.parse_args(argv)
    from outreach_setup import doctor, initialize, install_browser, prepare_source, start_worker, worker_running
    store = None
    watch_started = False
    directory = None
    log_handlers = []
    try:
        if args.command == "init":
            return initialize(args)
        if args.command == "stop-watch":
            # Stopping must still work if a template file was moved or edited
            # incorrectly while the running worker uses its cached settings.
            settings_path = args.settings.expanduser().resolve()
            raw = read_json(settings_path) if settings_path.is_file() else {}
            directory = resolve_path(raw.get("state_directory", ".local"), settings_path.parent)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "stop-watch.request").write_text(stamp(utcnow()), encoding="utf-8")
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
        if args.command == "status":
            print("Worker:", "running" if worker_running(directory) else "stopped")
            if not (directory / "outreach.sqlite3").is_file():
                print("No campaign imported yet. Run preview first.")
                return 0
            store = Store(directory / "outreach.sqlite3", readonly=True)
            show_status(store)
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
            if args.command in ("stop", "reconcile"):
                cleanup = None
            else:
                cleaned, cleanup = prepare_source(cfg, directory)
                if cleanup["changes"] or cleanup["duplicates_removed"]:
                    LOG.info("CSV normalized: %s field repairs, %s duplicates removed; original unchanged",
                             len(cleanup["changes"]), cleanup["duplicates_removed"])
                imported = store.import_csv(cleaned)
                LOG.info("CSV: %s", imported)
            store.recover()
            if args.command == "clean-csv":
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
                    if store.unresolved():
                        raise StopRun("An uncertain send needs reconciliation before starting the watcher")
                    stop_path.unlink(missing_ok=True)
                    LOG.info("Watching weekdays/hours %s/%s, timezone %s; headless=%s. stop-watch requests shutdown.", cfg["working_days"], cfg["working_hours"], cfg["timezone"], cfg["headless"])
                    (directory / "watcher-ready.json").write_text(json.dumps({"pid": os.getpid(), "started_at": stamp(utcnow())}), encoding="utf-8")
                    watch_started = True
                while True:
                    if args.command == "watch" and (stop_path.exists() or campaign_finished(store, cfg)):
                        LOG.info("Watcher stopped; campaign finished or stop requested. Progress is saved.")
                        break
                    needs_work = bool(store.active_due(utcnow(), 1)) or invite_capacity(store, cfg, utcnow()) > 0
                    if args.command != "watch" or (working_time(utcnow(), cfg) and needs_work):
                        with browser_session(cfg, visible=args.command == "inspect") as browser:
                            browser.require_login()
                            if args.command == "inspect":
                                if args.lead_id is not None:
                                    selected = store.lead(args.lead_id)
                                    if selected is None:
                                        raise ValueError("Unknown lead ID")
                                    candidates = [selected]
                                else:
                                    candidates = store.candidates(cfg.get("company_filter", ""))
                                if not candidates:
                                    print("No eligible profiles")
                                    break
                                lead = candidates[0]
                                try:
                                    state = browser.open_profile(lead)
                                except Exception:
                                    browser.profile_diagnostic(lead, directory / "profile-diagnostic.json")
                                    raise
                                print(json.dumps({"lead_id": lead["id"], "name": lead["full_name"], "company": lead["company"], "profile_state": state}, indent=2))
                                browser.profile_diagnostic(lead, directory / "profile-diagnostic.json")
                                browser.screenshot(directory / "profile-inspection.png")
                            else:
                                print(json.dumps(Campaign(store, cfg, browser,
                                                          pause=lambda seconds: wait_for_stop(stop_path, seconds) if args.command == "watch" else time.sleep(seconds),
                                                          stop_requested=stop_path.exists if args.command == "watch" else None).run()), flush=True)
                    if args.command != "watch":
                        break
                    if wait_for_stop(stop_path, cfg["watch_interval_minutes"] * 60):
                        LOG.info("Watcher stopped at operator request. Progress is saved.")
                        break
        return 0
    except KeyboardInterrupt:
        print("Stopped. Progress is saved.")
        return 130
    except StopRequested as exc:
        LOG.info("%s", exc)
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
