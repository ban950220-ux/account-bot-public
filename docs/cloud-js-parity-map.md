# Native Worker parity map (offline)

Source of truth is the Python `account_bot` runtime. The JavaScript modules in
`deploy/cloudflare-js/` are an offline candidate, not a deployed bot. A checked
box means only the stated synthetic boundary was compared with Python.

| Python operating path | JavaScript candidate | Current result |
|---|---|---|
| `telegram_ui.auth` and PTB `register_handlers` | `boundary.mjs`, `command-router.mjs`, `operating-worker.mjs` | All 12 registered English text commands route through synthetic injected providers and D1-shaped storage. The Worker candidate checks secret header, owner and private chat before D1; mutation revision and exact reply are staged in one D1 batch. Chunked read-only replies, one-attempt Telegram transport and `SENDING`/`UNKNOWN` no-retry semantics are mock/local-workerd tested. Real token, provider gateway and remote webhook remain unconnected. Callback payloads are ignored. |
| `/start`, `/help` | `static-commands.mjs` | Exact static replies checked against Python; webhook command dispatch and Telegram send remain. |
| `/assets`, `/portfolio` and scoped `_refresh` | `provider-contracts.mjs`, `aggregation.mjs`, `assets-report.mjs`, `portfolio-report.mjs` | Synthetic main/all source selection, partial provider results, merge, ratios and HTML formatters checked. Multi-source titles are made explicit and unsafe account display labels are suppressed. Python defines an `accounts` method but does not register `/accounts`; it is not a live command. Actual allowlisted read-only broker adapters remain. |
| `/recurring`, `/recurring_add`, `/recurring_update`, `/thesis` | `recurring-report.mjs`, `cloud-write.mjs`, `cloud-gateway.mjs`, `command-router.mjs` | No-analysis and one analysis-bearing synthetic recurring report match Python. D1 create/replay, per-plan version CAS, explicit source/account-scoped transactional group edit, replay and Thesis append/current history tested with synthetic rows through the router. Two staged D1 sources use the Python local/snapshot minimum amounts and retain distinct revisions. Changing command revisions and replies now share one D1 batch; actual Meritz snapshot import, source-to-account mapping and remote D1 remain. |
| `/stock`, `/analyze`, `/rebalance`, `/weekly` | `dynamic-commands.mjs`, `scoring.mjs`, `analysis-service.mjs`, `analysis-report.mjs`, `rebalance.mjs`, `periodic-report.mjs` | Mock `/stock`, deterministic valuation components, no-data `/analyze`, rich analysis HTML and synthetic `/rebalance` match Python; their router paths return replies with injected providers. Nonempty daily/weekly reports match Python with and without synthetic analysis. Live provider/cache adapters, broader analysis fixtures and production data boundaries remain. |
| `PortfolioAggregator` and `format_assets` | `aggregation.mjs`, `assets-report.mjs` | Exact subtotal, positions, direct/inverse FX, incompleteness, weights and assets HTML checked on synthetic cases. All broker edge cases remain. |
| `evaluate_recurring_buy` and transaction repository | `finance.mjs` narrow reconciliation, `storage.mjs` transaction claim | Synthetic status, account/source filtering and unique ledger key checked. Full original schema migration and currency-conversion judgment remain. |
| PTB daily, Saturday valuation, weekly report | `boundary.mjs`, `periodic-report.mjs`, `operating-worker.mjs` | UTC/local time, daily and weekly report composition, scheduled claim/outbox, duplicate tick and uncertain-send behavior are tested with synthetic data. Python parity covers nonempty reports with and without analysis; missing FX budget is withheld. Saturday valuation cache refresh needs a real cache/provider adapter and remains fail-closed. Remote Cron, persistence migration and missed-job behavior remain. |
| report claim, delivery, `UNKNOWN` | `outbox.mjs`, `telegram-transport.mjs`, `operating-worker.mjs`, `synthetic-harness.mjs` | Synthetic SQLite batch behavior and isolated local workerd/D1 scheduled-only and mock-delivery paths checked. Read-only reply/report chunks are claimed in one batch; confirmed send is marked `SENT`, ambiguous outcomes are not retried. Remote D1, actual Telegram, content hashes, fingerprints and history import remain. |

`storage.mjs` and the additive `cloud_*` tables are a **staging interface**.
They preserve synthetic plan revisions including instrument and timing changes,
source-scoped ledger keys and snapshot failure states. A complete snapshot
requires every declared source to succeed and base valuations to reconcile.
They do not yet contain every field of the original SQLite
portfolio, plan, transaction, snapshot, thesis, cache and report schema. Do
not import a real backup or run the current JS command path on operating data.

## Editing controls

The Python Telegram runtime registers text commands, not an inline button
editing menu. Its `callback_query` update type is allowed at polling, but there
is no callback handler for editing. The JavaScript candidate likewise ignores
callbacks and has no edit-screen state, preview, save button or delete command.
The tested command-only paths include add and group replay keys, per-plan
version CAS, stale-version conflict, account scope guard, stop without history
deletion, invalid amounts, missing FX withholding, denial before a gateway
call, cancellation before mutation and generic DB failure. A synthetic
router/gateway test exercises the group command, and a D1-shaped transaction
test rolls back a late stale version. Group editing staged D1 rows from an
explicit Meritz source scope is tested; importing a real snapshot and its
account mapping remain unapproved. Python registers no delete command, so
none was added to the cloud candidate. No cloud editing menu is
verified. Stale-screen save and pre-save screen cancellation do not apply to
the current command-only flow, and an in-flight mutation cannot be canceled
after its transaction starts. No Windows UI, PC process or operating account
setting was changed.

## Gates before any operating cutover

1. Build source-attributed D1 schema/import for plans, revisions, broker
   snapshots, ledger, market cache and report history. Preserve Meritz as a
   separate source. Compare counts, keys, versions and scoped totals against a
   frozen private backup only after data-transfer approval.
2. Port all commands, broker/market read adapters, analysis, position merge,
   exact Decimal division/rounding and HTML reports. Run cross-runtime golden
   fixtures for every command and partial/failure condition. Never display a
   partial subtotal as a complete total or infer a missing FX rate.
3. Test the complete Worker and D1 paths with synthetic data in an approved
   Free test resource. Local Node/SQLite tests do not establish D1 behavior or
   the Free HTTP CPU budget.
4. Obtain separate approvals for credential provision, private data transfer,
   webhook registration and the controlled Windows-to-Worker transition.

No feature is intentionally dropped. Until every gate passes, Windows remains
the operating bot.

## Smallest remote test gate

An existing Cloudflare connection must expose read-only account identity, Workers
Free plan, current Workers/D1 usage and remaining limits. The current CLI session
reports `authRequired`, so the account and remaining free capacity cannot be
verified here. Do not create or persist a token merely to run this test.

After that read-only check, a separate synthetic Worker and D1 database can be
approved for a bounded Free-only rehearsal. Use a distinct name and binding,
synthetic rows and a mock outbound transport. Verify claim concurrency, D1
transactions, reports and observed Worker CPU in the actual runtime; remove the
test resources afterward. Free usage must fail closed at its caps. This test
neither imports a financial backup nor registers a real Telegram webhook.
