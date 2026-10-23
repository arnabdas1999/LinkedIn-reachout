"""Current-user process supervision and explicit Windows logon registration."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import argparse
import csv
import hashlib
import io
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

from outreach_config import load_settings, state_path
from outreach_monitor import alert, atomic_json, read_status


def control(directory, desired):
    atomic_json(directory / 'control.json', {'desired': desired, 'requested_at': datetime.now(timezone.utc).isoformat()})


def task_name(cfg):
    identity = hashlib.sha256(cfg['_settings_path'].casefold().encode()).hexdigest()[:12]
    return 'LinkedInOutreach-' + identity


def task_xml(cfg, user_id, executable):
    namespace = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
    ET.register_namespace('', namespace)
    def element(parent, name, text=None, **attrs):
        node = ET.SubElement(parent, '{' + namespace + '}' + name, attrs)
        node.text = text
        return node
    root = ET.Element('{' + namespace + '}Task', {'version': '1.2'})
    registration = element(root, 'RegistrationInfo')
    element(registration, 'Description', 'Run this user-owned local outreach supervisor after login. Account blocks stay paused.')
    triggers = element(root, 'Triggers')
    trigger = element(triggers, 'LogonTrigger')
    element(trigger, 'Enabled', 'true')
    element(trigger, 'UserId', user_id)
    principals = element(root, 'Principals')
    principal = element(principals, 'Principal', id='Author')
    element(principal, 'UserId', user_id)
    element(principal, 'LogonType', 'InteractiveToken')
    element(principal, 'RunLevel', 'LeastPrivilege')
    settings = element(root, 'Settings')
    for name, value in (('MultipleInstancesPolicy', 'IgnoreNew'), ('DisallowStartIfOnBatteries', 'false'),
                        ('StopIfGoingOnBatteries', 'false'), ('StartWhenAvailable', 'true'),
                        ('Hidden', 'true'), ('ExecutionTimeLimit', 'PT0S')):
        element(settings, name, value)
    restart = element(settings, 'RestartOnFailure')
    element(restart, 'Interval', 'PT1M')
    element(restart, 'Count', '3')
    actions = element(root, 'Actions', Context='Author')
    execute = element(actions, 'Exec')
    element(execute, 'Command', str(executable))
    element(execute, 'Arguments', subprocess.list2cmdline(['-m', 'outreach_service', '--settings', cfg['_settings_path']]))
    element(execute, 'WorkingDirectory', cfg['_workspace'])
    return ET.tostring(root, encoding='unicode')


def install_startup(cfg):
    if sys.platform != 'win32':
        raise ValueError('Automatic login registration currently supports Windows only')
    executable = Path(sys.executable).with_name('pythonw.exe')
    if not executable.is_file():
        raise ValueError('A Python installation with pythonw.exe is required for hidden login startup')
    result = subprocess.run(['whoami.exe', '/user', '/fo', 'csv', '/nh'], check=True, capture_output=True, text=True)
    user_id = next(csv.reader(io.StringIO(result.stdout)))[1]
    directory = state_path(cfg)
    path = directory / 'startup-task.xml'
    path.write_text(task_xml(cfg, user_id, executable), encoding='utf-16')
    subprocess.run(['schtasks.exe', '/Create', '/TN', task_name(cfg), '/XML', str(path), '/F'], check=True, capture_output=True, text=True)
    atomic_json(directory / 'startup-registration.json', {'task_name': task_name(cfg), 'settings': cfg['_settings_path']})
    return task_name(cfg)


def uninstall_startup(cfg):
    if sys.platform != 'win32':
        raise ValueError('Windows startup task is unavailable on this platform')
    registration = read_status(state_path(cfg) / 'startup-registration.json')
    if registration.get('task_name') != task_name(cfg) or registration.get('settings') != cfg['_settings_path']:
        raise ValueError('No matching app-owned startup registration was recorded')
    subprocess.run(['schtasks.exe', '/Delete', '/TN', task_name(cfg), '/F'], check=True, capture_output=True, text=True)
    (state_path(cfg) / 'startup-registration.json').unlink()


def supervise(cfg, *, wait=time.sleep, stop=None, now=None, refresh=None):
    from outreach import ProcessLock, Store, campaign_finished
    from outreach_setup import start_worker, worker_running
    directory = state_path(cfg)
    directory.mkdir(parents=True, exist_ok=True)
    stop = stop or (lambda: (directory / 'stop-supervisor.request').exists())
    now = now or (lambda: datetime.now(timezone.utc))
    with ProcessLock(directory / 'supervisor.lock'):
        while not stop():
            try:
                latest = refresh() if refresh else load_settings(cfg['_settings_path'])
                if state_path(latest) != directory:
                    alert(directory, cfg, 'state moved', 'State location changed. Start supervision again using the updated settings.', now())
                    return
                cfg = latest
            except Exception as exc:
                alert(directory, cfg, 'configuration needs attention', str(exc), now())
                wait(15)
                continue
            current = read_status(directory / 'control.json')
            health = read_status(directory / 'worker-status.json')
            allowed = current.get('desired') == 'running'
            latched = (health.get('phase') in ('blocked', 'failed') and
                       datetime.fromisoformat(health['updated_at']) >= datetime.fromisoformat(current.get('requested_at', '1970-01-01T00:00:00+00:00')))
            atomic_json(directory / 'supervisor-status.json', {'pid': os.getpid(), 'at': now().isoformat(), 'desired': current.get('desired', 'paused'), 'latched': latched})
            if allowed and not latched and not worker_running(directory):
                finished = False
                database = directory / 'outreach.sqlite3'
                if database.exists():
                    try:
                        store = Store(database, readonly=True)
                        try:
                            finished = campaign_finished(store, cfg)
                        finally:
                            store.close()
                    except Exception as exc:
                        alert(directory, cfg, 'state unavailable', str(exc), now())
                        wait(15)
                        continue
                if not finished:
                    try:
                        # Explicit start records intent; a recovery must not reset
                        # that intent or bypass a newly recorded account block.
                        start_worker(cfg, record_intent=False)
                    except Exception as exc:
                        from outreach_worker import worker_status
                        worker_status(directory, 'failed', now(), error=str(exc))
                        alert(directory, cfg, 'worker restart failed', str(exc), now())
            heartbeat = read_status(directory / 'heartbeat.json')
            if allowed and worker_running(directory) and heartbeat.get('at'):
                if (now() - datetime.fromisoformat(heartbeat['at'])).total_seconds() > cfg['stalled_after_seconds']:
                    alert(directory, cfg, 'stalled', 'Worker heartbeat is stale; inspect the campaign before restarting.', now())
            if latched:
                alert(directory, cfg, health['phase'], health.get('last_error') or 'Worker requires attention', now())
            wait(15)


def start_supervisor(cfg):
    from outreach_setup import worker_running
    directory = state_path(cfg)
    if worker_running(directory, lock_name='supervisor.lock'):
        return False
    (directory / 'stop-supervisor.request').unlink(missing_ok=True)
    kwargs = {'cwd': cfg['_workspace'], 'stdin': subprocess.DEVNULL,
              'env': dict(os.environ, PYTHONIOENCODING='utf-8')}
    if sys.platform == 'win32':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs['start_new_session'] = True
    with (directory / 'supervisor.log').open('a', encoding='utf-8') as stream:
        subprocess.Popen([sys.executable, '-m', 'outreach_service', '--settings', cfg['_settings_path']],
                         stdout=stream, stderr=stream, **kwargs)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', type=Path, required=True)
    args = parser.parse_args()
    cfg = load_settings(args.settings)
    directory = state_path(cfg)
    directory.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(directory / 'supervisor.log', encoding='utf-8')
    logging.getLogger('outreach').addHandler(handler)
    with (directory / 'supervisor-output.log').open('a', encoding='utf-8') as stream:
        with redirect_stdout(stream), redirect_stderr(stream):
            try:
                supervise(cfg)
            except Exception:
                from outreach_setup import worker_running
                if not worker_running(directory, lock_name='supervisor.lock'):
                    raise


if __name__ == '__main__':
    main()
