import assert from 'node:assert/strict';
import { test } from 'node:test';
import { FakeD1 } from './cloud_js_fake_d1.mjs';
import { importAccount } from '../deploy/cloudflare-js/storage.mjs';
import { SyntheticCloudGateway } from '../deploy/cloudflare-js/cloud-gateway.mjs';
import { createPlanOnce } from '../deploy/cloudflare-js/cloud-write.mjs';
import { routeUpdate } from '../deploy/cloudflare-js/command-router.mjs';

const account = `fixture_${'a'.repeat(64)}`;
const secret = 'synthetic-secret';
const headers = [['X-Telegram-Bot-Api-Secret-Token', secret]];
function body(id, text) {
  return new TextEncoder().encode(JSON.stringify({ update_id: id, message: {
    from: { id: 42 }, chat: { id: 42, type: 'private' }, text,
  } }));
}

test('D1-backed synthetic commands preserve source, replay and revision history', async () => {
  const db = new FakeD1();
  await importAccount(db, { fingerprint: account, broker: 'synthetic', baseCurrency: 'KRW' });
  const provider = { name: 'kiwoom_main',
    getPositions: async () => ({ broker: 'kiwoom_main', status: 'ok', items: [] }),
    getCash: async () => ({ broker: 'kiwoom_main', status: 'ok', items: [] }),
    getAccounts: async () => ({ broker: 'kiwoom_main', status: 'ok', items: [] }) };
  const gateway = new SyntheticCloudGateway({ db, providers: [provider],
    scopeAccountFingerprint: account, externalRecurringPlans: async () => [],
    now: () => '2026-02-02T01:00:00Z' });
  const route = (id, command) => routeUpdate({ headerEntries: headers,
    body: body(id, command), secret, allowedUserIds: [42], gateway });
  assert.equal((await route(100, '/recurring_add TEST 20USD 8%')).status, 'READY');
  assert.equal((await route(100, '/recurring_add TEST 20USD 8%')).status, 'READY');
  assert.equal(db.rows('cloud_plans').length, 1);
  assert.equal(db.rows('cloud_plan_revisions').length, 1);
  assert.equal(db.rows('cloud_plans')[0].account_fingerprint, account);
  assert.equal((await route(101, '/모으기변경 개별주 전부 +1000')).status, 'READY');
  assert.equal(db.rows('cloud_plans')[0].daily_amount, '1020');
  assert.equal(db.rows('cloud_plan_revisions').length, 2);
  assert.equal((await route(101, '/모으기변경 개별주 전부 +1000')).status, 'READY');
  assert.equal(db.rows('cloud_plans')[0].daily_amount, '1020');
  assert.equal(db.rows('cloud_plan_revisions').length, 2);
  assert.equal((await route(102, '/thesis TEST fixture idea')).status, 'READY');
  assert.equal(db.rows('cloud_thesis_revisions').length, 1);
  assert.equal((await route(103, '/recurring')).status, 'READY');
  assert.equal((await route(104, '/assets')).status, 'READY');
  assert.equal(db.rows('cloud_transactions').length, 0);
});

test('missing external snapshot source is an explicit error', async () => {
  const db = new FakeD1();
  const gateway = new SyntheticCloudGateway({ db, providers: [],
    scopeAccountFingerprint: account, externalRecurringPlans: async () => {
      throw new Error('synthetic snapshot unavailable');
    } });
  await assert.rejects(() => gateway.listPlans(true), /snapshot unavailable/);
});

test('plan Thesis join uses the exact listing and current revision in one query', async () => {
  const db = new FakeD1();
  const gateway = new SyntheticCloudGateway({ db, providers: [],
    scopeAccountFingerprint: account, externalRecurringPlans: async () => [] });
  await importAccount(db, { fingerprint: account, broker: 'synthetic', baseCurrency: 'KRW' });
  const state = market => ({ instrument: { market, ticker: 'FIXTURE', assetType: 'EQUITY', currency: 'USD' },
    accountFingerprint: account, source: 'TELEGRAM', amountCurrency: 'KRW', dailyAmount: '1000',
    targetWeight: null, enabled: true, timezoneName: 'Asia/Seoul', startedAt: '2026-02-01T00:00:00Z', memo: null });
  for (const market of ['US', 'NASDAQ', 'NYSE']) {
    await createPlanOnce(db, `fixture:${market}`, `fixture:${market}`, state(market), '2026-02-01T00:00:00Z');
  }
  db.sqlite.exec(`INSERT INTO cloud_theses VALUES ('US:FIXTURE','US','FIXTURE','USD',1,NULL);
    INSERT INTO cloud_thesis_revisions VALUES ('US:FIXTURE',1,'first','hash','current text','2026-02-01T00:00:00Z','first');
    INSERT INTO cloud_thesis_revisions VALUES ('US:FIXTURE',2,'second','hash','later but not current','2026-02-02T00:00:00Z','second');
    INSERT INTO cloud_theses VALUES ('NASDAQ:FIXTURE','NASDAQ','FIXTURE','USD',1,NULL);
    INSERT INTO cloud_thesis_revisions VALUES ('NASDAQ:FIXTURE',1,'first','hash','other exchange','2026-02-01T00:00:00Z','first');`);
  let calls = 0;
  const prepare = db.prepare.bind(db);
  db.prepare = sql => { calls++; return prepare(sql); };
  const plans = await gateway.listPlans(true);
  assert.equal(calls, 1);
  assert.equal(plans.length, 3);
  assert.deepEqual(Object.fromEntries(plans.map(p => [p.instrument.market, p.investmentThesis])),
    { US: 'current text', NASDAQ: 'other exchange', NYSE: '' });
});

test('editing commands reject unauthorized, invalid and cancelled writes', async () => {
  const db = new FakeD1();
  await importAccount(db, { fingerprint: account, broker: 'synthetic', baseCurrency: 'KRW' });
  const gateway = new SyntheticCloudGateway({ db, providers: [],
    scopeAccountFingerprint: account, externalRecurringPlans: async () => [] });
  const route = (id, command, overrides = {}) => routeUpdate({ headerEntries: headers,
    body: body(id, command), secret, allowedUserIds: [42], gateway, ...overrides });
  assert.equal((await route(200, '/recurring_add TEST 0USD 8%')).status, 'READY');
  assert.equal((await route(201, '/recurring_add KOSPI:005930 20USD 8%')).status, 'READY');
  assert.equal((await route(202, '/recurring_add TEST 20USD 101%')).status, 'READY');
  assert.equal((await route(203, '/모으기변경 개별주 전부 +1500')).status, 'READY');
  assert.equal((await route(204, '/recurring_add TEST 20USD 8%',
    { allowedUserIds: [99] })).status, 'DENIED');
  const signal = AbortSignal.abort();
  assert.equal((await route(205, '/recurring_add TEST 20USD 8%', { signal })).status,
    'CANCELLED');
  assert.equal(db.rows('cloud_plans').length, 0);
  assert.equal(db.rows('cloud_plan_revisions').length, 0);
});

test('stop keeps history and cannot touch another account scope', async () => {
  const db = new FakeD1();
  const other = `fixture_${'b'.repeat(64)}`;
  await importAccount(db, { fingerprint: account, broker: 'synthetic', baseCurrency: 'KRW' });
  await importAccount(db, { fingerprint: other, broker: 'synthetic', baseCurrency: 'KRW' });
  const local = new SyntheticCloudGateway({ db, providers: [],
    scopeAccountFingerprint: account, externalRecurringPlans: async () => [] });
  const separate = new SyntheticCloudGateway({ db, providers: [],
    scopeAccountFingerprint: other, externalRecurringPlans: async () => [] });
  await local.createPlan({ market: 'US', ticker: 'TEST', amount: '20', currency: 'USD',
    targetWeightPct: '8', memo: '', mutationKey: 'update:301' });
  await assert.rejects(() => separate.createPlan({ market: 'US', ticker: 'TEST', amount: '30',
    currency: 'USD', targetWeightPct: '8', memo: '', mutationKey: 'update:302' }),
  /different account scope/);
  const changed = await local.changeGroup({ assetType: 'EQUITY', delta: null,
    stop: true, excluded: [], mutationKey: 'update:303' });
  assert.equal(changed.length, 1);
  assert.equal(db.rows('cloud_plans')[0].enabled, 0);
  assert.equal(db.rows('cloud_plan_revisions').length, 2);
  assert.equal((await local.changeGroup({ assetType: 'EQUITY', delta: null,
    stop: true, excluded: [], mutationKey: 'update:303' })).length, 1);
  assert.equal(db.rows('cloud_plan_revisions').length, 2);
});

test('explicit second D1 source scope joins atomic group edit without losing source history',
  async () => {
    const db = new FakeD1();
    const meritz = `fixture_${'b'.repeat(64)}`;
    for (const fingerprint of [account, meritz]) {
      await importAccount(db, { fingerprint, broker: 'synthetic', baseCurrency: 'KRW' });
    }
    const state = (ticker, scope, source, amount) => ({ instrument: {
      market: 'NASDAQ', ticker, assetType: 'EQUITY', currency: 'USD' },
    accountFingerprint: scope, source, amountCurrency: 'KRW', dailyAmount: amount,
    targetWeight: null, enabled: true, timezoneName: 'Asia/Seoul',
    startedAt: '2026-02-01T00:00:00Z', memo: null });
    await createPlanOnce(db, 'main:TEST', 'fixture-main',
      state('TEST', account, 'TELEGRAM', '3000'), '2026-02-01T00:00:00Z');
    await createPlanOnce(db, 'meritz:OTHER', 'fixture-meritz',
      state('OTHER', meritz, 'MERITZ_SNAPSHOT', '2000'), '2026-02-01T00:00:00Z');
    await createPlanOnce(db, 'unrelated:THIRD', 'fixture-unrelated',
      state('THIRD', meritz, 'TELEGRAM', '3000'), '2026-02-01T00:00:00Z');
    const gateway = new SyntheticCloudGateway({ db, providers: [],
      scopeAccountFingerprint: account, meritzSnapshotFingerprint: meritz,
      externalRecurringPlans: async () => [] });
    const changed = await gateway.changeGroup({ assetType: 'EQUITY', delta: '-1000',
      stop: false, excluded: [], mutationKey: 'update:400' });
    assert.deepEqual(changed.map(item => [item.broker, item.dailyAmount]).sort(),
      [['kiwoom_accum', '2000'], ['meritz', '1000']]);
    assert.equal(db.rows('cloud_plan_revisions').length, 5);
    assert.equal(db.rows('cloud_plans').length, 3);
    assert.equal(db.rows('cloud_plans').find(row => row.plan_key === 'unrelated:THIRD').daily_amount,
      '3000');
  });

test('every registered Python text command routes with synthetic providers', async () => {
  const db = new FakeD1();
  await importAccount(db, { fingerprint: account, broker: 'synthetic', baseCurrency: 'KRW' });
  const source = { name: 'kiwoom_main',
    getPositions: async () => ({ broker: 'kiwoom_main', status: 'ok', items: [] }),
    getCash: async () => ({ broker: 'kiwoom_main', status: 'ok', items: [] }),
    getAccounts: async () => ({ broker: 'kiwoom_main', status: 'ok', items: [] }) };
  const market = { getPrice: async () => null, getFundamentals: async () => null,
    getValuation: async () => null, getEarnings: async () => null,
    getNews: async () => [], getTrend: async () => null };
  const gateway = new SyntheticCloudGateway({ db, providers: [source],
    marketProvider: market, scopeAccountFingerprint: account,
    externalRecurringPlans: async () => [], now: () => '2026-02-02T01:00:00Z' });
  const commands = ['/start', '/help', '/assets', '/portfolio', '/recurring',
    '/recurring_add TEST 20USD 8%', '/recurring_update 개별주 전부 +1000',
    '/stock TEST', '/analyze TEST', '/thesis TEST synthetic idea',
    '/rebalance 1000', '/weekly'];
  for (const [index, command] of commands.entries()) {
    const result = await routeUpdate({ headerEntries: headers, body: body(500 + index, command),
      secret, allowedUserIds: [42], gateway });
    assert.equal(result.status, 'READY', command);
    assert.ok(result.reply.length, command);
  }
  assert.equal(db.rows('cloud_plan_revisions').length, 2);
  assert.equal(db.rows('cloud_thesis_revisions').length, 1);
  assert.equal(db.rows('cloud_outbox').length, 0);
});

test('plan mutation and exact reply are one D1 batch; failed outbox rolls both back', async () => {
  const db = new FakeD1();
  await importAccount(db, { fingerprint: account, broker: 'synthetic', baseCurrency: 'KRW' });
  const gateway = new SyntheticCloudGateway({ db, providers: [],
    scopeAccountFingerprint: account, externalRecurringPlans: async () => [] });
  const route = id => routeUpdate({ headerEntries: headers,
    body: body(id, '/recurring_add TEST 20USD 8%'), secret,
    allowedUserIds: [42], gateway, atomicDelivery: true,
    now: () => '2026-02-02T01:00:00Z' });
  db.sqlite.exec(`CREATE TRIGGER synthetic_fail_outbox BEFORE INSERT ON cloud_outbox
    BEGIN SELECT RAISE(ABORT, 'synthetic outbox failure'); END`);
  assert.equal((await route(700)).status, 'ERROR');
  assert.equal(db.rows('cloud_plans').length, 0);
  assert.equal(db.rows('cloud_plan_revisions').length, 0);
  assert.equal(db.rows('cloud_update_claims').length, 0);
  db.sqlite.exec('DROP TRIGGER synthetic_fail_outbox');
  const result = await route(700);
  assert.equal(result.status, 'READY');
  assert.equal(db.rows('cloud_plan_revisions').length, 1);
  assert.equal(db.rows('cloud_update_claims').length, 1);
  assert.equal(JSON.parse(db.rows('cloud_outbox')[0].payload_json).text, result.reply);
  assert.equal((await route(700)).status, 'READY');
  assert.equal(db.rows('cloud_plan_revisions').length, 1);
  assert.equal(db.rows('cloud_outbox').length, 1);
});

test('group and Thesis revisions stage their exact replies with the same transaction', async () => {
  const db = new FakeD1();
  await importAccount(db, { fingerprint: account, broker: 'synthetic', baseCurrency: 'KRW' });
  const gateway = new SyntheticCloudGateway({ db, providers: [],
    scopeAccountFingerprint: account, externalRecurringPlans: async () => [] });
  const route = (id, command) => routeUpdate({ headerEntries: headers,
    body: body(id, command), secret, allowedUserIds: [42], gateway,
    atomicDelivery: true, now: () => '2026-02-02T01:00:00Z' });
  await route(710, '/recurring_add TEST 20USD 8%');
  const group = await route(711, '/모으기변경 개별주 전부 +1000');
  const thesis = await route(712, '/thesis TEST synthetic idea');
  assert.equal(group.status, 'READY');
  assert.equal(thesis.status, 'READY');
  assert.equal(db.rows('cloud_plan_revisions').length, 2);
  assert.equal(db.rows('cloud_thesis_revisions').length, 1);
  for (const result of [group, thesis]) {
    const row = db.rows('cloud_outbox').find(item => item.event_key ===
      `update:${result.updateId}:0`);
    assert.equal(JSON.parse(row.payload_json).text, result.reply);
  }
  assert.equal((await route(711, '/모으기변경 개별주 전부 +1000')).status, 'READY');
  assert.equal(db.rows('cloud_plan_revisions').length, 2);
  assert.equal((await route(713, '/thesis TEST synthetic idea')).status, 'READY');
  assert.equal(db.rows('cloud_thesis_revisions').length, 1);
  assert.equal(db.rows('cloud_outbox').length, 4);
});

test('a preexisting update claim cannot commit a different plan mutation', async () => {
  const db = new FakeD1();
  await importAccount(db, { fingerprint: account, broker: 'synthetic', baseCurrency: 'KRW' });
  await db.prepare(`INSERT INTO cloud_update_claims (update_id, received_at, claim_nonce)
    VALUES (?, ?, ?)`).bind(720, '2026-02-02T01:00:00Z', 'synthetic-other').run();
  const gateway = new SyntheticCloudGateway({ db, providers: [],
    scopeAccountFingerprint: account, externalRecurringPlans: async () => [] });
  const outcome = await routeUpdate({ headerEntries: headers,
    body: body(720, '/recurring_add TEST 20USD 8%'), secret,
    allowedUserIds: [42], gateway, atomicDelivery: true });
  assert.equal(outcome.status, 'ERROR');
  assert.equal(db.rows('cloud_plans').length, 0);
  assert.equal(db.rows('cloud_plan_revisions').length, 0);
  assert.equal(db.rows('cloud_outbox').length, 0);
});

test('weekly valuation refresh deduplicates instruments and limits provider concurrency',
  async () => {
    const db = new FakeD1();
    let active = 0;
    let peak = 0;
    let clearCount = 0;
    const provider = { cache: { clear: prefix => {
      assert.equal(prefix, 'valuation:'); clearCount += 1;
    } }, getValuation: async () => {
      active += 1; peak = Math.max(peak, active);
      await Promise.resolve();
      active -= 1;
      return null;
    } };
    const plans = Array.from({ length: 9 }, (_, index) => ({
      instrument: { market: 'KOSPI', ticker: String(900000 + index),
        assetType: 'EQUITY', currency: 'KRW' },
      broker: 'synthetic', currency: 'KRW', dailyAmount: '1000',
      enabled: true, targetWeightPct: null,
    }));
    const gateway = new SyntheticCloudGateway({ db, providers: [],
      marketProvider: provider, scopeAccountFingerprint: account,
      externalRecurringPlans: async () => [plans[0], ...plans] });
    assert.deepEqual(await gateway.refreshValuations(), { successes: 9, failed: 0 });
    assert.ok(peak <= 4);
    assert.equal(clearCount, 1);
  });
