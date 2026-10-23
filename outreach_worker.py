"""Supervise browser cycles without retrying individual send actions."""

from datetime import timedelta, timezone
import json
import logging
import os

from playwright.sync_api import Error as BrowserError

from outreach_errors import AccountBlocked, RetryCycle, StopRequested

LOG = logging.getLogger('outreach')


def retry_delay(cfg, failures):
    return min(cfg['retry_max_seconds'], cfg['retry_initial_seconds'] * 2 ** min(max(failures - 1, 0), 20))


def worker_status(directory, phase, now, *, error='', next_retry=None, failures=0):
    data = dict(phase=phase, pid=os.getpid(), updated_at=now.astimezone(timezone.utc).isoformat(timespec='seconds'),
                last_error=error, consecutive_cycle_failures=failures,
                next_retry_at=next_retry.astimezone(timezone.utc).isoformat(timespec='seconds') if next_retry else None)
    pending = directory / 'worker-status.tmp'
    pending.write_text(json.dumps(data, indent=2), encoding='utf-8')
    pending.replace(directory / 'worker-status.json')


def watch_worker(cfg, directory, *, run_cycle, has_work, is_finished, next_retry, now, wait, stop_requested):
    from zoneinfo import ZoneInfo
    failures = 0
    try:
        while not stop_requested() and not is_finished():
            local = now().astimezone(ZoneInfo(cfg['timezone']))
            working = local.weekday() in cfg['working_days'] and cfg['working_hours'][0] <= local.hour < cfg['working_hours'][1]
            delay = cfg['watch_interval_minutes'] * 60
            if working and has_work():
                worker_status(directory, 'working', now())
                try:
                    result = run_cycle()
                except (RetryCycle, BrowserError, TimeoutError, ConnectionError) as exc:
                    failures += 1
                    delay = retry_delay(cfg, failures)
                    retry_at = now() + timedelta(seconds=delay)
                    worker_status(directory, 'backoff', now(), error=str(exc), next_retry=retry_at, failures=failures)
                    LOG.warning('RECOVERING: %s; fresh browser cycle at %s', exc, retry_at.isoformat())
                    if wait(delay):
                        break
                    continue
                failures = 0
                LOG.info('Cycle complete: %s', result)
                print(json.dumps(result), flush=True)
            retry_at = next_retry()
            if working and retry_at and retry_at > now():
                delay = min(delay, max(1, (retry_at - now()).total_seconds()))
            worker_status(directory, 'waiting' if working else 'outside_working_hours', now(),
                          next_retry=now() + timedelta(seconds=delay))
            if wait(delay):
                break
    except StopRequested:
        pass
    except AccountBlocked as exc:
        worker_status(directory, 'blocked', now(), error=str(exc))
        raise
    except BaseException as exc:
        worker_status(directory, 'failed', now(), error=str(exc) or type(exc).__name__)
        raise
    worker_status(directory, 'stopped', now())
    LOG.info('Watcher stopped; campaign finished or stop requested. Progress is saved.')
