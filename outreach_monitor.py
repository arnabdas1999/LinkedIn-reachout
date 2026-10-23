"""Local heartbeat, private daily reports and deduplicated desktop alerts."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from zoneinfo import ZoneInfo

LOG = logging.getLogger('outreach')


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    pending.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
    try:
        for attempt in range(5):
            try:
                pending.replace(path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05)
    finally:
        pending.unlink(missing_ok=True)


def read_status(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def progress(directory, now):
    atomic_json(directory / 'progress.json', {'pid': os.getpid(), 'updated_at': now.isoformat()})


def desktop_notification(title, message):
    if sys.platform != 'win32':
        return False
    code = '''Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$n = New-Object System.Windows.Forms.NotifyIcon
try {
  $n.Icon = [System.Drawing.SystemIcons]::Warning
  $n.Visible = $true
  $n.ShowBalloonTip(5000, $env:OUTREACH_ALERT_TITLE, $env:OUTREACH_ALERT_MESSAGE, [System.Windows.Forms.ToolTipIcon]::Warning)
  $until = [DateTime]::UtcNow.AddSeconds(8)
  while ([DateTime]::UtcNow -lt $until) { [System.Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 100 }
} finally { $n.Dispose() }
'''
    env = dict(os.environ, OUTREACH_ALERT_TITLE=title[:60], OUTREACH_ALERT_MESSAGE=message[:250])
    subprocess.Popen(['powershell.exe', '-NoProfile', '-NonInteractive', '-STA', '-WindowStyle', 'Hidden', '-Command', code],
                     env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=subprocess.CREATE_NO_WINDOW)
    return True


def alert(directory, cfg, kind, message, now):
    from outreach import ProcessLock
    from outreach_errors import StopRun
    try:
        with ProcessLock(directory / 'alerts.lock'):
            return _alert(directory, cfg, kind, message, now)
    except StopRun:
        return False


def _alert(directory, cfg, kind, message, now):
    history = read_status(directory / 'alert-state.json')
    key = hashlib.sha256((kind + message).encode('utf-8')).hexdigest()
    previous = history.get(key)
    if previous and (now - datetime.fromisoformat(previous)).total_seconds() < cfg['notification_cooldown_minutes'] * 60:
        return False
    history[key] = now.isoformat()
    atomic_json(directory / 'alert-state.json', history)
    with (directory / 'alerts.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'at': now.isoformat(), 'kind': kind, 'message': message}) + '\n')
    LOG.warning('ATTENTION %s: %s', kind, message)
    if cfg['desktop_notifications']:
        try:
            desktop_notification('LinkedIn Outreach: ' + kind, message)
        except OSError as exc:
            LOG.warning('Desktop notification unavailable; alert retained locally: %s', exc)
    return True


def daily_report(directory, cfg, day=None, now=None):
    now = now or datetime.now(timezone.utc)
    zone = ZoneInfo(cfg['timezone'])
    day = day or now.astimezone(zone).date()
    start = datetime.combine(day, datetime.min.time(), zone).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), zone).astimezone(timezone.utc)
    path = directory / 'outreach.sqlite3'
    if not path.exists():
        return None
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        columns = {row['name'] for row in db.execute('pragma table_info(actions)')}
        timing = 'coalesce(quota_at,started_at)' if 'quota_at' in columns else 'started_at'
        rows = db.execute(f'SELECT kind,status,count(*) n FROM actions WHERE {timing}>=? AND {timing}<? GROUP BY kind,status',
                          (start.isoformat(timespec='seconds'), end.isoformat(timespec='seconds'))).fetchall()
        confirmed = {kind: 0 for kind in ('invite', 'message_1', 'message_2')}
        reserved = {kind: 0 for kind in confirmed}
        for row in rows:
            if row['status'] == 'sent':
                confirmed[row['kind']] += row['n']
            elif row['status'] in ('prepared', 'unknown', 'cancelled'):
                reserved[row['kind']] += row['n']
        states = dict(db.execute('SELECT state,count(*) FROM leads GROUP BY state').fetchall())
        reasons = [dict(row) for row in db.execute("SELECT reason,count(*) n FROM leads WHERE state='needs_review' GROUP BY reason")]
        review = {'enabled': cfg.get('automatic_review', False), 'pending': 0, 'parked': 0, 'events': {}}
        if 'review_exhausted' in {row['name'] for row in db.execute('PRAGMA table_info(leads)')}:
            review['pending'] = db.execute("SELECT count(*) FROM leads WHERE state='needs_review' AND review_exhausted=0").fetchone()[0]
            review['parked'] = db.execute("SELECT count(*) FROM leads WHERE state='needs_review' AND review_exhausted=1").fetchone()[0]
            review['events'] = dict(db.execute('SELECT outcome,count(*) FROM review_events WHERE at>=? AND at<? GROUP BY outcome',
                (start.isoformat(timespec='seconds'), end.isoformat(timespec='seconds'))).fetchall())
        used_week = db.execute(f"SELECT count(*) FROM actions WHERE kind='invite' AND status IN ('prepared','unknown','sent','cancelled') AND {timing}>=?", ((now - timedelta(days=7)).isoformat(timespec='seconds'),)).fetchone()[0]
        used_total = db.execute("SELECT count(*) FROM actions WHERE kind='invite' AND status IN ('prepared','unknown','sent','cancelled')").fetchone()[0]
    health = read_status(directory / 'worker-status.json')
    constraints = []
    if day.weekday() not in cfg['working_days']:
        constraints.append('Not a configured working day')
    if reserved['invite']:
        constraints.append('Uncertain/cancelled attempts reserve part of the invitation allowance')
    if confirmed['invite'] + reserved['invite'] >= cfg['invitations_per_day']:
        constraints.append('Daily invitation allowance consumed')
    if used_week >= cfg['invitations_per_7_days']:
        constraints.append('Rolling seven-day allowance consumed at snapshot time')
    if cfg['campaign_invitation_limit'] is not None and used_total >= cfg['campaign_invitation_limit']:
        constraints.append('Total campaign allowance consumed at snapshot time')
    if not states.get('new'):
        constraints.append('No new contacts remain eligible')
    if health.get('phase') in ('blocked', 'failed', 'backoff', 'stopped'):
        constraints.append('Worker phase at snapshot: ' + health['phase'])
    if now.astimezone(zone).date() == day and now.astimezone(zone).hour < cfg['working_hours'][1]:
        constraints.append('Working window has not finished')
    if confirmed['invite'] < cfg['invitations_per_day'] and not constraints:
        constraints.append('Shortfall requires review of worker/alert history; snapshot alone cannot establish its cause')
    data = dict(date=str(day), timezone=cfg['timezone'], generated_at=now.isoformat(), confirmed=confirmed,
                reserved_attempts=reserved, invitation_limit=cfg['invitations_per_day'], lead_states=states,
                shortfall=max(0, cfg['invitations_per_day'] - confirmed['invite']),
                remaining_daily_allowance=max(0, cfg['invitations_per_day'] - confirmed['invite'] - reserved['invite']),
                held_reasons=reasons, automatic_review=review, worker_phase=health.get('phase'), last_error=health.get('last_error'),
                constraints=constraints,
                window_complete=now.astimezone(zone).date() > day or now.astimezone(zone).hour >= cfg['working_hours'][1])
    atomic_json(directory / 'reports' / (str(day) + '.json'), data)
    return data


class Monitor:
    def __init__(self, cfg, directory, now=None):
        self.cfg, self.directory = cfg, directory
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.stop = threading.Event()

    def tick(self):
        from outreach_state import automatic_backup
        now = self.now()
        health = read_status(self.directory / 'worker-status.json')
        atomic_json(self.directory / 'heartbeat.json', {'pid': os.getpid(), 'at': now.isoformat(), 'phase': health.get('phase')})
        if health.get('phase') in ('blocked', 'failed'):
            alert(self.directory, self.cfg, health['phase'], health.get('last_error') or 'Worker needs attention', now)
        if health.get('phase') == 'backoff' and health.get('consecutive_cycle_failures', 0) >= 3:
            alert(self.directory, self.cfg, 'recovery stalled', 'Several browser cycles failed. Automatic cooldown continues; inspect the worker and held contacts.', now)
        activity = read_status(self.directory / 'progress.json')
        last = activity.get('updated_at') or health.get('updated_at')
        if health.get('phase') == 'working' and last and (now - datetime.fromisoformat(last)).total_seconds() > self.cfg['stalled_after_seconds']:
            alert(self.directory, self.cfg, 'stalled', 'No campaign progress for several minutes. Check outreach status; no process was force-killed.', now)
        automatic_backup(self.directory, self.cfg, now)
        today = now.astimezone(ZoneInfo(self.cfg['timezone'])).date()
        daily_report(self.directory, self.cfg, today, now)
        daily_report(self.directory, self.cfg, today - timedelta(days=1), now)

    def run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                LOG.error('Monitoring/backup check failed: %s', exc)
                try:
                    alert(self.directory, self.cfg, 'monitoring failed', str(exc), self.now())
                except OSError:
                    pass
            self.stop.wait(self.cfg['heartbeat_interval_seconds'])

    def __enter__(self):
        self.thread = threading.Thread(target=self.run, name='outreach-monitor', daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=10)
        try:
            self.tick()
        except Exception as exc:
            LOG.error('Final monitoring snapshot failed: %s', exc)
