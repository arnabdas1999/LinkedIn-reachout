"""Versioned state, consistent backups, and conservative restore/retry controls."""

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import uuid

SCHEMA_VERSION = 4


def check_database(db):
    if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise ValueError('Database integrity check failed')
    if db.execute('PRAGMA foreign_key_check').fetchone():
        raise ValueError('Database has invalid references')
    for table in ('leads', 'actions'):
        if not db.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name=?', (table,)).fetchone():
            raise ValueError('Not an outreach campaign database')


def backup_database(source, destination=None):
    source = Path(source).resolve()
    if destination is None:
        folder = source.parent / 'backups'
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / ('outreach-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.sqlite3')
    destination = Path(destination).resolve()
    if source == destination or destination.exists():
        raise ValueError('Backup destination must be a new file')
    destination.parent.mkdir(parents=True, exist_ok=True)
    pending = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src:
            with closing(sqlite3.connect(pending)) as target:
                src.backup(target)
                check_database(target)
        pending.replace(destination)
    except BaseException:
        pending.unlink(missing_ok=True)
        raise
    return destination


def migrate(db, path, existed):
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version > SCHEMA_VERSION:
        raise ValueError('This campaign uses a newer schema; upgrade the application before opening it')
    if version == SCHEMA_VERSION:
        return
    if existed:
        backup_database(path)
    with db:
        columns = {row[1] for row in db.execute('PRAGMA table_info(leads)')}
        if 'failure_count' not in columns:
            db.execute('ALTER TABLE leads ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0')
        for name, definition in (
            ('review_attempts', 'INTEGER NOT NULL DEFAULT 0'),
            ('review_next_at', 'TEXT'), ('review_exhausted', 'INTEGER NOT NULL DEFAULT 0'),
            ('review_last_result', "TEXT NOT NULL DEFAULT ''"),
            ('automatic_resumes', 'INTEGER NOT NULL DEFAULT 0'),
            ('observed_company', "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in columns:
                db.execute(f'ALTER TABLE leads ADD COLUMN {name} {definition}')
        columns = {row[1] for row in db.execute('PRAGMA table_info(actions)')}
        for name in ('quota_at', 'click_started_at', 'finished_at'):
            if name not in columns:
                db.execute(f'ALTER TABLE actions ADD COLUMN {name} TEXT')
        if 'guard_version' not in columns:
            # Historical NULL click markers cannot prove a click never happened.
            db.execute('ALTER TABLE actions ADD COLUMN guard_version INTEGER NOT NULL DEFAULT 0')
        db.execute('''CREATE TABLE IF NOT EXISTS review_events (
            id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL REFERENCES leads(id),
            at TEXT NOT NULL, outcome TEXT NOT NULL, detail TEXT NOT NULL,
            action_snapshot TEXT NOT NULL DEFAULT '{}'
        )''')
        db.execute('UPDATE actions SET quota_at=coalesce(sent_at,started_at) WHERE quota_at IS NULL')
        db.execute('CREATE TABLE IF NOT EXISTS campaign_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        db.execute('INSERT OR IGNORE INTO campaign_meta VALUES (?,?)', ('campaign_id', str(uuid.uuid4())))
        db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')


def meta(db, key, value=None):
    if value is not None:
        with db:
            db.execute('INSERT OR REPLACE INTO campaign_meta VALUES (?,?)', (key, str(value)))
    row = db.execute('SELECT value FROM campaign_meta WHERE key=?', (key,)).fetchone()
    return row[0] if row else None


def restore_database(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target:
        raise ValueError('Choose a separate backup file')
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as incoming:
        check_database(incoming)
        query = 'SELECT l.url,a.kind,a.status,a.started_at,a.sent_at,a.message FROM actions a JOIN leads l ON l.id=a.lead_id'
        incoming_actions = {tuple(row) for row in incoming.execute(query)}
        if target.exists():
            try:
                with closing(sqlite3.connect(target.as_uri() + '?mode=ro', uri=True)) as current:
                    check_database(current)
                    current_actions = {tuple(row) for row in current.execute(query)}
                    current_id = meta(current, 'campaign_id') if current.execute("SELECT 1 FROM sqlite_master WHERE name='campaign_meta'").fetchone() else None
            except (sqlite3.DatabaseError, ValueError):
                current_actions = None
                current_id = None
            incoming_id = meta(incoming, 'campaign_id') if incoming.execute("SELECT 1 FROM sqlite_master WHERE name='campaign_meta'").fetchone() else None
            if current_id and incoming_id and current_id != incoming_id:
                raise ValueError('Backup belongs to a different campaign')
            if current_actions is not None and not current_actions <= incoming_actions:
                raise ValueError('Restore refused: backup would lose newer or changed send history')
            if current_actions is not None:
                backup_database(target)
            else:
                import shutil
                preserved = target.with_name(target.name + '.' + uuid.uuid4().hex + '.damaged')
                shutil.copy2(target, preserved)
        pending = target.with_name('restore-' + uuid.uuid4().hex + '.sqlite3')
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with closing(sqlite3.connect(pending)) as restored:
                incoming.backup(restored)
                migrate(restored, pending, False)
                meta(restored, 'restore_review_required', '1')
                check_database(restored)
            pending.replace(target)
        finally:
            pending.unlink(missing_ok=True)


def resume_contact(store, lead_id):
    lead = store.lead(lead_id)
    if lead is None or lead['state'] != 'needs_review':
        raise ValueError('Only a held contact can be resumed')
    if store.db.execute("SELECT 1 FROM actions WHERE lead_id=? AND status IN ('prepared','unknown','cancelled')", (lead_id,)).fetchone():
        raise ValueError('This contact has a possible send or cancelled action; inspect/reconcile it first')
    if lead['message_2_at']:
        raise ValueError('This sequence is already complete')
    state = 'message_1_sent' if lead['message_1_at'] else 'accepted' if lead['accepted_at'] else 'invited' if lead['invited_at'] else 'new'
    store.update(lead_id, state=state, reason='', failure_count=0, next_check_at=None,
                 review_attempts=0, review_next_at=None, review_exhausted=0, review_last_result='Resumed by operator')


def automatic_backup(directory, cfg, now):
    from outreach_monitor import atomic_json, read_status
    database = directory / 'outreach.sqlite3'
    if not database.exists():
        return None
    marker = directory / 'backup-status.json'
    prior = read_status(marker)
    if prior.get('completed_at') and (now - datetime.fromisoformat(prior['completed_at'])).total_seconds() < cfg['backup_interval_hours'] * 3600:
        return None
    backup = backup_database(database)
    atomic_json(marker, {'completed_at': now.isoformat(), 'path': str(backup)})
    retained = sorted((directory / 'backups').glob('outreach-*.sqlite3'), reverse=True)
    for old in retained[cfg['backup_retention']:]:
        old.unlink()
    return backup


def migrate_directory(cfg, destination=None):
    import shutil
    from outreach import ProcessLock
    from outreach_config import durable_state_path, state_path
    from outreach_monitor import atomic_json
    source = state_path(cfg)
    target = Path(destination).expanduser().resolve() if destination else durable_state_path(cfg['_workspace'])
    if source == target:
        return target
    if target.exists() and any(target.iterdir()):
        raise ValueError('New state directory must be empty')
    with ProcessLock(source / 'supervisor.lock'), ProcessLock(source / 'process.lock'):
        target.mkdir(parents=True, exist_ok=True)
        backup_database(source / 'outreach.sqlite3', target / 'outreach.sqlite3')
        for file in source.iterdir():
            if file.is_file() and (file.suffix in ('.json', '.jsonl', '.log') or file.name == 'startup-task.xml'):
                shutil.copy2(file, target / file.name)
        for name in ('backups', 'reports'):
            old = source / name
            if old.is_dir():
                (target / name).mkdir(exist_ok=True)
                for file in old.iterdir():
                    if file.is_file() and file.suffix in ('.sqlite3', '.json'):
                        shutil.copy2(file, target / name / file.name)
        settings = Path(cfg['_settings_path'])
        raw = json.loads(settings.read_text(encoding='utf-8-sig'))
        shutil.copy2(settings, target / 'settings-before-state-migration.json')
        raw['state_directory'] = str(target)
        atomic_json(settings, raw)
    return target
