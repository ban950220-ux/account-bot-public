# Account-bot operating Worker boundary (offline candidate)

`deploy/cloudflare-js/operating-worker.mjs` is the fail-closed fetch/scheduled entrypoint candidate. It is not the synthetic test Worker and has not been deployed. Its default export deliberately refuses to construct an operating gateway until the approved read-only provider and migrated data adapter is connected. Tests inject a synthetic gateway and a mock Telegram transport; no live Telegram, brokerage or paid API call was made.

## Implemented and locally checked

- `POST /telegram/webhook` accepts only a bounded update with one correct Telegram secret header, an allowlisted user and that user's private chat. Invalid configuration or missing D1 binding returns 503; unauthorized updates are rejected before D1 access. Other paths are 404.
- Registered text commands use the existing router and calculation functions. Plan creation/change, group edit and Thesis revision stage the **exact response** with the update claim in the same D1 `batch()` transaction. Read-only responses use a bounded chunk claim batch.
- Delivery uses one Telegram send attempt per chunk. `PENDING` is claimed as `SENDING` first. Only a confirmed Bot API success with a message ID becomes `SENT`; timeout, ambiguous response or failed completion write remains `UNKNOWN` or `SENDING`. Duplicate updates, concurrent Worker invocations and Worker restarts do not automatically resend uncertain chunks. Any later chunk waits behind an uncertain earlier chunk.
- Daily and weekly scheduled reports use the existing schedule and report functions, claim one local period per report and stage bounded response chunks. Duplicate ticks do not send a second report. The Saturday valuation refresh calls the existing gateway interface, with instrument deduplication and at most four concurrent provider calls.
- Synthetic Node/SQLite tests cover authorization, exact mutation/reply rollback when outbox insertion fails, duplicate and concurrent updates, successful-send/failed-status uncertainty, ambiguous send, restart, scheduled duplicate and Unicode chunking. A local workerd+D1 wrapper also exercised initialization, a daily report tick, unauthorized request, changing command and replay with a mock transport.

## Required external connections and separate gates

1. Implement and verify the **real** read-only broker/market provider factory and persistent valuation cache. Preserve source-specific completeness and missing FX states; do not substitute empty mock results. The default Worker intentionally rejects requests until this adapter is supplied.
2. Privately migrate and reconcile account-scoped plan, revision, Meritz snapshot, ledger, snapshot and report history only after data-transfer approval. Assign the Meritz source fingerprint explicitly; do not infer it from ticker or aggregate amount.
3. Provision the real bot token, webhook secret and allowlisted user/chat settings through approved secret handling. Register the webhook and Cron only in a controlled single-bot transition after real Workers Free/D1 tests and CPU/quota checks. Keep the Windows bot running until that cutover is approved.
4. Establish a manual review process for `SENDING`/`UNKNOWN` outbox records. They are intentionally not retried automatically. Confirm delivery history and missed-job policy before operating use.

The separate `account-bot-cloud-test-20261027.mjs` artifact remains the **synthetic test bundle** built from earlier commit `d3765ce6c28bc17c49614920805de1875ffa6d55`. This operating candidate did not alter that frozen artifact or its SHA-256. No GitHub publish or remote deployment occurred in this coding step.
