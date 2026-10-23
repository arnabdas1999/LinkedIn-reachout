"""Portable configuration and message-template validation."""

from copy import deepcopy
import json
from pathlib import Path
import re
from string import Formatter
from zoneinfo import ZoneInfo

VERSION = "0.1.0"
TEMPLATE_KEYS = ("connection_note", "connection_note_without_location", "message_1", "message_2")
DEFAULTS = {
    "csv_file": "contacts.csv", "browser_channel": "chromium", "browser_executable": None,
    "headless": True, "state_directory": ".local", "browser_profile_directory": None,
    "message_templates_file": None, "timezone": "America/New_York",
    "campaign_invitation_limit": 1, "invitations_per_run": 1, "invitations_per_day": 1,
    "invitations_per_7_days": 5, "messages_per_run": 5, "messages_per_day": 10,
    "acceptance_checks_per_run": 20, "check_interval_minutes": 60,
    "watch_interval_minutes": 30, "minimum_seconds_between_sends": 60,
    "read_attempts": 2, "contact_retry_limit": 3, "max_consecutive_failures": 3,
    "retry_initial_seconds": 60, "retry_max_seconds": 1800,
    "message_1_delay_days": 1, "message_2_delay_days": 3,
    "working_days": [0, 1, 2, 3, 4], "working_hours": [9, 18], "company_filter": "",
    "connection_note": "Hi {first_name}, I'm looking to expand my network of engineering leaders in and around {location}. Would love to connect!",
    "connection_note_without_location": "Hi {first_name}, I'm looking to expand my network of engineering leaders. Would love to connect!",
    "message_1": "Hi {first_name}, thanks for accepting my invite! I'd love to hear what it's like on the engineering team at {company}.",
    "message_2": "Hi {first_name}, would you be open to a 15-minute chat next week about what the team at {company} is working on? Happy to work around your schedule.",
}
POSITIVE_INTS = (
    "invitations_per_run", "invitations_per_day",
    "invitations_per_7_days", "messages_per_run", "messages_per_day",
    "acceptance_checks_per_run", "check_interval_minutes", "watch_interval_minutes",
    "minimum_seconds_between_sends", "message_1_delay_days", "message_2_delay_days",
    "read_attempts", "contact_retry_limit", "max_consecutive_failures",
    "retry_initial_seconds", "retry_max_seconds",
)
ALIASES = {"firstname": "first_name", "companyname": "company", "location": "location",
           "first_name": "first_name", "company": "company"}


def template(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Message templates must be nonempty strings")
    def replace(match):
        key = match.group(1).strip().casefold()
        if key not in ALIASES:
            raise ValueError(f"Unknown message placeholder: {match.group(0)}")
        return "{" + ALIASES[key] + "}"
    value = re.sub(r"\{\{([^{}]+)\}\}", replace, value)
    for _, field, spec, conversion in Formatter().parse(value):
        if field is not None and (field not in {"first_name", "company", "location"} or spec or conversion):
            raise ValueError(f"Unsupported placeholder {{{field}}}; use first_name, company or location")
    value.format(first_name="Alex", company="Example", location="Boston")
    return value


def read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}, line {exc.lineno}: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def resolve_path(value, base):
    path = Path(value).expanduser()
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def load_settings(path=None):
    path = Path(path or Path.cwd() / "settings.json").expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Settings not found: {path}. Run 'linkedin-outreach init' first.")
    supplied = read_json(path)
    unknown = set(supplied) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"Unknown settings: {', '.join(sorted(unknown))}")
    cfg = deepcopy(DEFAULTS)
    cfg.update(supplied)
    if cfg["message_templates_file"]:
        messages = read_json(resolve_path(cfg["message_templates_file"], path.parent))
        if set(messages) - set(TEMPLATE_KEYS):
            raise ValueError("Message file contains unknown keys; see the Message templates section of README.md")
        if not {"connection_note", "message_1", "message_2"} <= messages.keys():
            raise ValueError("Message file needs connection_note, message_1 and message_2")
        cfg.update(messages)
    for key in POSITIVE_INTS:
        if type(cfg[key]) is not int or cfg[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    total = cfg["campaign_invitation_limit"]
    if cfg["retry_initial_seconds"] > cfg["retry_max_seconds"]:
        raise ValueError("retry_initial_seconds must not exceed retry_max_seconds")
    if cfg["read_attempts"] > 5:
        raise ValueError("read_attempts must not exceed 5")
    if total is not None and (type(total) is not int or total < 1):
        raise ValueError("campaign_invitation_limit must be a positive integer or null for no total cap")
    if cfg["browser_channel"] not in ("brave", "msedge", "chrome", "chromium"):
        raise ValueError("browser_channel must be brave, msedge, chrome or chromium")
    if type(cfg["headless"]) is not bool:
        raise ValueError("headless must be true or false")
    ZoneInfo(cfg["timezone"])
    hours, days = cfg["working_hours"], cfg["working_days"]
    if not isinstance(hours, list) or len(hours) != 2 or not all(type(h) is int for h in hours) or not 0 <= hours[0] < hours[1] <= 24:
        raise ValueError("working_hours must be [start, end], within 0..24")
    if not isinstance(days, list) or not days or any(type(d) is not int or d not in range(7) for d in days):
        raise ValueError("working_days uses Monday=0 through Sunday=6")
    for key in ("csv_file", "state_directory"):
        if not isinstance(cfg[key], str) or not cfg[key].strip():
            raise ValueError(f"{key} must be a nonempty path")
    for key in ("browser_executable", "browser_profile_directory", "message_templates_file"):
        if cfg[key] is not None and (not isinstance(cfg[key], str) or not cfg[key].strip()):
            raise ValueError(f"{key} must be a nonempty path or null")
    if not isinstance(cfg["company_filter"], str):
        raise ValueError("company_filter must be a string")
    for key in TEMPLATE_KEYS:
        cfg[key] = template(cfg[key])
    cfg["_settings_path"] = str(path)
    cfg["_workspace"] = str(path.parent)
    return cfg


def state_path(cfg):
    return resolve_path(cfg["state_directory"], Path(cfg["_workspace"]))
