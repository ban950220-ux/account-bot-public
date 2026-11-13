// Isolated Free Worker/D1 rehearsal. No webhook, broker, Telegram, arbitrary SQL or user input.
import { importAccount, recordTransactionOnce } from './storage.mjs';
import { beginSend, enqueueUpdate, finishSend } from './outbox.mjs';
import { appendThesis, applyGroupChanges, applyPlanChange, createPlanOnce } from './cloud-write.mjs';

const RUN_KEY = 'synthetic-v2';
const ACCOUNT = `fixture_${'c'.repeat(64)}`;
const DDL = [
  `CREATE TABLE IF NOT EXISTS cloud_test_runs (
    run_key TEXT PRIMARY KEY, status TEXT NOT NULL, completed_at TEXT)`,
  `CREATE TABLE IF NOT EXISTS cloud_accounts (
    account_fingerprint TEXT PRIMARY KEY, broker TEXT NOT NULL,
    masked_label TEXT, base_currency TEXT NOT NULL)`,
  `CREATE TABLE IF NOT EXISTS cloud_transactions (
    account_fingerprint TEXT NOT NULL REFERENCES cloud_accounts(account_fingerprint),
    source_key TEXT NOT NULL, source TEXT NOT NULL, market TEXT, ticker TEXT,
    asset_type TEXT, currency TEXT NOT NULL, transaction_type TEXT NOT NULL,
    status TEXT NOT NULL, traded_at TEXT NOT NULL, settled_at TEXT, quantity TEXT NOT NULL,
    unit_price TEXT, gross_amount TEXT, fee TEXT, tax TEXT, net_amount TEXT,
    PRIMARY KEY (account_fingerprint, source_key))`,
  `CREATE TABLE IF NOT EXISTS cloud_update_claims (
    update_id INTEGER PRIMARY KEY, received_at TEXT NOT NULL, claim_nonce TEXT NOT NULL)`,
  `CREATE TABLE IF NOT EXISTS cloud_report_claims (
    period_key TEXT PRIMARY KEY, claimed_at TEXT NOT NULL, claim_nonce TEXT NOT NULL)`,
  `CREATE TABLE IF NOT EXISTS cloud_outbox (
    event_key TEXT PRIMARY KEY, destination_key TEXT NOT NULL, payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING'
      CHECK (status IN ('PENDING', 'SENDING', 'SENT', 'UNKNOWN')),
    created_at TEXT NOT NULL, finished_at TEXT)`,
  `CREATE TABLE IF NOT EXISTS cloud_plans (
    plan_key TEXT PRIMARY KEY, account_fingerprint TEXT REFERENCES cloud_accounts(account_fingerprint),
    source TEXT NOT NULL, market TEXT NOT NULL, ticker TEXT NOT NULL,
    asset_type TEXT NOT NULL, instrument_currency TEXT NOT NULL,
    amount_currency TEXT NOT NULL, daily_amount TEXT NOT NULL, target_weight TEXT,
    enabled INTEGER NOT NULL, timezone_name TEXT NOT NULL, started_at TEXT NOT NULL,
    memo TEXT, version INTEGER NOT NULL, last_mutation_key TEXT)`,
  `CREATE TABLE IF NOT EXISTS cloud_plan_revisions (
    plan_key TEXT NOT NULL REFERENCES cloud_plans(plan_key), revision_no INTEGER NOT NULL,
    change_hash TEXT NOT NULL, source TEXT NOT NULL, account_fingerprint TEXT,
    market TEXT NOT NULL, ticker TEXT NOT NULL, asset_type TEXT NOT NULL,
    instrument_currency TEXT NOT NULL, enabled INTEGER NOT NULL, daily_amount TEXT NOT NULL,
    amount_currency TEXT NOT NULL, target_weight TEXT, timezone_name TEXT NOT NULL,
    started_at TEXT NOT NULL, effective_from TEXT NOT NULL, memo TEXT,
    mutation_key TEXT, PRIMARY KEY (plan_key, revision_no),
    UNIQUE (plan_key, mutation_key))`,
  `CREATE TABLE IF NOT EXISTS cloud_mutation_checks (
    mutation_key TEXT NOT NULL, plan_key TEXT NOT NULL,
    valid INTEGER NOT NULL CHECK (valid = 1),
    PRIMARY KEY (mutation_key, plan_key))`,
  `CREATE TABLE IF NOT EXISTS cloud_theses (
    instrument_key TEXT PRIMARY KEY, market TEXT NOT NULL, ticker TEXT NOT NULL,
    currency TEXT NOT NULL, current_revision_no INTEGER NOT NULL, last_mutation_key TEXT)`,
  `CREATE TABLE IF NOT EXISTS cloud_thesis_revisions (
    instrument_key TEXT NOT NULL REFERENCES cloud_theses(instrument_key),
    revision_no INTEGER NOT NULL, revision_key TEXT NOT NULL, content_hash TEXT NOT NULL,
    thesis_text TEXT NOT NULL, effective_at TEXT NOT NULL, mutation_key TEXT NOT NULL,
    PRIMARY KEY (instrument_key, revision_no), UNIQUE (instrument_key, revision_key),
    UNIQUE (instrument_key, mutation_key))`,
];

export async function initSyntheticTables(db) {
  for (const statement of DDL) await db.prepare(statement).run();
}

async function runOnce(db) {
  // DDL is fixed in this source. The sole invocation channel is a scheduled event.
  await initSyntheticTables(db);
  const claimed = await db.prepare(`INSERT INTO cloud_test_runs (run_key, status)
    VALUES (?, 'RUNNING') ON CONFLICT(run_key) DO NOTHING`).bind(RUN_KEY).run();
  if (claimed.meta.changes !== 1) return;
  try {
    await importAccount(db, { fingerprint: ACCOUNT, broker: 'synthetic',
      maskedLabel: '***fixture', baseCurrency: 'KRW' });
    const row = { accountFingerprint: ACCOUNT, sourceKey: 'fill-fixed',
      source: 'synthetic', instrument: null, currency: 'KRW',
      transactionType: 'CASH', status: 'FILLED', tradedAt: '2026-02-02T00:00:00Z',
      settledAt: null, quantity: '0', unitPrice: null, grossAmount: '10.25',
      fee: null, tax: null, netAmount: '10.25' };
    const ledgerFirst = await recordTransactionOnce(db, row);
    const ledgerReplay = await recordTransactionOnce(db, row);
    const claims = await Promise.all([
      enqueueUpdate(db, 731001, 'synthetic', '{"fixture":true}', 'synthetic-t0'),
      enqueueUpdate(db, 731001, 'synthetic', '{"fixture":true}', 'synthetic-t0'),
    ]);
    const sendStarted = await beginSend(db, 'update:731001:0');
    const uncertain = await finishSend(db, 'update:731001:0', false, 'synthetic-t1');
    const replay = await enqueueUpdate(db, 731001, 'synthetic',
      '{"fixture":true}', 'synthetic-t2');
    const state = { instrument: { market: 'NASDAQ', ticker: 'FIXTURE',
      assetType: 'ETF', currency: 'USD' }, accountFingerprint: ACCOUNT,
    source: 'TELEGRAM', amountCurrency: 'KRW', dailyAmount: '1000',
    targetWeight: '0.1', enabled: true, timezoneName: 'Asia/Seoul',
    startedAt: '2026-02-01T00:00:00Z', memo: null };
    const planFirst = await createPlanOnce(db, 'manual:NASDAQ:FIXTURE',
      'fixture-plan-create', state, '2026-02-01T00:00:00Z');
    const planReplay = await createPlanOnce(db, 'manual:NASDAQ:FIXTURE',
      'fixture-plan-create', state, '2026-02-01T00:00:00Z');
    const planChanged = await applyPlanChange(db, 'manual:NASDAQ:FIXTURE', 1,
      'fixture-plan-change', { ...state, dailyAmount: '3000' },
      '2026-02-02T00:00:00Z');
    const planStale = await applyPlanChange(db, 'manual:NASDAQ:FIXTURE', 1,
      'fixture-plan-stale', { ...state, dailyAmount: '3000' },
      '2026-02-02T00:00:00Z');
    const second = { ...state, instrument: { ...state.instrument, ticker: 'FIXTURE2' },
      dailyAmount: '4000' };
    const secondCreated = await createPlanOnce(db, 'manual:NASDAQ:FIXTURE2',
      'fixture-plan-create-2', second, '2026-02-01T00:00:00Z');
    const group = [
      { planKey: 'manual:NASDAQ:FIXTURE', expectedVersion: 2,
        nextState: { ...state, dailyAmount: '2000' } },
      { planKey: 'manual:NASDAQ:FIXTURE2', expectedVersion: 1,
        nextState: { ...second, dailyAmount: '3000' } },
    ];
    const groupChanged = await applyGroupChanges(db, group, 'fixture-group',
      '2026-02-02T00:00:00Z');
    const groupReplay = await applyGroupChanges(db, group, 'fixture-group',
      '2026-02-02T00:00:00Z');
    const thesis = { market: 'NASDAQ', ticker: 'FIXTURE', currency: 'USD',
      text: 'synthetic-only thesis', revisionKey: 'fixture-revision',
      mutationKey: 'fixture-thesis-create', expectedRevision: 0,
      effectiveAt: '2026-02-01T00:00:00Z' };
    const thesisFirst = await appendThesis(db, thesis);
    const thesisReplay = await appendThesis(db, thesis);
    const counts = await db.prepare(`SELECT
      (SELECT COUNT(*) FROM cloud_transactions) AS ledger_count,
      (SELECT COUNT(*) FROM cloud_outbox WHERE status = 'UNKNOWN') AS unknown_count,
      (SELECT COUNT(*) FROM cloud_update_claims) AS update_count,
      (SELECT COUNT(*) FROM cloud_plan_revisions) AS plan_revisions,
      (SELECT COUNT(*) FROM cloud_mutation_checks) AS group_checks,
      (SELECT COUNT(*) FROM cloud_thesis_revisions) AS thesis_revisions`).first();
    const passed = ledgerFirst && !ledgerReplay && claims.filter(Boolean).length === 1 &&
      sendStarted && uncertain && !replay && counts.ledger_count === 1 &&
      counts.unknown_count === 1 && counts.update_count === 1 &&
      planFirst === 'CREATED' && planReplay === 'REPLAY' &&
      planChanged === 'CHANGED' && planStale === 'CONFLICT' &&
      secondCreated === 'CREATED' && groupChanged === 'CHANGED' &&
      groupReplay === 'REPLAY' && counts.group_checks === 2 &&
      thesisFirst === 'CHANGED' && thesisReplay === 'CONFLICT' &&
      counts.plan_revisions === 5 && counts.thesis_revisions === 1;
    await db.prepare(`UPDATE cloud_test_runs SET status = ?, completed_at = ?
      WHERE run_key = ? AND status = 'RUNNING'`).bind(passed ? 'PASS' : 'FAIL',
      new Date().toISOString(), RUN_KEY).run();
  } catch {
    // No retry or hidden second send after an uncertain result.
    await db.prepare(`UPDATE cloud_test_runs SET status = 'FAIL', completed_at = ?
      WHERE run_key = ? AND status = 'RUNNING'`)
      .bind(new Date().toISOString(), RUN_KEY).run();
  }
}

export default {
  async fetch(request, env) {
    const path = new URL(request.url).pathname;
    if (request.method !== 'GET' || path !== '/health') {
      return new Response('not found', { status: 404 });
    }
    if (!env.DB) return Response.json({ status: 'UNBOUND' }, { status: 503 });
    try {
      const run = await env.DB.prepare(`SELECT status FROM cloud_test_runs
        WHERE run_key = ?`).bind(RUN_KEY).first();
      return Response.json({ status: run?.status || 'NOT_RUN' },
        { headers: { 'Cache-Control': 'no-store' } });
    } catch {
      return Response.json({ status: 'NOT_RUN' },
        { headers: { 'Cache-Control': 'no-store' } });
    }
  },
  async scheduled(_event, env) {
    if (env.TEST_WRITES_ENABLED !== '1' || !env.DB) return;
    await runOnce(env.DB);
  },
};
