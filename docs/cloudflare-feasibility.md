# Account bot Cloudflare feasibility

This is an offline design assessment. No Worker, D1 database, webhook, Cron
trigger, secret or operational data has been created or transferred. The
intended account is on Workers Free; available quota and runtime compatibility
still need validation. Do not upgrade to Workers Paid or a paid VM to bypass
these limits.

Cloudflare Workers Free offers 100,000 requests/day and 10 ms CPU per HTTP
request. Cron invocations below a one-hour interval have 10 ms CPU; triggers
at one hour or more have a longer CPU allowance. The account allows five Cron
triggers. D1 Free has 500 MB per database, 5 GB total storage, 5 million rows
read/day and 100,000 rows written/day. Free overages fail instead of silently
becoming paid usage. Actual CPU use is unverified. Sources:
[Workers limits](https://developers.cloudflare.com/workers/platform/limits/),
[D1 limits](https://developers.cloudflare.com/d1/platform/limits/),
[D1 pricing](https://developers.cloudflare.com/d1/platform/pricing/).

| Existing behavior | Workers implementation required |
|---|---|
| PTB `run_polling` plus `Application` handlers | HTTPS webhook, secret-header/private-chat admission, command dispatch and Telegram response transport per request. The offline `cloud_webhook` helper covers only admission. |
| PTB JobQueue daily/weekly report and valuation | UTC Cron triggers mapped to the configured Asia/Seoul report times. Reuse pure report calculations where possible, with durable claim before send. |
| SQLAlchemy + aiosqlite file and OS lock | D1 binding and query/transaction adapter, schema import, atomic update/report claims. File locks and local SQLite are inapplicable. |
| Meritz holdings and recurring JSON files | A durable private storage source or D1 representation, with separate source attribution and freshness. Workers' file system is temporary. |
| Kiwoom/Toss read-only HTTP, Yahoo/yfinance, report analysis | Worker-compatible HTTP and dependency evaluation. Large analyses may need partitioning; 10 ms webhook CPU must be measured on the actual Worker runtime. |

## Offline proof of the narrow boundaries

`cloud_webhook.py` admits only a valid Telegram secret-header request from an
allowlisted private chat. `cloud_d1.py` defines an additive D1 binding adapter:
one transactional batch claims a Telegram update or report period and stages
outbox rows. A per-invocation nonce prevents a duplicate claim from adding
new recipients. A conflicting outbox key aborts the claim batch instead of
leaving a newly claimed event without a staged message. A conditional
transition reserves a pending send, then records
`SENT` or `UNKNOWN`. A crash after reservation leaves `SENDING` for manual
reconciliation; the code never automatically resends that state. These tables
are defined in `deploy/cloudflare/schema.sql` and tested against a disposable
SQLite implementation of the D1 batch contract. No operating DB import or
Telegram transport is included.

`cloud_schedule.py` interprets Cloudflare's scheduled UTC tick in the configured
local timezone. For the documented Asia/Seoul defaults, three Cron expressions
would be `0 23 * * SUN-THU` (weekday 08:00), `30 23 * * FRI`
(Saturday 08:30 valuation) and `0 0 * * SAT` (Saturday 09:00 report). Each
is at least daily, within the five-trigger Free allowance. A different
configured timezone/time requires recalculating and testing the expressions;
the current code does not configure a Worker. Polling's report runner now uses
the same pure `build_periodic_report` dispatch a future Cron adapter would use.

`scripts/cloud/measure_cpu.py` profiles only synthetic admission and report
composition on local CPython. It excludes D1 calls, broker refresh, Telegram
HTTP, Pyodide and Worker startup, so sub-10ms local values do **not** establish
that a deployed webhook fits Workers Free. The official limits grant 10 ms CPU
to Free HTTP requests and longer CPU to Cron triggers at intervals of at least
one hour. Actual Worker-runtime profiling is a gate before rollout.

An isolated local `pywrangler dev` probe with synthetic imports was attempted
without credentials or cloud resources. The tool downloaded the Pyodide
runtime, but dependency resolution for `workers-runtime-sdk` from the official
Pyodide package index failed with a network tunnel error before a Worker
started. A minimal dependency configuration produced the same error. No local
Worker request, D1 binding test or CPU measurement was obtained. This is an
environment/package-index blocker, not evidence of code compatibility.

This proof is intentionally incomplete: the existing SQLAlchemy repository,
Meritz snapshot source and broker/market adapters are not yet ported to D1 or
Worker-compatible storage/HTTP. Existing holdings, plans, ledger and report
delivery rows cannot be read through the new adapter. The cloud code must not
be exposed as an operational bot until those data and command paths have full
parity and source-scope regression tests.

Python Workers use Pyodide. Its standard library excludes `fcntl` and `msvcrt`,
threads do not function, and files are ephemeral; Python code cannot simply
run with the present `runtime_lock`, `aiosqlite` and PTB polling lifecycle.
There is a [Python D1 binding](https://developers.cloudflare.com/d1/examples/query-d1-from-python-workers/),
but it is a distinct API from SQLAlchemy's file-backed connection. See the
[Python standard-library restrictions](https://developers.cloudflare.com/workers/languages/python/stdlib/).

## Minimum implementation gates

1. Keep the current Windows bot active. Implement a credential-free local
   webhook test harness. Reject missing/wrong Telegram secret headers, empty
   allowlist, groups, mismatched chat/user IDs and malformed updates.
2. Implement and test a D1 adapter with an atomic unique `update_id` claim,
   and equivalent recurring plans, source attribution, report claims and
   `UNKNOWN` delivery behavior. Import from a frozen SQLite backup and both
   Meritz snapshots only after explicit data-transfer authorization. Compare
   record counts and source scopes; do not merge Meritz amounts into the bot DB.
3. Run Python/TypeScript package compatibility tests in a local Worker runtime
   with synthetic data. Profile actual CPU on webhook and scheduled paths;
   CPython timings cannot prove that the 10 ms Pyodide budget is met. Reject
   a Free rollout if any required command exceeds a hard limit.
4. Confirm remaining Free quota, webhook hostname, secret storage,
   data-transfer method and cutover approval. Register the webhook only after
   Windows polling has gracefully stopped and the D1 import has been verified.
   Preserve the original backup and a rollback path. Never run both modes with
   one Telegram token or automatically replay uncertain report deliveries.

Until these gates pass, the Workers architecture is a candidate, not a usable
cloud deployment. A paid host or paid Workers tier is outside this effort.
