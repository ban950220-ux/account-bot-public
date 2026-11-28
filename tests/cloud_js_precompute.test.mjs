import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { FakeD1 } from './cloud_js_fake_d1.mjs';
import { beginD1Archive, stageD1ArchiveChunk, finalizeD1Archive } from '../deploy/cloudflare-js/d1-archive-import.mjs';
import { beginCoreImport, stageCoreChunk, finalizeCoreImport } from '../deploy/cloudflare-js/d1-core-import.mjs';
import { activateCutover } from '../deploy/cloudflare-js/cutover-state.mjs';
import { createOperatingGateway } from '../deploy/cloudflare-js/operating-gateway.mjs';
import { createOperatingWorker } from '../deploy/cloudflare-js/operating-worker.mjs';
import { splitTelegramMessage } from '../deploy/cloudflare-js/telegram-transport.mjs';
import { putMarketCaches } from '../deploy/cloudflare-js/market-cache.mjs';
import { signalSymbol, trendFromWeeklyCloses } from '../deploy/cloudflare-js/signals.mjs';
import { createD1Rest, statementKind } from '../deploy/cloudflare-js/d1-rest.mjs';
import { computeAll, inputFingerprint, storePrecomputed } from '../deploy/cloudflare-js/precompute.mjs';
import { asOfLine } from '../deploy/cloudflare-js/precomputed-store.mjs';

const root = fileURLToPath(new URL('../', import.meta.url));
const ACCOUNT = 'a'.repeat(32);
const DATABASE = 'aa55d17f-9560-4e48-b06c-166c53a21126';

async function fixture(t) {
  const path = await mkdtemp(join(tmpdir(), 'precompute-fixture-'));
  t.after(() => rm(path, { recursive: true, force: true }));
  const result = spawnSync('python', ['-B', 'tests/cloud_bundle_fixture.py', path, '--manual'],
    { cwd: root, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' }, encoding: 'utf8', maxBuffer: 1 << 26 });
  assert.equal(result.status, 0, result.stderr);
  const { archive, core } = JSON.parse(result.stdout);
  const db = new FakeD1();
  t.after(() => db.sqlite.close());
  await beginD1Archive(db, archive.manifest);
  for (const chunk of archive.chunks) await stageD1ArchiveChunk(db, archive.manifest, chunk);
  await finalizeD1Archive(db, archive.manifest);
  await beginCoreImport(db, core.manifest, core.metadata);
  for (const chunk of core.chunks) await stageCoreChunk(db, core.manifest, chunk);
  await finalizeCoreImport(db, core.manifest);
  await activateCutover(db, { expectedEpoch: 1, edits: true, sends: true, proof: {
    runId: core.manifest.runId, sourceSha: core.manifest.sourceLogicalSha,
    targetId: core.metadata.scope.targetId, ownerUserId: '42', observedAt: '2026-10-29T00:00:00Z',
    reportsAfter: '2026-10-29T00:01:00Z', pcStopped: true, pcStopEvidenceSha: '1'.repeat(64),
    backupReadbackSha: core.manifest.backupSha, rollbackRehearsalSha: '3'.repeat(64),
    deliveryStatesReconciled: true } }, '2026-10-29T00:00:00Z');
  // Production vars (wrangler.production.json) plus the fixture's scope and owner.
  const env = { DB: db, CLOUD_TARGET_ID: core.metadata.scope.targetId,
    CLOUD_PORTFOLIO_ID: String(core.metadata.scope.portfolioId),
    KIWOOM_ACCUM_FINGERPRINT: core.metadata.scope.recurringAccountFingerprint,
    TELEGRAM_BOT_TOKEN: `123456:${'x'.repeat(30)}`, TELEGRAM_WEBHOOK_SECRET: 'fixture-secret',
    TELEGRAM_ALLOWED_USER_IDS: '42', TELEGRAM_CHAT_ID: '42', REPORT_TIMEZONE: 'Asia/Seoul',
    DAILY_REPORT_TIME: '08:00:00', WEEKLY_REPORT_TIME: '09:00:00', WEEKLY_REPORT_WEEKDAY: '5',
    PUBLIC_SIGNAL_REFRESH_ENABLED: 'true' };
  return { db, env };
}

// The D1 REST API answered from the same SQLite database the Worker binding uses.
function restFetcher(db, seen = []) {
  return async (url, options) => {
    assert.match(String(url), new RegExp(`/accounts/${ACCOUNT}/d1/database/${DATABASE}/query$`));
    assert.equal(options.headers.authorization, 'Bearer fixture-token');
    const { sql, params } = JSON.parse(options.body);
    seen.push(sql);
    const statement = db.sqlite.prepare(sql);
    const reader = statement.columns().length > 0;
    const results = reader ? statement.all(...params) : [];
    const meta = reader ? {} : { changes: Number(statement.run(...params).changes) };
    return { ok: true, status: 200, json: async () => ({ success: true, errors: [],
      result: [{ success: true, results, meta }] }) };
  };
}

const request = (id, text) => new Request('https://fixture.invalid/telegram/webhook', { method: 'POST',
  headers: { 'x-telegram-bot-api-secret-token': 'fixture-secret' },
  body: JSON.stringify({ update_id: id, message: { from: { id: 42 }, chat: { id: 42, type: 'private' }, text } }) });

async function seedPlans(db, env, clock) {
  const worker = createOperatingWorker({ requireCutover: true, now: () => clock.now,
    fetcher: async () => ({ ok: true, json: async () => ({ ok: true, result: { message_id: 1 } }) }),
    gatewayFactory: (e, _c, o) => createOperatingGateway(e, { ...o, now: () => clock.now }) });
  const tickers = ['QQQ', 'VOO', 'NVDA', 'AMD', 'brk.b', 'SCHD', 'TSLA', 'MSFT'];
  let id = 100;
  for (const [index, ticker] of tickers.entries()) {
    clock.now = new Date(Date.parse(clock.now) + 1000).toISOString();
    const response = await worker.fetch(request(id++, `/recurring_add ${ticker} ${10 + index * 5}USD ${index + 1}%`), env);
    assert.equal(response.status, 200);
  }
  clock.now = new Date(Date.parse(clock.now) + 1000).toISOString();
  assert.equal((await worker.fetch(request(id++, '/모으기설정 AMD 30'), env)).status, 200);
  // Cached public signals for most plans, as the scheduled refresh leaves them.
  const closes = Array.from({ length: 261 }, (_, i) => 100 * (1 + i / 300) * (1 + 0.04 * Math.sin(i / 6)));
  const trend = trendFromWeeklyCloses(closes);
  await putMarketCaches(db, tickers.slice(0, 6).map(ticker => {
    const symbol = signalSymbol({ market: 'US', ticker });
    return { provider: 'yahoo-signal', namespace: 'signal',
      cacheKey: [...symbol].map(c => c.charCodeAt(0).toString(16).padStart(2, '0')).join(''),
      value: { symbol, asOf: clock.now, trend, valuation: null },
      fetchedAt: clock.now, expiresAt: '2026-12-31T00:00:00.000Z', staleUntil: '2026-12-31T00:00:00.000Z' };
  }));
}

test('Actions precompute produces exactly the bytes the Worker sends', async t => {
  const { db, env } = await fixture(t);
  const clock = { now: '2026-11-20T00:00:00.000Z' };
  await seedPlans(db, env, clock);
  // Friday 08:00 KST: the daily report is due. Saturday 09:00 KST: the weekly report.
  for (const at of ['2026-11-26T23:00:00.000Z', '2026-11-28T00:00:00.000Z']) {
    clock.now = at;
    const restEnv = { ...env, DB: createD1Rest({ accountId: ACCOUNT, databaseId: DATABASE,
      token: 'fixture-token', fetcher: restFetcher(db) }) };
    const pre = await computeAll(restEnv, { now: () => clock.now,
      fetcher: async () => assert.fail('no public provider is enabled in production') });
    assert.equal(pre.stable, true);
    const gateway = await createOperatingGateway(env, { now: () => clock.now });
    assert.equal((await gateway.signals.read(await gateway.listPlans(true))).size, 6, 'scores are part of the replies');
    assert.equal(pre.items[0].inputFingerprint, await inputFingerprint(db));

    const sent = [];
    const worker = createOperatingWorker({ requireCutover: true, now: () => clock.now,
      fetcher: async (_url, options) => { sent.push(JSON.parse(options.body).text);
        return { ok: true, json: async () => ({ ok: true, result: { message_id: sent.length } }) }; },
      providerFetcher: async () => { throw new Error('public source unavailable'); },
      gatewayFactory: (e, _c, o) => createOperatingGateway(e, { ...o, now: () => clock.now }) });
    const byKind = new Map(pre.items.map(item => [item.kind, item]));
    let id = at.startsWith('2026-11-26') ? 5000 : 6000;
    for (const [text, kind] of [['/모으기', 'recurring'], ['/weekly', 'weekly_cmd'], ['/추천', 'recommend']]) {
      sent.length = 0;
      assert.equal((await worker.fetch(request(id++, text), env)).status, 200);
      assert.deepEqual(sent, splitTelegramMessage(byKind.get(kind).body), kind);
    }
    sent.length = 0;
    await worker.scheduled({ scheduledTime: Date.parse(at) }, env);
    if (at.startsWith('2026-11-26')) {
      assert.equal(byKind.get('daily_report').periodKey, 'daily:20261127');
      assert.equal(byKind.has('weekly_report'), false);
      assert.deepEqual(sent, splitTelegramMessage(byKind.get('daily_report').body));
    } else {
      assert.equal(byKind.has('daily_report'), false);
      assert.equal(byKind.get('weekly_report').periodKey, 'weekly:20261128');
      assert.deepEqual(sent, splitTelegramMessage(byKind.get('weekly_report').body));
    }
    assert.ok(sent.length > 0);
  }
});

test('the fingerprint moves on every plan, policy and archive change', async t => {
  const { db, env } = await fixture(t);
  const clock = { now: '2026-11-20T00:00:00.000Z' };
  const first = await inputFingerprint(db);
  await seedPlans(db, env, clock);
  const second = await inputFingerprint(db);
  assert.notEqual(first, second);
  const worker = createOperatingWorker({ requireCutover: true, now: () => clock.now,
    fetcher: async () => ({ ok: true, json: async () => ({ ok: true, result: { message_id: 1 } }) }),
    gatewayFactory: (e, _c, o) => createOperatingGateway(e, { ...o, now: () => clock.now }) });
  let previous = second;
  for (const [index, text] of ['/모으기설정 QQQ 25', '/목표 예산 50000', '/모으기삭제 TSLA', '/모으기복원 TSLA'].entries()) {
    clock.now = new Date(Date.parse(clock.now) + 1000).toISOString();
    assert.equal((await worker.fetch(request(9000 + index, text), env)).status, 200);
    const next = await inputFingerprint(db);
    assert.notEqual(next, previous, text);
    previous = next;
  }
  // Read-only commands leave it unchanged.
  await worker.fetch(request(9100, '/모으기'), env);
  assert.equal(await inputFingerprint(db), previous);
});

test('computeAll keeps nothing when the inputs change while it runs', async t => {
  const { db, env } = await fixture(t);
  const clock = { now: '2026-11-20T00:00:00.000Z' };
  await seedPlans(db, env, clock);
  clock.now = '2026-11-26T23:00:00.000Z';
  const worker = createOperatingWorker({ requireCutover: true, now: () => clock.now,
    fetcher: async () => ({ ok: true, json: async () => ({ ok: true, result: { message_id: 1 } }) }),
    gatewayFactory: (e, _c, o) => createOperatingGateway(e, { ...o, now: () => clock.now }) });
  // A plan edit lands after the first fingerprint read, while replies are computed.
  const result = await computeAll(env, { now: () => clock.now, fetcher: async () => assert.fail(),
    gatewayFactory: async (e, options) => {
      assert.equal((await worker.fetch(request(7000, '/모으기설정 QQQ 30'), env)).status, 200);
      return createOperatingGateway(e, options);
    } });
  assert.deepEqual(result, { stable: false, items: [] });
});

test('the REST adapter refuses every write outside derived tables before sending it', async () => {
  for (const sql of ['SELECT * FROM cloud_plans WHERE plan_key = ?', 'WITH x AS (SELECT 1) SELECT * FROM x',
    "SELECT 'DELETE FROM cloud_plans' AS text"]) assert.equal(statementKind(sql), 'read');
  for (const sql of ['INSERT INTO cloud_precomputed (kind) VALUES (?) ON CONFLICT (kind) DO UPDATE SET body = excluded.body',
    'INSERT INTO cloud_market_cache (provider) VALUES (?)']) assert.equal(statementKind(sql), 'write');
  for (const sql of ['INSERT INTO cloud_plans (plan_key) VALUES (?)', 'UPDATE cloud_plans SET daily_amount = 0',
    'DELETE FROM cloud_outbox', 'DROP TABLE cloud_plans', 'REPLACE INTO cloud_policy VALUES (1)',
    'INSERT OR REPLACE INTO cloud_plans VALUES (1)', 'SELECT 1; DELETE FROM cloud_outbox',
    'INSERT INTO cloud_market_cache (provider) SELECT 1; UPDATE cloud_cutover SET state = 1',
    'WITH x AS (DELETE FROM cloud_outbox RETURNING *) SELECT * FROM x', 'PRAGMA foreign_keys = OFF',
    'INSERT INTO cloud_precomputed (kind) VALUES (1) ON CONFLICT DO UPDATE SET kind = (SELECT 1); DROP TABLE cloud_plans']) {
    assert.throws(() => statementKind(sql), /not allowed|one statement/, sql);
  }
  let sends = 0;
  const db = createD1Rest({ accountId: ACCOUNT, databaseId: DATABASE, token: 't', fetcher: async () => { sends += 1; } });
  assert.throws(() => db.prepare('UPDATE cloud_plans SET enabled = 0'), /not allowed/);
  assert.equal(sends, 0);
  const failing = createD1Rest({ accountId: ACCOUNT, databaseId: DATABASE, token: 't',
    fetcher: async () => ({ ok: false, status: 400, json: async () => ({ success: false, errors: [{ code: 7500 }] }) }) });
  await assert.rejects(failing.prepare('SELECT ? AS secret').bind('plan-data').first(),
    error => /HTTP 400, code 7500/.test(error.message) && !error.message.includes('plan-data'));
});

test('the Worker sends a stored reply only while every condition holds', async t => {
  const { db, env } = await fixture(t);
  const clock = { now: '2026-11-20T00:00:00.000Z' };
  await seedPlans(db, env, clock);
  const sent = [];
  const worker = createOperatingWorker({ requireCutover: true, now: () => clock.now,
    fetcher: async (_url, options) => { sent.push(JSON.parse(options.body).text);
      return { ok: true, json: async () => ({ ok: true, result: { message_id: sent.length } }) }; },
    gatewayFactory: (e, _c, o) => createOperatingGateway(e, { ...o, now: () => clock.now }) });
  let id = 8000;
  const reply = async text => { sent.length = 0;
    assert.equal((await worker.fetch(request(id++, text), env)).status, 200); return sent.join(''); };
  // Stored through the REST adapter, as the Actions job will.
  const rest = createD1Rest({ accountId: ACCOUNT, databaseId: DATABASE, token: 'fixture-token', fetcher: restFetcher(db) });
  const precompute = async at => { const result = await computeAll({ ...env, DB: rest }, { now: () => at,
    fetcher: async () => assert.fail() }); await storePrecomputed(rest, result.items); return result; };
  const marker = '\u2063precomputed';
  const mark = () => db.sqlite.exec(`UPDATE cloud_precomputed SET body = body || '${marker}'`);

  clock.now = '2026-11-26T23:00:00.000Z';
  const direct = await reply('/모으기');
  assert.doesNotMatch(direct, /🕘/);
  const result = await precompute('2026-11-26T22:10:00.000Z');
  assert.equal(result.items.find(item => item.kind === 'recurring').body, direct);
  mark();
  const stored = await reply('/모으기');
  assert.equal(stored, `${asOfLine('2026-11-26T22:10:00.000Z', 'Asia/Seoul')}\n${direct}${marker}`);
  assert.match(stored, /^🕘 11\/27 07:10 KST 기준\n/);
  assert.match(await reply('/추천'), new RegExp(marker));
  assert.match(await reply('/weekly'), new RegExp(marker));
  // Arguments always take the computed path.
  assert.doesNotMatch(await reply('/모으기 전체'), new RegExp(marker));

  // Older than two hours, or from the future.
  clock.now = '2026-11-27T00:10:01.000Z';
  assert.equal(await reply('/모으기'), direct);
  clock.now = '2026-11-26T22:09:59.000Z';
  assert.equal(await reply('/모으기'), direct);
  clock.now = '2026-11-26T23:00:00.000Z';
  assert.match(await reply('/모으기'), new RegExp(marker));
  // Another period key.
  db.sqlite.exec("UPDATE cloud_precomputed SET period_key = 'daily:20261127' WHERE kind = 'recurring'");
  assert.equal(await reply('/모으기'), direct);
  db.sqlite.exec("UPDATE cloud_precomputed SET period_key = '' WHERE kind = 'recurring'");
  assert.match(await reply('/모으기'), new RegExp(marker));
  // A plan change after the computation.
  assert.match(await reply('/모으기설정 QQQ 40'), /QQQ/);
  const changed = await reply('/모으기');
  assert.doesNotMatch(changed, new RegExp(marker));
  assert.doesNotMatch(changed, /🕘/);
  // No table yet (before the migration): computed as today.
  await precompute('2026-11-26T22:30:00.000Z');
  mark();
  assert.match(await reply('/모으기'), new RegExp(marker));
  db.sqlite.exec('DROP TABLE cloud_precomputed');
  assert.equal(await reply('/모으기'), changed);
});
