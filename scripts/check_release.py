"""Reject private runtime files in tracked sources and Python distributions."""
import argparse
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import zipfile


def private(path):
    parts = PurePosixPath(path).parts
    name = parts[-1] if parts else ""
    if any(part in {".local", ".venv", "profiles", "__pycache__", ".env", "examples",
                    "data", "input", "inputs", "output", "outputs", "backups", "downloads", "screenshots",
                    "dist", "build", ".auth", ".idea", ".vscode", ".ruff_cache", ".pytest_cache"} for part in parts):
        return True
    if any(part.startswith("browser-profile") for part in parts):
        return True
    if name in {"settings.json", "messages.json", "storage_state.json", "auth.json", "Cookies", "Login Data"} or name.startswith(".env"):
        return True
    if name.startswith("settings.") and name.endswith(".json") and name != "settings.example.json":
        return True
    if name.startswith("messages.") and name.endswith(".json") and name != "messages.example.json":
        return True
    if name.casefold().endswith((".csv", ".tsv", ".ods", ".ots", ".numbers")) or PurePosixPath(name.casefold()).suffix.startswith(".xl"):
        return True
    return any(token in name for token in (".sqlite", ".log")) or name.endswith((".db", ".png", ".jpg", ".har", ".pyc", ".bak", ".backup", ".tmp", "~"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", action="store_true")
    parser.add_argument("--history", action="store_true", help="Also inspect file paths in reachable commit history")
    args = parser.parse_args()
    paths = subprocess.check_output(["git", "ls-files", "-z"], text=True).split("\0")
    if not any(paths):
        raise SystemExit("No tracked files to check. Stage the reviewed source files first.")
    bad = [path for path in paths if path and private(path)]
    if args.history:
        history = subprocess.check_output(["git", "log", "--all", "--format=", "--name-only"], text=True)
        bad.extend(f"history: {path}" for path in sorted(set(history.splitlines())) if path and private(path))
    if args.dist:
        artifacts = list(Path("dist").glob("*.whl")) + list(Path("dist").glob("*.tar.gz")) + list(Path("dist").glob("*.zip"))
        if not artifacts:
            raise SystemExit("No distribution artifacts found. Run python -m build first.")
        for artifact in artifacts:
            if artifact.suffix in (".whl", ".zip"):
                with zipfile.ZipFile(artifact) as archive:
                    names = archive.namelist()
            else:
                with tarfile.open(artifact) as archive:
                    names = archive.getnames()
            bad.extend(f"{artifact.name}: {name}" for name in names if private(name))
    if bad:
        raise SystemExit("Private files must not be published:\n" + "\n".join(bad))
    print("Release privacy check passed: no private runtime files in the checked sources/artifacts.")


if __name__ == "__main__":
    main()
