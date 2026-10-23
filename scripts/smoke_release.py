"""Install a built wheel into a clean environment; no LinkedIn traffic or sends."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import venv


def main():
    wheels = sorted(Path('dist').resolve().glob('*.whl'))
    if len(wheels) != 1:
        raise ValueError('Build exactly one wheel before running the release smoke check')
    with tempfile.TemporaryDirectory(prefix='outreach-release-') as directory:
        root = Path(directory)
        environment = root / 'venv'
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        workspace = root / 'workspace'
        workspace.mkdir()
        def run(*args):
            return subprocess.run([str(python), *args], cwd=workspace, check=True, capture_output=True, text=True)
        run('-m', 'pip', '--disable-pip-version-check', 'install', str(wheels[0]))
        run('-c', "import outreach,outreach_worker,outreach_service,outreach_state,outreach_monitor,outreach_review; assert 'site-packages' in outreach.__file__")
        source = workspace / 'contacts.csv'
        source.write_text('Full Name,Company,LinkedIn Profile\nAlex Example,Example Company,https://www.linkedin.com/in/fixture-not-real/\n', encoding='utf-8')
        run('-m', 'outreach', 'init', '--csv', str(source), '--browser', 'chromium', '--timezone', 'UTC', '--state-directory', str(workspace / 'state'))
        settings = workspace / 'settings.json'
        cfg = json.loads(settings.read_text())
        cfg['working_days'] = [(datetime.now(timezone.utc).weekday() + 2) % 7]
        cfg['desktop_notifications'] = False
        settings.write_text(json.dumps(cfg), encoding='utf-8')
        run('-m', 'outreach', 'preview', '--limit', '1')
        run('-m', 'outreach', 'backup')
        run('-m', 'outreach', 'report')
        state = workspace / 'state'
        def health():
            return json.loads(run('-m', 'outreach', 'health').stdout)
        def wait_for(predicate, description, seconds=40):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                value = health()
                if predicate(value):
                    return value
                time.sleep(0.25)
            raise RuntimeError(description)
        try:
            run('-m', 'outreach', 'start', '--live')
            run('-m', 'outreach', 'stop-watch')
            deadline = time.monotonic() + 20
            while (state / 'watcher-ready.json').exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            if (state / 'watcher-ready.json').exists():
                raise RuntimeError('Wheel-installed worker did not stop cleanly')
            stopped_health = json.loads((state / 'worker-status.json').read_text())
            if stopped_health['phase'] != 'stopped':
                raise RuntimeError('Wheel-installed worker did not report a clean shutdown')
            # This synthetic campaign is outside working days: a real process
            # restart cannot launch a browser or send to any profile.
            run('-m', 'outreach', 'supervise', '--live')
            current = wait_for(lambda h: h['supervisor_running'] and h['worker_running'] and
                               h['worker-status'].get('phase') == 'outside_working_hours',
                               'Supervisor did not start an idle worker')
            owned_pid = current['worker-status']['pid']
            ready = json.loads((state / 'watcher-ready.json').read_text())
            if ready['pid'] != owned_pid:
                raise RuntimeError('Owned fixture worker identity could not be verified')
            os.kill(owned_pid, signal.SIGTERM)
            wait_for(lambda h: h['worker_running'] and h['worker-status'].get('pid') != owned_pid and
                     h['worker-status'].get('phase') == 'outside_working_hours',
                     'Supervisor did not recover the terminated fixture worker')
            run('-m', 'outreach', 'stop-watch')
            wait_for(lambda h: not h['worker_running'], 'Fixture worker did not pause')
            # Cover a complete parent polling interval and verify intent was
            # retained instead of restarting an explicitly paused campaign.
            time.sleep(16)
            paused = health()
            if paused['worker_running'] or paused['control'].get('desired') != 'paused':
                raise RuntimeError('Supervisor did not preserve the explicit pause')
            run('-m', 'outreach', 'stop-supervisor')
            wait_for(lambda h: not h['worker_running'] and not h['supervisor_running'],
                     'Fixture supervisor did not stop')
            result = run('-c', "import sqlite3; c=sqlite3.connect('state/outreach.sqlite3'); assert c.execute('select count(*) from actions').fetchone()[0]==0; print('zero actions')")
            print('Clean-wheel setup, preview, backup, report, detached start/stop, process recovery and pause:', result.stdout.strip())
        finally:
            if state.exists():
                run('-m', 'outreach', 'stop-supervisor')
                wait_for(lambda h: not h['worker_running'] and not h['supervisor_running'],
                         'Release smoke cleanup could not stop its owned processes')
    return 0


if __name__ == '__main__':
    sys.exit(main())
