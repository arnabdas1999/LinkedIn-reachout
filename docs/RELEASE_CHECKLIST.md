# Beta release evidence

Keep this checklist with a release candidate. Code coverage of an operation and evidence from a real installation are separate requirements. Do not describe a candidate as production verified until its applicable evidence gates pass.

## Implemented checks

- Send guards recheck hours, daily/rolling/total allowances and spacing at the actual UI action. Tests include midnight, closing time and the repeated DST hour.
- A durable journal is committed before clicks. A test terminates an owned Python child after that barrier, then verifies recovery holds the recipient without a resend.
- Local contact faults, transient browser recovery and account/fatal stops have separate behavior. A supervisor preserves explicit pauses and blocked status, including within the same second as a start request.
- Health snapshots distinguish a living process from campaign progress. Deduplicated local alerts cover blocked, stalled and repeatedly recovering work; Windows desktop delivery is available.
- Private daily reports separate confirmed sends from reserved attempts. They record known constraints without inferring an unsupported cause for a shortfall.
- Schema upgrades back up existing state. Restore validates snapshots, rejects lost send history/different campaign identity, preserves damaged files and requires an explicit audit acknowledgment.
- Contact recovery refuses a possible send until explicit reconciliation. State migration preserves the old copy and the settings backup.
- The clean-wheel smoke check installs into a fresh environment and verifies offline setup, detached start/stop, recovery after terminating its owned fixture worker, and a retained explicit pause, with zero action rows.
- CI defines Windows/macOS/Linux with Python 3.11/3.13, local browser fixtures, package checks and clean-wheel smoke checks. A configured workflow is not evidence of a hosted pass.

## Evidence required for a candidate

Record the commit, operating system, Python/browser version and outcome privately where needed. Contact data and sessions must not enter release artifacts.

- [ ] Lint, full core tests and intercepted browser tests pass for the candidate.
- [ ] Wheel and source package build; release privacy checker passes for tracked history and all published archives.
- [ ] Fresh wheel setup/start/stop smoke check passes on the local release machine.
- [ ] Hosted CI passes every supported matrix job on the candidate commit.
- [ ] A new user can follow only the README on a clean Windows machine; initial login is visible and routine runs stay hidden.
- [ ] Windows logon registration is inspected and survives a real OS sign-out/login or reboot. Paused campaigns remain paused; running campaigns resume after login.
- [ ] Desktop notification request is tested with OS notification settings enabled. If display is suppressed, health files/local alerts remain usable.
- [ ] Manual login expiry is repaired and explicitly resumed; account challenges stay paused before that action.
- [ ] A real acceptance is observed, Message 1 becomes due after its configured delay, Message 2 becomes due after Message 1, and a recipient reply stops the sequence. Use consenting test recipients when possible.
- [ ] A backup is restored in an isolated campaign, independently audited, acknowledged and resumed without duplicate actions.
- [ ] At least 14 consecutive days of the release candidate are observed: daily reports, actual counts, held contacts, faults and recovery are reviewed; no unexplained duplicate, wrong-recipient or unacknowledged uncertain resend occurs.

If a candidate changes during the live observation, record which evidence still applies and repeat the affected checks. No fixed daily quota can guarantee delivery when the account is blocked, the computer is unavailable, contacts are ineligible or outcomes need review.

## Known operating limits

Windows login startup is supplied; macOS/Linux startup registration is left to OS tooling. The computer must be awake, online and signed into the OS. The supervisor avoids force-killing a stalled send. Browser layouts and account-specific behavior can change. Backups contain personal data and do not replace independent review after a damaged/lost database. The app remains subject to LinkedIn's platform restrictions described in the README.
