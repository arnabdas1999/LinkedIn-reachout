"""Setup, diagnostics and detached worker lifecycle; no external messages."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import time

from lead_data import csv_text, read_clean_csv
from outreach_config import DEFAULTS, TEMPLATE_KEYS, load_settings, resolve_path, state_path
from outreach_errors import StopRun


def initialize(args):
    path = args.settings.expanduser().resolve()
    if path.exists():
        raise ValueError(f"Settings already exist: {path}. Edit them in place; init never overwrites a campaign.")
    csv_file, browser, messages = args.csv, args.browser, args.messages
    if not csv_file:
        if not sys.stdin.isatty():
            raise ValueError("Supply --csv PATH and --browser chromium|chrome|msedge|brave, or run init interactively")
        csv_file = input("Path to your contacts CSV: ").strip().strip('"')
        browser = browser or input("Browser [chromium/chrome/msedge/brave] (chromium): ").strip() or "chromium"
        messages = messages or input("Message templates JSON (Enter for starter templates): ").strip().strip('"') or None
    csv_path = Path(csv_file).expanduser().resolve()
    _, rows, _ = read_clean_csv(csv_path)
    if not rows:
        raise ValueError("The CSV has no contacts")
    cfg = deepcopy(DEFAULTS)
    cfg.update(csv_file=str(csv_path), browser_channel=browser or "chromium", timezone=args.timezone)
    path.parent.mkdir(parents=True, exist_ok=True)
    if messages:
        message_path = Path(messages).expanduser().resolve()
        if not message_path.is_file():
            raise ValueError(f"Message file not found: {message_path}")
    else:
        message_path = path.parent / "messages.json"
        if message_path.exists():
            raise ValueError("messages.json already exists. Pass --messages messages.json to use it.")
    cfg["message_templates_file"] = str(message_path)
    # Validate a draft before writing the real settings file.
    draft = deepcopy(cfg)
    if not messages:
        draft["message_templates_file"] = None
    draft_path = path.with_name(path.name + ".init.tmp")
    try:
        draft_path.write_text(json.dumps(draft, indent=2), encoding="utf-8")
        load_settings(draft_path)
    finally:
        draft_path.unlink(missing_ok=True)
    if not messages:
        message_path.write_text(json.dumps({key: cfg[key] for key in TEMPLATE_KEYS}, indent=2) + "\n", encoding="utf-8")
    for key in TEMPLATE_KEYS:
        del cfg[key]
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(cfg, indent=2) + "\n")
    print(f"Created {path}\nMessages: {message_path}\nContacts: {len(rows)}; browser: {cfg['browser_channel']}")
    print("Review your messages, then run: linkedin-outreach doctor, login, preview, start --live")
    return 0


def prepare_source(cfg, directory):
    """Write a private normalized copy; never modify the user's input file."""
    source = resolve_path(cfg["csv_file"], Path(cfg["_workspace"]))
    headers, rows, report = read_clean_csv(source)
    output = directory / "contacts.cleaned.csv"
    if source == output:
        raise ValueError("csv_file must point to your input, not the app's .local/contacts.cleaned.csv")
    text = csv_text(headers, rows)
    if not output.is_file() or output.read_text(encoding="utf-8-sig") != text.replace("\r\n", "\n"):
        temporary = output.with_suffix(".csv.tmp")
        try:
            temporary.write_text(text, encoding="utf-8-sig", newline="")
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
    report.update(source=str(source), cleaned_file=str(output))
    (directory / "csv-cleanup-report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return output, report


def doctor(cfg):
    from outreach import profile_directory, render
    from linkedin_browser import brave_executable
    source = resolve_path(cfg["csv_file"], Path(cfg["_workspace"]))
    _, rows, report = read_clean_csv(source)
    problems = []
    for index, row in enumerate(rows, 2):
        try:
            render({"first_name": row["First Name"], "full_name": row["Full Name"],
                    "company": row.get("Company", ""), "city": row.get("City", ""),
                    "region": row.get("State or Province", "")}, cfg)
        except ValueError as exc:
            problems.append({"row": index, "error": str(exc)})
    browser = cfg["browser_channel"]
    executable = cfg["browser_executable"]
    if executable:
        executable = str(resolve_path(executable, Path(cfg["_workspace"])))
        if not Path(executable).is_file():
            raise ValueError(f"Browser executable not found: {executable}")
    elif browser == "brave":
        executable = brave_executable()
    elif browser == "chromium":
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            executable = pw.chromium.executable_path
            if not Path(executable).is_file():
                raise ValueError("Chromium is missing. Run: python -m playwright install chromium")
    elif browser in ("chrome", "msedge"):
        # A launch smoke check catches missing branded channels without network.
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            instance = pw.chromium.launch(channel=browser, headless=True, chromium_sandbox=True)
            instance.close()
        executable = f"Installed {browser} (launch verified)"
    result = {"settings": cfg["_settings_path"], "csv": str(source), "unique_contacts": len(rows),
              "duplicate_rows": report["duplicates_removed"], "invalid_url_rows": report["invalid_url_rows"],
              "template_errors": problems[:10], "template_error_count": len(problems),
              "browser": browser, "executable": executable, "headless_runs": cfg["headless"],
              "state_directory": str(state_path(cfg)),
              "browser_profile": str(profile_directory(browser, cfg["_workspace"], cfg["browser_profile_directory"]))}
    print(json.dumps(result, indent=2))
    print("Offline checks complete. Login and live LinkedIn compatibility are checked separately.")
    return 0 if rows and not problems and not report["invalid_url_rows"] else 2


def worker_running(directory):
    from outreach import ProcessLock
    try:
        with ProcessLock(directory / "process.lock"):
            return False
    except StopRun:
        return True


def install_browser(cfg):
    if cfg["browser_channel"] == "chromium" and not cfg["browser_executable"]:
        return subprocess.call([sys.executable, "-m", "playwright", "install", "chromium"])
    print("Using your installed browser. Run doctor to check that it is available.")
    return 0


def start_worker(cfg):
    directory = state_path(cfg)
    directory.mkdir(parents=True, exist_ok=True)
    from outreach import ProcessLock
    with ProcessLock(directory / "start.lock"):
        if worker_running(directory):
            raise StopRun("An outreach worker is already running. Use status or stop-watch.")
        ready = directory / "watcher-ready.json"
        ready.unlink(missing_ok=True)
        command = [sys.executable, "-u", "-m", "outreach", "--settings", cfg["_settings_path"], "watch", "--live"]
        kwargs = {"cwd": cfg["_workspace"], "stdin": subprocess.DEVNULL, "close_fds": True}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        with (directory / "watch-output.log").open("w", encoding="utf-8") as output, (directory / "watch-error.log").open("w", encoding="utf-8") as errors:
            process = subprocess.Popen(command, stdout=output, stderr=errors, **kwargs)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                if process.returncode == 0:
                    print("Watcher finished normally; no active work remains or a stop was requested. Run status for details.")
                    return 0
                raise StopRun(f"Watcher exited. Read {directory / 'watch-error.log'}")
            if ready.exists() and worker_running(directory):
                print(f"Background watcher started (PID {process.pid}). Browser windows stay hidden when headless=true.")
                print(f"Logs: {directory}. Stop with: linkedin-outreach stop-watch")
                return 0
            time.sleep(0.2)
        print(f"Watcher launched (PID {process.pid}); startup is still in progress. Run status and check {directory / 'watch-error.log'}.")
        return 0
