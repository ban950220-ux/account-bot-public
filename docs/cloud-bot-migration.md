# Account bot cloud operating handoff

This is a VM fallback plan and an offline verification tool, not an active cloud
deployment. [Cloudflare Workers feasibility](cloudflare-feasibility.md) is also
under review as a free-first option. A VM implementation would use a single,
continuously running Linux host with persistent local storage. An
Oracle Cloud Always Free VM is a possible no-monthly-fee host if the owner has
an eligible account, capacity and sufficient always-free storage; it may be
reclaimed after sustained idleness. Validate host eligibility before creating
anything. The saved Codex cloud development environment is not the host.

The one Python process handles Telegram `run_polling` and PTB JobQueue daily
and weekly reports. A GitHub Actions scheduled job cannot substitute for
continuous interactive polling, and the free Render web tier can sleep while
idle and does not provide persistent disk for this SQLite workload. Do not run
two pollers with one token. The existing file lock only prevents duplicates on
one machine; cross-host exclusion requires the cutover procedure below.

## Inputs that must be approved before operating a VM

- Selected host and an account that permits a persistent VM/disk with no paid
  resources, plus an owner-approved cost cap if any resource can bill.
- Private deployment of the exact approved Git commit and a Linux service
  account with a persistent data directory. The [example unit](../deploy/account-bot.service.example)
  is not installed or enabled by this repository.
- Transfer of the existing SQLite **online backup**, normalized Meritz holdings
  JSON, Meritz recurring-plan JSON, and any configured Kiwoom/Toss snapshot
  fallback. These are private operational data; do not put them in Git or CI.
- Secure provision of the existing Telegram token, allowlist, fingerprint
  pepper, broker read-only credentials and configured paths. Do not rotate or
  print them as part of this handoff. Preserve the pepper to keep fingerprints
  and report idempotency keys stable.
- Explicit Windows stop and cloud start window. The Windows bot stays active
  until the cloud host, settings, data transfer, offline checks and rollback
  method have all been validated.

## Offline rehearsal and final cutover

1. Rehearse the process on disposable fixtures. Keep `systemctl` unit disabled.
   Build a Python 3.11+ virtual environment from the approved commit and run
   `scripts/cloud/validate.py check` without credentials or live network APIs.
2. On the Windows host, create a consistent SQLite online backup with Python's
   `sqlite3.Connection.backup`, never a raw copy of a live WAL database. Store
   it in a private staging area. Include both Meritz JSON files. The snapshot
   source remains distinct from the bot DB; do not merge amounts or create
   duplicate recurring plans. Copy the files only through an approved private
   channel. Keep the original DB and a separate verified rollback backup.
3. On the **frozen source backup**, create a manifest:

   ```text
   python scripts/cloud/verify_transfer.py create --db BACKUP.db --meritz snapshot.json --meritz-accumul recurring.json --manifest transfer.json
   ```

   On the target, verify the transferred files against that manifest using the
   same arguments with `verify` instead of `create`. The tool checks SQLite
   integrity and foreign keys, parses both JSON roots, and compares exact
   SHA-256/size of each file. It prints no account data. Keep the manifest
   private. If Kiwoom or Toss fallback snapshots are configured, include them
   with `--kiwoom` and `--toss` on both commands.
4. Before final cutover, validate target settings without exposing values:
   `python -m account_bot --env-file /etc/account-bot/account-bot.env doctor`.
   Confirm `DATABASE_URL`, data/log/report/cache directories and both Meritz
   paths target persistent local disk; check the installed commit, timezone,
   schedule, allowlist, source configuration and expected schema. Do not run
   `init-db` or a migration against the copied operating DB.
5. At the approved cutover window, use the existing Windows graceful stop
   command and confirm the OS lock is free and the state is `stopped`. Do not
   force-kill. Take a **new final online backup** after the stop, repeat the
   transfer and verification, then enable exactly one cloud service instance.
   Confirm the cloud `status` command reports the intended launch version and
   only then test a harmless read-only Telegram command with explicit approval.
   Wait for scheduled-delivery observation before declaring the move complete.
6. Keep the Windows process stopped while cloud polling. On rollback, stop the
   cloud process gracefully and confirm it exited before re-enabling Windows.
   Compare/restore the preserved DB as appropriate; never replay a report with
   `UNKNOWN` delivery automatically. A cloud DB that received new history
   cannot simply be discarded without reviewing that history.

The DB file transfer preserves source-attributed plans, holdings history,
transactions, report claims and delivery statuses byte-for-byte. Keep each
source's existing reporting scope. A separate recurring-plan snapshot must
not be added to the main portfolio total or written as duplicate DB plans.
