import assert from 'node:assert/strict';
import { test } from 'node:test';
import { FakeD1 } from './cloud_js_fake_d1.mjs';
import { createOperatingGateway } from '../deploy/cloudflare-js/operating-gateway.mjs';
import { createOperatingWorker } from '../deploy/cloudflare-js/operating-worker.mjs';
import { putMarketCache } from '../deploy/cloudflare-js/market-cache.mjs';
import { enqueueReportChunks } from '../deploy/cloudflare-js/outbox.mjs';

const stamp = '2026-10-31T00:00:00.000Z';
const fingerprint = `fixture_${'f'.repeat(64)}`;
const symbols = Array.from({ length: 40 }, (_, i) => `FIX${i}`);
const cacheKey = symbol => [...symbol].map(c => c.charCodeAt(0).toString(16).padStart(2, '0')).join('');

// Count D1 binding invocations, not SQL statements inside a batch.
function metered(db, limit = Infinity) {
  let calls = 0;
  const charge = () => { if (++calls > limit) throw new Error('synthetic D1 invocation limit'); };
  const originals = new WeakMap();
  function wrap(statement) {
    const value = new Proxy(statement, { get(target, key) {
      if (key === 'bind') return (...values) => wrap(target.bind(...values));
      if (['first', 'all', 'run', 'raw'].includes(key)) return async (...args) => {
        charge(); return target[key](...args);
      };
      return target[key];
    } });
    originals.set(value, statement);
    return value;
  }
  return { get calls() { return calls; }, reset() { calls = 0; }, db: {
    prepare: sql => wrap(db.prepare(sql)),
    batch: async statements => { charge(); return db.batch(statements.map(s => originals.get(s) || s)); },
  } };
}

async function fixture(t, mode, limit = Infinity, thesisLength = 0, brokerConfigured = false, { brokerPages = 1, httpLimit = Infinity } = {}) {
  const db = new FakeD1();
  t.after(() => db.sqlite.close());
  db.sqlite.prepare(`INSERT INTO cloud_accounts VALUES (?, 'synthetic', NULL, 'KRW')`).run(fingerprint);
  for (let i = 0; i < 41; i++) {
    db.sqlite.prepare(`INSERT INTO cloud_plans (plan_key, account_fingerprint, source,
      market, ticker, asset_type, instrument_currency, amount_currency, daily_amount,
      target_weight, enabled, timezone_name, started_at, memo, version)
      VALUES (?, ?, ?, 'NASDAQ', ?, 'EQUITY', 'USD', 'KRW', '1000', NULL, 1,
        'Asia/Seoul', '2026-10-01T00:00:00Z', NULL, 1)`)
      .run(`fixture:${i}`, fingerprint, i === 40 ? 'MERITZ_SNAPSHOT' : 'TELEGRAM', symbols[i % 40]);
  }
  if (thesisLength) for (const symbol of symbols) {
    const key = `NASDAQ:${symbol}`;
    db.sqlite.prepare(`INSERT INTO cloud_theses VALUES (?, 'NASDAQ', ?, 'USD', 1, NULL)`).run(key, symbol);
    db.sqlite.prepare(`INSERT INTO cloud_thesis_revisions VALUES (?, 1, 'fixture',
      'fixture', ?, ?, 'fixture')`).run(key, '"'.repeat(thesisLength), stamp);
  }
  db.sqlite.exec(`INSERT INTO cloud_core_import_runs VALUES ('fixture', '{}', '{}', 'VERIFIED')`);
  db.sqlite.prepare(`INSERT INTO cloud_cutover (singleton, run_id, epoch, state,
    owner_user_id, target_id, scope_fingerprint, source_sha256, report_timezone,
    portfolio_id, reports_after, edits_enabled, sends_enabled)
    VALUES (1, 'fixture', 2, 'ACTIVE', '42', 'fixture-worker', ?, 'fixture',
      'Asia/Seoul', 1, '2026-10-30T08:22:50Z', 1, 1)`).run(fingerprint);
  if (['hit', 'stale'].includes(mode)) for (const symbol of ['KRW=X', ...symbols]) {
    await putMarketCache(db, { provider: 'yahoo-chart', namespace: 'price', cacheKey: cacheKey(symbol),
      value: { price: symbol === 'KRW=X' ? '1400' : '100',
        currency: symbol === 'KRW=X' ? 'KRW' : 'USD', asOf: stamp, provider: 'yahoo-chart' },
      fetchedAt: mode === 'stale' ? '2026-10-30T00:00:00Z' : stamp,
      expiresAt: mode === 'stale' ? '2026-10-30T00:15:00Z' : '2026-10-31T00:15:00Z',
      staleUntil: '2026-11-02T00:00:00Z' });
  }
  const meter = metered(db, limit);
  const sent = [];
  let http = 0;
  let brokerHttp = 0;
  let lastRefresh = null;
  const env = { DB: meter.db, TELEGRAM_BOT_TOKEN: `123456:${'a'.repeat(36)}`,
    TELEGRAM_WEBHOOK_SECRET: 'synthetic-secret', TELEGRAM_ALLOWED_USER_IDS: '42',
    TELEGRAM_CHAT_ID: '42', REPORT_TIMEZONE: 'Asia/Seoul', DAILY_REPORT_TIME: '08:00',
    WEEKLY_REPORT_TIME: '09:00', WEEKLY_REPORT_WEEKDAY: '5',
    CLOUD_TARGET_ID: 'fixture-worker', CLOUD_PORTFOLIO_ID: '1',
    KIWOOM_ACCUM_FINGERPRINT: fingerprint, PUBLIC_YAHOO_CHART_ENABLED: String(mode !== 'off') };
  if (brokerConfigured) Object.assign(env, { KIWOOM_MAIN_APP_KEY: 'synthetic-key',
    KIWOOM_MAIN_APP_SECRET: 'synthetic-secret', KIWOOM_MAIN_ACCOUNT_ALIAS: 'synthetic-main' });
  const externalAttempts = [];
  const pageCounts = new Map();
  function charge(kind) {
    externalAttempts.push(kind);
    if (externalAttempts.length > httpLimit) throw new Error('synthetic external HTTP cap');
  }
  const providerFetch = async (url, options) => {
    charge(url.startsWith('https://api.kiwoom.com/') ? 'broker' : 'yahoo');
    if (url.startsWith('https://api.kiwoom.com/')) {
      brokerHttp++;
      const payload = url.endsWith('/oauth2/token') ? { token: 'synthetic-token' } :
        options.headers['api-id'] === 'kt00018' ? { acnt_evlt_remn_indv_tot: [] } :
          options.headers['api-id'] === 'kt00001' ? { entr: '100' } : { result_list: [] };
      const tr = options.headers?.['api-id'];
      const page = (pageCounts.get(tr) || 0) + 1;
      pageCounts.set(tr, page);
      const pageLimit = typeof brokerPages === 'number' ? brokerPages : brokerPages[tr] || 1;
      const more = ['kt00018', 'ust21070'].includes(tr) && page < pageLimit;
      return { ok: true, headers: new Headers(more ? { 'cont-yn': 'Y', 'next-key': String(page) } : {}),
        json: async () => payload };
    }
    http++;
    assert.ok(url.startsWith('https://query1.finance.yahoo.com/'));
    if (['stale', 'outage'].includes(mode)) throw new Error('synthetic provider outage');
    const fx = url.includes('KRW%3DX');
    return { ok: true, headers: new Headers(), json: async () => ({ chart: { result: [{
      meta: { regularMarketPrice: fx ? 1400 : 100,
        regularMarketTime: 1793404800, currency: fx ? 'KRW' : 'USD' },
    }] } }) };
  };
  const worker = createOperatingWorker({ requireCutover: true, now: () => stamp,
    providerFetcher: providerFetch,
    gatewayFactory: async (env, _config, options) => {
      const gateway = await createOperatingGateway(env,
        { now: () => stamp, fetcher: options?.fetcher || providerFetch });
      const refresh = gateway.refresh.bind(gateway);
      gateway.refresh = async options => { lastRefresh = await refresh(options); return lastRefresh; };
      return gateway;
    },
    fetcher: async (_url, options) => {
      charge('telegram');
      const body = JSON.parse(options.body);
      assert.equal(body.chat_id, '42');
      sent.push(body.text);
      return { ok: true, json: async () => ({ ok: true, result: { message_id: sent.length } }) };
    } });
  return { db, meter, env, worker, sent, externalAttempts, get http() { return http; },
    get brokerHttp() { return brokerHttp; },
    get lastRefresh() { return lastRefresh; },
    tick: { scheduledTime: Date.parse(stamp) } };
}

function insertConcurrentWinningOutbox(f, chunks) {
  const prepare = f.db.prepare.bind(f.db);
  const batch = f.db.batch.bind(f.db);
  let enqueuePrepared = false;
  let winnerInserted = false;
  f.db.prepare = sql => {
    if (sql.includes('INSERT INTO cloud_report_claims')) enqueuePrepared = true;
    return prepare(sql);
  };
  f.db.batch = async statements => {
    if (enqueuePrepared && !winnerInserted) {
      winnerInserted = true;
      await enqueueReportChunks(f.db, 'weekly:20261031', '42',
        Array.from({ length: chunks }, (_, i) => `winner chunk ${i}`), stamp);
    }
    return batch(statements);
  };
}

for (const mode of ['off', 'hit', 'miss', 'stale', 'outage']) {
  test(`41 plans / 40 identities complete weekly path: ${mode}`, async t => {
    const f = await fixture(t, mode, 50);
    await f.worker.scheduled(f.tick, f.env);
    t.diagnostic(JSON.stringify({ mode, queries: f.meter.calls, http: f.http,
      chunks: f.sent.length, claims: f.db.rows('cloud_report_claims').length }));
    assert.equal(f.db.rows('cloud_report_claims').length, 1);
    assert.ok(f.db.rows('cloud_outbox').every(row => row.status === 'SENT'));
    assert.ok(f.sent.length > 0);
    assert.ok(f.meter.calls <= 50);
    const count = f.sent.length;
    f.meter.reset();
    await f.worker.scheduled(f.tick, f.env);
    assert.equal(f.sent.length, count);
  });
}

test('same 50-query cap now completes the formerly failing warm-cache path', async t => {
  const f = await fixture(t, 'hit', 50);
  await f.worker.scheduled(f.tick, f.env);
  assert.equal(f.db.rows('cloud_report_claims').length, 1);
  assert.ok(f.sent.length > 0);
  assert.ok(f.meter.calls <= 50);
  t.diagnostic(JSON.stringify({ queries: f.meter.calls,
    claims: f.db.rows('cloud_report_claims').length, chunks: f.db.rows('cloud_outbox').length,
    fakeSends: f.sent.length }));
});

test('oversized escaped Thesis report fails before claim, outbox or any fake send', async t => {
  const f = await fixture(t, 'miss', 50, 240);
  await assert.rejects(() => f.worker.scheduled(f.tick, f.env), /budget unavailable before report delivery/);
  assert.ok(f.meter.calls <= 50);
  assert.equal(f.db.rows('cloud_report_claims').length, 0);
  assert.equal(f.db.rows('cloud_outbox').length, 0);
  assert.equal(f.sent.length, 0);
  t.diagnostic(JSON.stringify({ queries: f.meter.calls, chunks: f.sent.length, http: f.http }));
});

test('full cold-cache path also includes the real read-only broker adapter with fake HTTP', async t => {
  const f = await fixture(t, 'miss', 50, 0, true);
  await f.worker.scheduled(f.tick, f.env);
  assert.equal(f.http, 41);
  assert.equal(f.brokerHttp, 6);
  assert.equal(f.db.rows('cloud_report_claims').length, 1);
  assert.ok(f.db.rows('cloud_outbox').every(row => row.status === 'SENT'));
  assert.ok(f.http + f.brokerHttp + f.sent.length <= 50);
  t.diagnostic(JSON.stringify({ queries: f.meter.calls, yahooHttp: f.http,
    brokerHttp: f.brokerHttp, telegramHttp: f.sent.length }));
});

test('claim loser reserves the actual winning outbox before its first send', async t => {
  const f = await fixture(t, 'miss', 50);
  // A warm-cache invocation can reserve 19 chunks in 49 D1 calls and commit
  // while this cold-cache invocation awaits its two-chunk enqueue batch.
  insertConcurrentWinningOutbox(f, 19);
  let failure;
  try { await f.worker.scheduled(f.tick, f.env); }
  catch (error) { failure = error; }
  const rows = f.db.rows('cloud_outbox');
  t.diagnostic(JSON.stringify({ queries: f.meter.calls, fakeSends: f.sent.length,
    pending: rows.filter(row => row.status === 'PENDING').length,
    sending: rows.filter(row => row.status === 'SENDING').length,
    sent: rows.filter(row => row.status === 'SENT').length }));
  assert.equal(f.sent.length, 0);
  assert.match(failure?.message || '', /budget unavailable before report delivery/);
  assert.equal(f.db.rows('cloud_report_claims').length, 1);
  assert.equal(rows.length, 19);
  assert.ok(rows.every(row => row.status === 'PENDING'));
});

for (const [label, thesisLength, brokerPages] of [
  ['two extra position pages', 0, 2], ['four report chunks', 30, 1],
]) {
  test(`shared external budget refuses before claim: ${label}`, async t => {
    const f = await fixture(t, 'miss', 50, thesisLength, true, { brokerPages, httpLimit: 50 });
    let failure;
    try { await f.worker.scheduled(f.tick, f.env); }
    catch (error) { failure = error; }
    const rows = f.db.rows('cloud_outbox');
    t.diagnostic(JSON.stringify({ label, queries: f.meter.calls, yahoo: f.http,
      broker: f.brokerHttp, externalAttempts: f.externalAttempts.length,
      fakeSends: f.sent.length, statuses: rows.map(row => row.status) }));
    assert.equal(f.sent.length, 0);
    assert.match(failure?.message || '', /external request budget/i);
    assert.equal(f.db.rows('cloud_report_claims').length, 0);
    assert.equal(rows.length, 0);
  });
}

test('one extra position page fits exactly 50 external calls with all chunks completed', async t => {
  const f = await fixture(t, 'miss', 50, 0, true,
    { brokerPages: { kt00018: 2 }, httpLimit: 50 });
  await f.worker.scheduled(f.tick, f.env);
  assert.equal(f.externalAttempts.length, 50);
  assert.equal(f.sent.length, 2);
  assert.ok(f.db.rows('cloud_outbox').every(row => row.status === 'SENT'));
});

test('page explosion never claims a report or promotes incomplete broker coverage to complete', async t => {
  const f = await fixture(t, 'miss', 50, 0, true, { brokerPages: 50, httpLimit: 50 });
  await assert.rejects(() => f.worker.scheduled(f.tick, f.env), /external request budget/);
  assert.equal(f.externalAttempts.length, 50);
  assert.equal(f.sent.length, 0);
  assert.equal(f.db.rows('cloud_report_claims').length, 0);
  assert.equal(f.db.rows('cloud_outbox').length, 0);
  assert.equal(f.lastRefresh.snapshot.isComplete, false);
  assert.equal(f.lastRefresh.snapshot.totalMarketValue, null);
  assert.ok(f.lastRefresh.positionResults.some(row => ['partial', 'unavailable'].includes(row.status)));
  t.diagnostic(JSON.stringify({ externalCalls: f.externalAttempts.length,
    positionStatuses: f.lastRefresh.positionResults.map(row => row.status),
    snapshotComplete: f.lastRefresh.snapshot.isComplete }));
});

test('claim loser also reserves the winning chunk count against remaining HTTP budget', async t => {
  const f = await fixture(t, 'miss', 50, 0, true, { httpLimit: 50 });
  insertConcurrentWinningOutbox(f, 4);
  await assert.rejects(() => f.worker.scheduled(f.tick, f.env), /external request budget/);
  assert.equal(f.externalAttempts.length, 47);
  assert.equal(f.sent.length, 0);
  const rows = f.db.rows('cloud_outbox');
  assert.equal(rows.length, 4);
  assert.ok(rows.every(row => row.status === 'PENDING'));
});
