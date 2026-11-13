# Native JavaScript Worker candidate

This is a credential-free, offline comparison with the existing Python bot.
It has a fail-closed Worker entrypoint and mock-tested Telegram transport,
but no connected broker calls, operating data import, or cloud deployment.
The Windows bot remains the only operating bot.

`finance.mjs` uses decimal strings and `BigInt` for exact addition,
multiplication, weekly budget, display rounding, and a narrow recurring-fill
reconciliation. `aggregation.mjs` checks source coverage, compatible listing
merges, position weights, and exact included subtotals while withholding an
incomplete total. Direct and inverse FX are checked with synthetic fixtures.
`static-commands.mjs` has
the exact `/start` and `/help` replies. `assets-report.mjs` and
`portfolio-report.mjs` match synthetic Python output; `periodic-report.mjs`
also matches synthetic plan reports with and without injected analysis.
`scoring.mjs` compares the deterministic valuation component and decision path
with Python; `analysis-service.mjs` composes injected read-only results and
`analysis-report.mjs` compares full HTML against synthetic Python cases.
`provider-contracts.mjs` exercises selected read-only source and market-price
contracts using mocks, including partial failures. `dynamic-commands.mjs`
matches `/stock` replies with synthetic quotes. No live provider HTTP is
implemented. `command-router.mjs` routes registered English/Korean text
commands after owner admission and returns HTML without sending it.
`operating-worker.mjs` adds a fail-closed fetch/scheduled boundary with
owner/chat/header checks, D1 outbox delivery claims and one-attempt Telegram
transport. Its default gateway intentionally refuses operation until an
approved real read-only provider/data adapter is connected. No credential is
embedded. A changing command's plan/Thesis revision and exact response are
staged in one D1 batch; read-only replies and scheduled reports use bounded
chunks. Ambiguous sends stay `UNKNOWN` or `SENDING` and are never auto-retried.
`boundary.mjs` validates a private,
allowlisted Telegram update and maps UTC ticks to configured local report
times. `storage.mjs` stages synthetic account fingerprints, plan revisions,
transaction keys and source-labelled snapshots in D1-shaped tables. It is not
a complete operating-database importer. `cloud-write.mjs` adds replay-safe
single-plan and Thesis revisions with optimistic version checks. The
transactional group-write primitive is wired to the synthetic D1 gateway and
tested with account scope, replay and revision history; a production
gateway/outbox transaction remains pending. `outbox.mjs`
stages update/report claims in a D1-style transactional batch and preserves
uncertain sends as `UNKNOWN`. The same additive schema is in
`../cloudflare/schema.sql`.

`synthetic-harness.mjs` is a bounded D1 binding rehearsal. Its HTTP handler is
read-only (`GET /health` exposes only `NOT_RUN`, `PASS`, `FAIL`, or `RUNNING`)
and every other path returns 404. Only Cloudflare's `scheduled` event can write.
That event requires the plain `TEST_WRITES_ENABLED=1` binding, claims the fixed
`synthetic-v2` run key once, writes fixed fixture rows, and verifies concurrent
update claiming, ledger idempotency and `UNKNOWN` delivery without any send.
It also checks fixed synthetic plan and Thesis revisions.
Once the test completes, set `TEST_WRITES_ENABLED=0` and remove any Cron trigger.
Do not expose a public write trigger or add real identifiers, SQL or input data.
The harness uses no persistent credential; remote use waits for the separately
approved test Worker/D1 and a current Free usage check. A dashboard scheduled
event trigger must be confirmed before remote use; otherwise leave the Worker
untriggered rather than adding a public POST endpoint.

Offline checks:

```text
node --test tests/cloud_js_*.test.mjs
.venv/bin/python -m pytest -q
```

The Python parity test uses synthetic fixtures and checks exact weekly budget,
rounding, FX, schedule, webhook admission, source/account filtering, partial
portfolio subtotals, fill classification, and static command text. The
D1-shaped tests use Node's disposable in-memory SQLite. A separate isolated
local Wrangler 4.153.0/workerd probe also exercised two concurrent synthetic
requests against local D1: the first claimed the update and the second did
not; both observed one `UNKNOWN` outbox row. Neither test proves remote D1
concurrency or Workers Free CPU use.

The scheduled-only `synthetic-v2` harness also ran on isolated local
workerd/D1 with a fixed fixture including a two-plan atomic group edit:
POST `/run` returned 404, the first scheduled invocation returned `PASS`,
and replay kept `PASS`. Local HTTP wall times were not CPU measurements.

Neither the Python Telegram bot nor this candidate has a button editing menu.
Callback payloads do not mutate plans. The command-only add, group-change and
Thesis interfaces must not be presented as a verified editing UI. In
particular, account-scoped multi-plan change is tested only as a command-only
D1-shaped flow. Pre-save screen cancellation and stale-screen save are not
implemented as UI flows. Python has no delete command. Staged D1 rows from
an explicitly included second source can join a group edit; actual Meritz
snapshot import and its account mapping remain unverified.

Before this can replace polling, the full command and report path, all broker
and market read adapters, source-attributed holdings and Meritz snapshots,
historical plans/revisions/ledger, and report delivery history need porting and
cross-runtime parity tests. KRW-funded USD instrument fill reconciliation must
remain unavailable until a verified conversion is present. Real D1 and Worker
tests, Free quota/CPU profiling, approved private data transfer and a controlled
single-bot cutover are separate gates. Missing inputs must remain unavailable,
never zero-valued or assumed complete.

Input-path audit (offline, synthetic only): the Python composition root uses
Kiwoom and Toss read-only REST when their respective credentials exist, and
falls back to local JSON snapshots when configured. Meritz holdings are only a
local normalized JSON snapshot; its recurring-plan snapshot is a separate
local file. A one-time copy cannot keep either Meritz file current. Kiwoom's
domestic execution endpoint supplies only same-day trades, so historical
ledger continuity requires repeated collection and durable writes. Python's
market provider uses yfinance and Yahoo chart hosts; it cannot be imported
unchanged into the native JavaScript Worker. `rest-normalizers.mjs` now maps
synthetic Kiwoom/Toss holdings and cash payloads to exact decimal strings,
and `market-cache.mjs` persists normalized public observations in D1 with
explicit stale status. These are offline components, not connected broker
transports or a live market feed. They do not establish remote Workers
execution, quota suitability, or PC-independent operation.

The current Windows configuration was checked separately without reading
credential values: both Kiwoom sources select REST, Toss is unconfigured, and
Meritz account and recurring inputs are separate manually maintained JSON
files. `manual-input.mjs` now has a strict synthetic Meritz JSON validator,
append-only D1 revision store, compare-and-set upload semantics, explicit
source timestamp versus upload timestamp, and a bounded handler that requires
an injected owner authentication decision before reading the body. The handler
is deliberately not mounted on the operating Worker: no verified owner-only
Cloudflare authentication or approved private file transfer exists yet.
Publishing a URL without that authentication would expose a financial-data
write path. Historical documents remain queryable by revision in D1; the
latest account snapshot is marked `STALE` after three days based on its data
time, even if uploaded more recently. No automatic Meritz brokerage feed is
claimed, and the current unconfigured Toss source is not a migration gate.

Web UI scope audit: `web/src/App.tsx` contains a React/Vite dashboard with
amount and ON/OFF edits, zero-holding ETF addition, scenario preview/save,
comparison and deletion. It calls the localhost FastAPI routes in
`account_bot.web_api.app` through Vite's `127.0.0.1:8000` proxy. Repository
`ARCHITECTURE.md` and `PROJECT_CONTEXT.md` say this is a local reference;
the owner-only hosted Portflow Site has a separate source and D1 boundary.
The localhost `/api/portfolio` read uses the latest READY Excel import rows
plus `web-dca:US:` recurring-plan overrides from its configured web SQLite DB;
it is not a direct read of the Telegram bot's broker execution ledger.
No deployment configuration here connects this React bundle to that Site or
to the Telegram Worker candidate. Therefore the Worker candidate does not
preserve these browser features. A future decision to migrate the local web
surface would require owner authentication, the portfolio/import/market and
projection API contracts, scenario storage, and a browser deployment path.

`tests/test_web_edit_contract.py` exercises the local API with a disposable
SQLite DB and no market provider: preview has no write, identical repeated
plan saves retain a version, zero disables an existing plan but cannot create
one, ETF add persists, scenario save/compare/delete work, and a duplicate
scenario name reuses the same row. It also records a current limitation:
an old browser state can overwrite a newer plan because the PUT request has
no expected version. The React amount save button has no in-flight lock;
the local backend deduplicates identical payloads, but changed concurrent
payloads need an explicit version precondition. The draft has no explicit
cancel button; scenario delete uses a browser confirmation. These behaviors
are local-reference findings, not proof about the separately hosted Site.

## Allocation policy and proposal commands

`allocation-policy.mjs` is a pure calculation over enabled recurring plans and
an owner-defined policy: daily budget, exposure-group caps, a per-ticker cap,
optional group targets, pinned tickers, step and minimum. `policy-store.mjs`
keeps the policy in `cloud_policy` with append-only `cloud_policy_revisions`
(additive tables at the end of `../cloudflare/schema.sql`).

- `/목표` (`/policy`) shows or edits the policy.
- `/추천` (`/recommend`) shows a deterministic proposal and an 8-hex token.
- `/추천적용 <token>` (`/recommend_apply`) recomputes the proposal and applies it
  through the existing atomic group write only if the token still matches the
  policy version, plan versions and amounts. Nothing is applied automatically.

Reductions resolve ticker caps, then group caps, then the budget. Spare budget
is spent only toward owner-set group targets. If the rules cannot all be met
within pinned tickers and minimum amounts, no proposal is produced. With no
policy configured every existing reply is unchanged. `/모으기` appends a short
violation notice once a policy exists; scheduled reports are not changed.
Each plan currently maps to one exposure group; the calculation already accepts
weighted multi-group input for a later ETF look-through table. The Worker reads
a missing `cloud_policy` table as "not installed", so the tables must be created
on the target D1 before the commands can save anything.
