# Security and privacy

Authentication lives in a dedicated local browser profile, not in repository settings. The app does not request a LinkedIn password, copy the normal browser profile, or provide a hosted backend. Browser session files are sensitive credentials: do not upload or share them.

CSV inputs, templates, SQLite state, cleanup reports, review-event snapshots, logs and screenshots may contain personal data. New setups put campaign state in local application data outside the repository; older `.local/` state and private settings/inputs are excluded by `.gitignore`. Custom state directories need the same protection. Run the release privacy checker before publishing; `.gitignore` alone cannot remove a previously tracked file.

If private information was committed, remove it from tracked content and repository history before publishing. If credentials were exposed, revoke the affected session and sign in again; deleting the file alone does not invalidate a copied session.

Report vulnerabilities using the repository's private security-reporting feature when enabled. Do not post session files, raw contact lists or unredacted logs in public issues. Maintainers should enable private vulnerability reporting before opening the repository to contributions.
