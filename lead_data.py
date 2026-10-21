"""Repair source data before personalizing outreach; never change campaign history."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import io
from pathlib import Path
import re
import shutil
from urllib.parse import unquote, urlsplit


SOURCE_DATA_REASON = "Missing first name or company in source CSV"
SOURCE_DATA_REASONS = (SOURCE_DATA_REASON, "Missing first name or company; review this lead")
COLUMN_ALIASES = {
    "First Name": ("first name", "firstname", "given name"),
    "Last Name": ("last name", "lastname", "surname", "family name"),
    "Full Name": ("full name", "fullname", "name"),
    "Job Title": ("job title", "title", "position"),
    "Company": ("company", "company name", "organization"),
    "City": ("city",), "State or Province": ("state or province", "state", "region", "province"),
    "Country": ("country",),
    "LinkedIn Profile": ("linkedin profile", "linkedin url", "linkedin profile url", "linkedin", "profile url"),
}


def canonical_column(value):
    token = re.sub(r"[^a-z0-9]", "", clean_text(value).casefold())
    for column, aliases in COLUMN_ALIASES.items():
        if token in {re.sub(r"[^a-z0-9]", "", alias) for alias in aliases}:
            return column
    return clean_text(value)


def clean_text(value):
    return " ".join((value or "").replace("\ufeff", "").replace("\u200b", "").split())


def canonical_profile(value):
    parts = urlsplit(clean_text(value))
    if parts.scheme not in ("http", "https") or parts.hostname not in (
        "linkedin.com", "www.linkedin.com"
    ) or parts.username or parts.password or parts.port:
        raise ValueError("Expected a public linkedin.com/in/ profile URL")
    path = parts.path.strip("/").split("/")
    if len(path) != 2 or path[0] != "in" or not path[1]:
        raise ValueError("Expected a public linkedin.com/in/ profile URL")
    if any(c in unquote(path[1]) for c in "/\\?#"):
        raise ValueError("Invalid LinkedIn profile slug")
    return "https://www.linkedin.com/in/" + path[1].casefold() + "/"


def name_parts(full_name):
    # Keep Full Name intact for profile identity checks. Ignore annotations only
    # when deriving missing name components; Guru/Wing/King/Deacon are names.
    name = re.sub(r"\([^)]*\)", " ", clean_text(full_name)).split(",", 1)[0]
    name = re.sub(r"^(?:(?:dr|mr|mrs|ms|mx|prof)\.?\s+)+", "", name, flags=re.I)
    return [part for part in name.split() if any(c.isalpha() for c in part)]


def repair_contact(source):
    row = {key: clean_text(value) for key, value in source.items()}
    parts = name_parts(row.get("Full Name", ""))
    first = row.get("First Name", "")
    last = row.get("Last Name", "")
    if not first and parts:
        row["First Name"] = first = parts[0]
        if len(parts) == 1 and last.casefold() == first.casefold():
            row["Last Name"] = last = ""
    if not last and len(parts) > 1:
        if first.casefold() == parts[0].casefold():
            row["Last Name"] = " ".join(parts[1:])
        elif first.casefold() == parts[-1].casefold():
            # A title parser sometimes consumes the given name (e.g. King Lam).
            row["First Name"], row["Last Name"] = parts[0], " ".join(parts[1:])
    if not row.get("Full Name"):
        row["Full Name"] = clean_text(row.get("First Name", "") + " " + row.get("Last Name", ""))
    country = row.get("Country", "").casefold()
    region = row.get("State or Province", "").casefold()
    if country and row.get("City", "").casefold() == country:
        row["City"] = ""
    if country and region == country:
        row["State or Province"] = ""
    try:
        row["LinkedIn Profile"] = canonical_profile(row.get("LinkedIn Profile", ""))
    except ValueError:
        pass  # Preserve an unrepairable URL for review; never invent an identity.
    return row


def read_clean_csv(path):
    """Return repaired unique rows and an auditable report, without writing."""
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        sample = stream.read(8192)
        stream.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(stream, dialect=dialect)
        raw_headers = reader.fieldnames or []
        headers = [canonical_column(key) for key in raw_headers]
        required = {"Company", "LinkedIn Profile"}
        if not required <= set(headers) or not {"Full Name", "First Name"}.intersection(headers) or len(headers) != len(set(headers)):
            raise ValueError("CSV needs Company, LinkedIn Profile, and Full Name (or First Name); header aliases must be distinct")
        reader.fieldnames = headers
        rows, by_url, changes, duplicates, conflicts, invalid = [], {}, [], [], [], []
        input_rows = 0
        for line, raw in enumerate(reader, 2):
            input_rows += 1
            if None in raw:
                raise ValueError(f"CSV row {line} has extra cells; check its quoting")
            original = {clean_text(key): value or "" for key, value in raw.items()}
            row = {key: clean_text(value) for key, value in original.items()}
            try:
                url = canonical_profile(row.get("LinkedIn Profile", ""))
                row["LinkedIn Profile"] = url
            except ValueError:
                invalid.append(line)
                url = None
            if url in by_url:
                target = by_url[url]
                for key, value in row.items():
                    if not target[key] and value:
                        target[key] = value
                    elif target[key] and value and target[key] != value:
                        conflicts.append({"row": line, "column": key, "kept": target[key], "other": value})
                duplicates.append({"row": line, "url": url})
                continue
            rows.append(row)
            if url:
                by_url[url] = row
            # Remember pre-repair values so reports include derived name fields.
            changes.append((line, original, row))
    for key in ("First Name", "Last Name", "Full Name"):
        if key not in headers:
            headers.insert(0 if key == "First Name" else 1, key)
    edits = []
    for line, original, row in changes:
        repaired = repair_contact(row)
        row.clear()
        row.update(repaired)
        for key in headers:
            row.setdefault(key, "")
            if original.get(key, "") != row[key]:
                edits.append({"row": line, "column": key, "before": original.get(key, ""), "after": row[key]})
    report = {"input_rows": input_rows, "output_rows": len(rows),
              "duplicates_removed": len(duplicates), "invalid_url_rows": invalid,
              "changes": edits, "duplicates": duplicates, "conflicts": conflicts}
    report["column_mapping"] = dict(zip(raw_headers, [canonical_column(key) for key in raw_headers]))
    return headers, rows, report


def csv_text(headers, rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=headers)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def clean_csv_file(path, backup_directory):
    """Idempotent cleanup for the standalone program, with an original backup."""
    path = Path(path)
    headers, rows, report = read_clean_csv(path)
    changed = bool(report["changes"] or report["duplicates_removed"])
    if changed:
        backup_directory = Path(backup_directory)
        backup_directory.mkdir(parents=True, exist_ok=True)
        suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = backup_directory / f"{path.stem}.{suffix}.csv"
        shutil.copy2(path, backup)
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            temporary.write_text(csv_text(headers, rows), encoding="utf-8-sig", newline="")
            try:
                temporary.replace(path)
            except PermissionError:
                # Some OneDrive/file-viewer handles permit writing but not
                # replacing the file. The original backup already exists.
                shutil.copyfile(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        report["backup"] = str(backup)
    return report
