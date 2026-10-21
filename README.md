# LinkedIn Outreach Local

A local Python app that reads your contacts CSV, personalizes your message templates, and tracks connection requests and two follow-up messages. Choose Chromium, Chrome, Edge, or Brave. Sign in once in a visible, separate browser profile; routine automation runs **headlessly**, with no browser windows appearing on your desktop.

**Platform limitation:** LinkedIn [prohibits third-party automation](https://www.linkedin.com/help/linkedin/answer/a1340567/), and accounts can be restricted. This project is unaffiliated with LinkedIn. Premium does not remove that restriction. There are no stealth plugins, CAPTCHA solvers, account rotation, or private API calls.

## What it does

- Imports CSV files with common header variants, UTF-8/BOM, and comma, semicolon or tab delimiters.
- Recovers missing first names from Full Name, merges duplicate profile URLs, and cleans spacing and incorrect country-as-city values.
- Writes a private cleaned copy; your input CSV is never overwritten.
- Supports your connection note, a message after acceptance, and a later follow-up.
- Stops a person's sequence when a reply or an unrecognized/manual message appears.
- Journals sends before clicking, prevents parallel workers, and never blindly retries an uncertain send.
- Provides an offline preview, setup diagnostics, background start/stop, progress and rotating logs.

## Requirements

- Python **3.11 or newer**, internet access, and a LinkedIn account.
- Windows, macOS or Linux with an interactive desktop for the initial login.
- English-language LinkedIn UI. Website layout changes can require adapter updates.
- Installed Chrome/Edge/Brave, or the bundled Chromium browser installed during setup.

The browser adapter is Chromium-based. Firefox and Safari are not supported. Brave uses a custom executable and can behave differently from Playwright's supported Chromium/Chrome/Edge channels. See [Playwright's browser documentation](https://playwright.dev/python/docs/browsers).

## Quick start on Windows

1. Download this repository using **Code → Download ZIP**, and extract it to a writable folder.
2. Install Python from [python.org](https://www.python.org/downloads/), including the Python launcher or PATH option.
3. In **File Explorer**, double-click **Setup.cmd**. Opening a `.cmd` file in VS Code only opens its text; it does not run it.
4. Supply the CSV path, browser choice and optional message-template JSON when prompted. Setup creates a virtual environment, installs the app, installs Chromium if selected, and checks the inputs.
5. Review the generated **messages.json** and **settings.json**. Use your own contacts CSV; input spreadsheets are kept local and are not included in this repository.
6. Open **Start LinkedIn Outreach.cmd**. Choose **1** to sign in, then **2** to preview your messages.
7. Choose **5** to start background automation. You can close the menu window after it starts.

Use **Show Outreach Progress.cmd** to check progress and **Stop LinkedIn Outreach.cmd** to stop. **Run in Background.cmd** starts the same background worker directly.

The initial campaign cap is **one invitation total**, even across restarts and subsequent days. Review the result before increasing `campaign_invitation_limit` and the daily/weekly caps in your private settings. These caps are local controls, not LinkedIn-approved thresholds.

For an ongoing campaign, set `campaign_invitation_limit` to `null`. Daily and rolling seven-day invitation limits still apply. For example, 25 per day can use `invitations_per_day: 25`, `invitations_per_7_days: 175`, and `invitations_per_run: 5`. Include all seven values `[0, 1, 2, 3, 4, 5, 6]` in `working_days` to include weekends. Follow-up messages have separate limits and do not consume the new-invitation allowance.

## Command-line setup

On macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
linkedin-outreach init
linkedin-outreach install-browser
linkedin-outreach doctor
linkedin-outreach login
linkedin-outreach preview --limit 3
linkedin-outreach start --live
```

On Windows, the equivalent installation commands are:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\linkedin-outreach.exe init
.\.venv\Scripts\linkedin-outreach.exe install-browser
.\.venv\Scripts\linkedin-outreach.exe doctor
```

For the remaining commands, use `.\.venv\Scripts\linkedin-outreach.exe` in place of `linkedin-outreach`, or activate the virtual environment. `python outreach.py ...` also works from a source checkout.

For setup without prompts:

```bash
linkedin-outreach init --csv /path/to/contacts.csv --messages /path/to/messages.json --browser brave --timezone America/New_York
```

Omit `--messages` to create starter templates. `init` refuses to overwrite existing settings or messages. Linux may need `python -m playwright install --with-deps chromium`; this can request OS package-manager privileges. Chrome, Edge and Brave must be installed separately. `install-browser` only downloads bundled Chromium when selected.

## CSV format

Supply your own contacts CSV. A minimal header is `Full Name,Company,LinkedIn Profile`; add the optional fields below as needed. Required information:

| Field | Accepted examples | Behavior |
| --- | --- | --- |
| Full Name, or First Name | `Full Name`, `name`, `first_name` | Missing Full Name is assembled from supplied name parts. Exact profile-name matching helps verify identity. |
| Company | `Company`, `company_name`, `organization` | Used in follow-ups. Missing values need correction. |
| LinkedIn Profile | `LinkedIn Profile`, `linkedin_url`, `profile_url` | Must be a public `https://www.linkedin.com/in/.../` URL. |
| Optional name parts | `First Name`, `Last Name`, `surname` | Missing first names are derived from Full Name. |
| Optional location | `City`, `State or Province`, `state`, `region`, `Country` | Uses city, then state/region. Missing location uses the location-free note. |
| Optional title | `Job Title`, `title`, `position` | Stored for reference. |

Names are not globally title-cased, and missing surnames or locations are not invented. Duplicate URLs merge complementary fields while preserving the first supplied nonempty value. Conflicts and invalid URLs appear in `.local/csv-cleanup-report.json`. Ambiguous duplicate headers or broken quoting need correction before import. Source job/company information is not independently verified for freshness.

Changing a CSV imports new profiles and updates contact details; it preserves existing send history and sequence states. Removing a row does **not** cancel a sequence already started. Use `stop LEAD_ID` to cancel it. Stop the worker before changing inputs/settings, then start again.

## Message templates

Edit the `messages.json` created by setup, or supply your own template JSON. The three sequence keys are `connection_note`, `message_1` and `message_2`. Supply `connection_note_without_location` for your preferred fallback; otherwise the starter location-free note is used.

Supported placeholders:

| Standard syntax | Also accepted |
| --- | --- |
| `{first_name}` | `{{FirstName}}` |
| `{company}` | `{{CompanyName}}` |
| `{location}` | `{{location}}` |

Use `\n\n` inside a JSON string for a paragraph break. Unsupported placeholders fail validation before any browser activity. Personalized invitation notes over 300 characters are held for review instead of truncated. Your account may have stricter availability/limits, which the browser adapter respects when displayed.

## Browser windows and login

`headless: true` is the default. The `login` command explicitly opens a visible browser and saves the session in a **dedicated profile**, separate from your normal browser. After sign-in succeeds, that window closes normally. The `run`, `watch` and `start` commands then reuse the session without displaying browser windows.

If LinkedIn requires sign-in or verification again, the worker stops and writes an error; it does not silently open a window. Run `stop-watch`, finish `login` manually, then start again. `inspect` intentionally opens a visible browser for troubleshooting. Setting `headless: false` enables visible routine runs when debugging.

The default profile lives under your user's local application-data directory (`LinkedInOutreach/profiles`), outside the repository. Older installations keep using their existing temporary profile for compatibility; if that is cleared, sign in again. `browser_profile_directory` can override the location. Never select your regular browser's personal profile. A changed browser choice requires a separate sign-in.

`browser_channel` accepts `chromium`, `chrome`, `msedge` or `brave`. For a nonstandard installation, set `browser_executable` to its executable path. New profiles keep browser sandboxing enabled. Headless mode improves desktop usability; it does not conceal automation from LinkedIn.

## Timing and limits

The default watcher checks every 30 minutes, Monday–Friday, 9am–6pm in `America/New_York`. It schedules Message 1 **24 hours after acceptance is first observed**, and Message 2 **72 hours after Message 1 was actually sent**. The precise time of acceptance is not inferred. Any reply, manual conversation or unreadable history stops that person's automated sequence.

Useful settings in [settings.example.json](settings.example.json):

| Setting | Meaning |
| --- | --- |
| `campaign_invitation_limit` | Total invitations across all runs; `null` disables the total cap while keeping daily/weekly limits. |
| `invitations_per_run`, `invitations_per_day`, `invitations_per_7_days` | Additional invitation ceilings; seven days is a rolling window. |
| `messages_per_run`, `messages_per_day` | Follow-up limits. |
| `working_days`, `working_hours`, `timezone` | Watcher schedule. Monday is 0; end hour is exclusive. |
| `message_1_delay_days`, `message_2_delay_days` | Delays after observed acceptance and Message 1. |
| `company_filter` | Exact company filter for new invitations; active follow-ups continue. |
| `state_directory` | Private campaign database, normalized CSV, reports and logs. |
| `message_templates_file` | Your separate JSON templates; inline templates also work. |

Relative paths resolve beside `settings.json`, not beside the installed Python package. Select another settings file with `linkedin-outreach --settings /path/settings.json COMMAND`. Use a separate `state_directory` for a genuinely separate campaign; keep it unchanged when upgrading to retain duplicate protection.

The computer must remain awake, online and signed into the OS. A background worker survives closing the terminal, but does not start itself after reboot. `run --live` executes one cycle immediately, including outside working hours. `watch --live` runs in the foreground; `start --live` detaches it. No actions run while the computer is asleep.

## Commands and troubleshooting

```bash
linkedin-outreach doctor                  # Offline inputs/browser checks; no LinkedIn requests
linkedin-outreach preview --limit 3       # Import and render; never send
linkedin-outreach clean-csv               # Save private normalized CSV and sync metadata
linkedin-outreach login                   # Visible manual authentication only
linkedin-outreach start --live            # Detached worker; headless by default
linkedin-outreach status                  # Counts, held leads and worker state
linkedin-outreach stop-watch              # Graceful stop; current action may finish
linkedin-outreach inspect --lead-id 1     # Visible, read-only profile inspection
linkedin-outreach stop 1                  # Stop one person's sequence, with worker stopped
```

`run` and `watch` without `--live` only preview and exit. `start` requires `--live`. A process lock prevents two workers from running against the same campaign. The worker exits when the cap has been reached and all active sequences are finished, or no eligible/active leads remain.

- **Missing browser:** run `install-browser` for Chromium, or install your chosen branded browser and run `doctor`.
- **Already running:** use `status`; stop the existing worker before login, inspection or changing settings. Do not delete lock files to bypass a live process.
- **No new invites:** check the total campaign cap, daily/weekly caps, company filter, working window and existing Pending/connected state.
- **Login/verification required:** complete `login` manually. There is no automated challenge bypass.
- **Identity mismatch before an action:** that person is held for review and verified profiles continue. Check `status` and review their CSV identity; the app does not guess that abbreviated names identify the same person.
- **Unexpected UI, login or verification issue:** the worker stops. Inspect the exact profile or complete login; website selector updates may be necessary.
- **Uncertain send:** inspect LinkedIn before resolving it, as described below.

Logs are in `.local/outreach.log` with rotation, plus `watch-output.log` and `watch-error.log` for detached runs. Logs, screenshots and CSV reports can contain contact data. Redact them before sharing. This app has no hosted backend or telemetry; normal browser traffic goes to LinkedIn. Files inside cloud-synced folders follow that folder's sync settings. Prefer a local, unsynced campaign-state directory for database reliability.

### Resolve an uncertain send

The script commits a journal entry **before** sending. If it crashes or cannot verify the outcome, live runs stop rather than risk sending a duplicate. With the worker stopped, check LinkedIn manually, then choose exactly one outcome:

```bash
linkedin-outreach reconcile LEAD_ID sent
linkedin-outreach reconcile LEAD_ID not-sent
linkedin-outreach reconcile LEAD_ID stop
```

Use `not-sent` only after verifying nothing was sent; it permits a retry. `sent` records the confirmed action, and `stop` ends the sequence. Never delete `.local/outreach.sqlite3` to reset limits: that discards the send history and duplicate protection. Back up this database with the worker stopped before an upgrade.

## Development and publishing

```bash
python -m pip install -e '.[dev]'
python -m ruff check .
python -m unittest discover -s tests -v
python -m unittest discover -s tests -p browser_checks.py -v
python -m build
python scripts/check_release.py --dist
```

Core tests use fake sends; browser tests intercept fixture pages and do not contact LinkedIn. GitHub Actions is configured for Python 3.11/3.13 on Windows, macOS and Ubuntu using bundled Chromium. CI does not use your login or send real outreach. Passing fixtures does not guarantee compatibility with every live LinkedIn layout, account or custom browser installation.

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and [the publishing guide](docs/PUBLISHING.md). Spreadsheets, CSVs, the `examples/` folder, private settings, sessions, logs, databases and generated outputs are excluded. The release checker also rejects them in tracked files and distribution packages. Setup generates starter messages locally. Licensed under the [MIT License](LICENSE).
