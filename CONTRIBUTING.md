# Contributing

Use Python 3.11+ and a virtual environment. Install `python -m pip install -e '.[dev]'` and `python -m playwright install chromium`. Run the commands under Development in the README before submitting a change.

Keep browser interactions in `linkedin_browser.py`, source repair in `lead_data.py`, settings in `outreach_config.py`, setup/lifecycle in `outreach_setup.py`, and campaign/journal logic in `outreach.py`. State migrations/backups live in `outreach_state.py`, health/alerts/reports in `outreach_monitor.py`, browser-cycle recovery in `outreach_worker.py`, and process supervision/startup in `outreach_service.py`. Shared stop errors live in `outreach_errors.py` so CLI and adapter exception types remain identical.

Add regression coverage for changed send, scheduling, recipient verification, recovery or worker-lifecycle behavior. Browser tests must intercept every page request used by their fixtures. Never run real outreach in CI. Use synthetic contacts and stripped HTML fixtures; do not contribute real names, messages, cookies or authentication state.

Do not add stealth settings, CAPTCHA bypasses, proxy/account rotation or private LinkedIn endpoints. An ambiguous send must remain blocked until explicitly reconciled. Never change saved timestamps or action history while normalizing source data.

For bug reports include OS, Python/app/browser versions, the command, and a redacted error. Keep private logs and contact CSVs out of public issues. Changes to dependencies must pass the same tests and package privacy checks.
