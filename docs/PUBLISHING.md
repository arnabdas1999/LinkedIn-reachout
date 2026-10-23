# Publish to GitHub

Publish the application source and documentation. Keep spreadsheets, the `examples/` folder, working contacts, personal templates, settings and `.local/` campaign state local.

## Before publishing

1. Run `backup` to preserve a consistent private campaign snapshot. Keep it outside the release.
2. Run the core and intercepted browser tests, lint, `python -m build`, and `python scripts/smoke_release.py`.
3. Review `git status --short` and `git diff --cached --stat`. If this folder is new, initialize it with `git init -b main` and stage the reviewed app files. Do not force-add ignored files.
4. Run `python scripts/check_release.py --dist --history`. Inspect the staged contents and package contents; the checker detects excluded file paths/types, not every possible secret embedded in source text. `.gitignore` does not remove files already tracked or present in older commits. Remove those paths from unpublished history before the first push.
5. Confirm the public README has no personal paths, contact details or campaign-specific configuration.

## Create the repository

Choose a repository name and public/private visibility. With the GitHub CLI signed into the intended account:

```bash
gh auth status
git commit -m "Prepare reusable local outreach app"
gh repo create linkedin-outreach-local --private --source . --remote origin --push
```

Use `--public` only when you intend the reviewed contents to be public. The example command creates a private repository. If a repository already exists, inspect its remote/history and follow its contribution workflow instead of replacing it.

Enable Actions, require the CI workflow for changes to `main`, and enable private vulnerability reporting. CI runs fixture tests, builds packages and tests a fresh wheel installation; it never uses a LinkedIn session. Review the first hosted CI run and the [release evidence checklist](RELEASE_CHECKLIST.md) before calling the cross-platform release verified.

## Release

After CI passes, tag a version consistent with `pyproject.toml` and `outreach_config.VERSION`. Create a GitHub Release, and attach only the reviewed `dist/*.whl` and `dist/*.tar.gz` files, or let users download GitHub's source ZIP. Do not zip the working folder: it contains private ignored files.

The download still requires Python and the documented setup; this is not a standalone Windows executable. Browser profile/session data is never part of a release. No PyPI publishing or GitHub release upload is performed automatically.
