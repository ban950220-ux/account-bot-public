import assert from 'node:assert/strict';
import { test } from 'node:test';
import { FakeD1 } from './cloud_js_fake_d1.mjs';
import { importAccount } from '../deploy/cloudflare-js/storage.mjs';
import { SyntheticCloudGateway } from '../deploy/cloudflare-js/cloud-gateway.mjs';
import { createPlanOnce } from '../deploy/cloudflare-js/cloud-write.mjs';
import { routeUpdate } from '../deploy/cloudflare-js/command-router.mjs';
import { editPolicy, evaluate, normalizePolicy, proposalFingerprint } from
  '../deploy/cloudflare-js/allocation-policy.mjs';
import { currentPolicy, savePolicy } from '../deploy/cloudflare-js/policy-store.mjs';

const plan = (ticker, dailyAmount, group, extra = {}) => ({ id: `k:${ticker}`,
  planKey: `k:${ticker}`, version: 1, ticker, label: `US:${ticker}`, currency: 'KRW',
  dailyAmount, days: 5, adjustable: true, groups: [[group, '1']], ...extra });
const amounts = result => Object.fromEntries(result.changes.map(change =>
  [change.plan.ticker, change.to]));

test('no rules means no violations and no changes', () => {
  const [result] = evaluate([plan('AAA', '3000', 'A'), plan('BBB', '1000', 'B')],
    normalizePolicy({}));
  assert.equal(result.feasible, true);
  assert.deepEqual(result.changes, []);
  assert.deepEqual(result.violationsBefore, []);
  assert.equal(proposalFingerprint(0, [result]), null);
});

test('group cap reduces the largest member until the share fits', () => {
  // A = 6000 of 10000 (60%). Cap 50%: 5000/9000 = 55.6% still over, 4000/8000 = 50% fits.
  const [result] = evaluate([plan('AAA', '4000', 'A'), plan('AAB', '2000', 'A'),
    plan('BBB', '4000', 'B')], normalizePolicy({ groupCaps: { A: '50' } }));
  assert.equal(result.feasible, true);
  assert.deepEqual(amounts(result), { AAA: '2000' });
  assert.equal(result.before.weekly, '50000');
  assert.equal(result.after.weekly, '40000');
  assert.match(result.violationsBefore[0], /A 60\.0% > 상한 50%/);
  assert.deepEqual(result.changes[0].reasons, ['A 상한 초과']);
});

test('budget overrun is cut from the heaviest group and never below the minimum', () => {
  const [result] = evaluate([plan('AAA', '3000', 'A'), plan('BBB', '2000', 'B')],
    normalizePolicy({ dailyBudget: { KRW: '4000' } }));
  assert.equal(result.feasible, true);
  assert.deepEqual(amounts(result), { AAA: '2000' });
  const [stuck] = evaluate([plan('AAA', '1000', 'A'), plan('BBB', '1000', 'B')],
    normalizePolicy({ dailyBudget: { KRW: '1000' } }));
  assert.equal(stuck.feasible, false);
  assert.deepEqual(stuck.changes, []);
  assert.equal(stuck.after.weekly, stuck.before.weekly);
  assert.equal(stuck.unmet.length, 1);
});

test('pinned and non-adjustable plans are never changed', () => {
  const policy = normalizePolicy({ groupCaps: { A: '50' }, pinned: ['AAA'] });
  const [result] = evaluate([plan('AAA', '4000', 'A'), plan('AAB', '2000', 'A'),
    plan('BBB', '4000', 'B')], policy);
  // Only AAB can move: 5000/9000 = 55.6%, then AAB is at its minimum: infeasible.
  assert.equal(result.feasible, false);
  assert.deepEqual(result.changes, []);
  const [external] = evaluate([plan('AAA', '6000', 'A', { adjustable: false }),
    plan('BBB', '4000', 'B')], normalizePolicy({ groupCaps: { A: '50' } }));
  assert.equal(external.feasible, false);
});

test('an impossible ticker cap yields no proposal instead of slashing everything', () => {
  const [result] = evaluate([plan('AAA', '3000', 'A'), plan('BBB', '3000', 'B')],
    normalizePolicy({ tickerCap: '10' }));
  assert.equal(result.feasible, false);
  assert.deepEqual(result.changes, []);
});

test('spare budget is spent only toward owner targets and within caps', () => {
  const plans = [plan('AAA', '2000', 'A'), plan('BBB', '1000', 'B')];
  const [idle] = evaluate(plans, normalizePolicy({ dailyBudget: { KRW: '5000' } }));
  assert.deepEqual(idle.changes, []);
  assert.equal(idle.spareWeekly, '10000');
  const [filled] = evaluate(plans, normalizePolicy({ dailyBudget: { KRW: '5000' },
    groupTargets: { B: '50' } }));
  // B grows 1000 to 2000 (2000/4000 = 50% meets the target); 1000/day stays spare.
  assert.deepEqual(amounts(filled), { BBB: '2000' });
  assert.equal(filled.changes[0].action, '증액');
  assert.equal(filled.spareWeekly, '5000');
  const [capped] = evaluate(plans, normalizePolicy({ dailyBudget: { KRW: '5000' },
    groupTargets: { B: '50' }, groupCaps: { B: '40' } }));
  assert.deepEqual(capped.changes, []);
});

test('currencies are evaluated separately and schedule length counts', () => {
  const results = evaluate([plan('AAA', '2000', 'A'),
    plan('USD1', '3', 'A', { currency: 'USD', days: 1 })], normalizePolicy({}));
  assert.deepEqual(results.map(result => [result.currency, result.before.weekly]),
    [['KRW', '10000'], ['USD', '3']]);
});

test('policy edits validate input and keep a canonical form', () => {
  const groups = ['반도체', '미국 광범위 지수'];
  let policy = normalizePolicy({});
  policy = editPolicy(policy, ['예산', '30,000'], groups).policy;
  policy = editPolicy(policy, ['섹터', '미국', '광범위', '지수', '40%'], groups).policy;
  policy = editPolicy(policy, ['고정', 'qqq,voo'], groups).policy;
  assert.equal(policy.dailyBudget.KRW, '30000');
  assert.equal(policy.groupCaps['미국 광범위 지수'], '40');
  assert.deepEqual(policy.pinned, ['QQQ', 'VOO']);
  assert.ok(editPolicy(policy, ['섹터', '없는군', '30%'], groups).error);
  assert.ok(editPolicy(policy, ['종목', '120%'], groups).error);
  assert.ok(editPolicy(policy, ['예산', '-5'], groups).error);
  assert.deepEqual(editPolicy(policy, ['예산', '없음'], groups).policy.dailyBudget, {});
  assert.throws(() => normalizePolicy({ tickerCap: '0' }), TypeError);
});

test('policy store uses version CAS and append-only revisions', async () => {
  const db = new FakeD1();
  assert.deepEqual(await currentPolicy(db), { version: 0, policy: normalizePolicy({}),
    installed: true });
  const first = normalizePolicy({ tickerCap: '20' });
  const writes = await Promise.all([
    savePolicy(db, { policy: first, expectedVersion: 0, mutationKey: 'fixture-a',
      effectiveAt: '2026-02-01T00:00:00Z' }),
    savePolicy(db, { policy: normalizePolicy({ tickerCap: '30' }), expectedVersion: 0,
      mutationKey: 'fixture-b', effectiveAt: '2026-02-01T00:00:00Z' }),
  ]);
  assert.deepEqual(writes.sort(), ['CHANGED', 'CONFLICT']);
  assert.equal(db.rows('cloud_policy')[0].version, 1);
  assert.equal(db.rows('cloud_policy_revisions').length, 1);
  assert.throws(() => db.sqlite.exec('DELETE FROM cloud_policy_revisions'), /immutable/);
  db.sqlite.exec('DROP TABLE cloud_policy');
  assert.equal((await currentPolicy(db)).installed, false);
});

const account = `fixture_${'c'.repeat(64)}`;
const secret = 'synthetic-secret';
const headers = [['X-Telegram-Bot-Api-Secret-Token', secret]];
const body = (id, text) => new TextEncoder().encode(JSON.stringify({ update_id: id,
  message: { from: { id: 42 }, chat: { id: 42, type: 'private' }, text } }));

async function operating() {
  const db = new FakeD1();
  await importAccount(db, { fingerprint: account, broker: 'synthetic', baseCurrency: 'KRW' });
  const state = (ticker, assetType, dailyAmount) => ({ instrument: { market: 'US', ticker,
    assetType, currency: 'USD' }, accountFingerprint: account, source: 'TELEGRAM',
  amountCurrency: 'KRW', dailyAmount, targetWeight: null, enabled: true,
  timezoneName: 'Asia/Seoul', startedAt: '2026-02-01T00:00:00Z', memo: null });
  for (const [ticker, type, amount] of [['NVDA', 'EQUITY', '4000'], ['AVGO', 'EQUITY', '2000'],
    ['VOO', 'ETF', '4000']]) {
    await createPlanOnce(db, `fixture:${ticker}`, `seed:${ticker}`, state(ticker, type, amount),
      '2026-02-01T00:00:00Z');
  }
  const provider = { name: 'kiwoom_main',
    getPositions: async () => ({ broker: 'kiwoom_main', status: 'ok', items: [] }),
    getCash: async () => ({ broker: 'kiwoom_main', status: 'ok', items: [] }),
    getAccounts: async () => ({ broker: 'kiwoom_main', status: 'ok', items: [] }) };
  const gateway = new SyntheticCloudGateway({ db, providers: [provider],
    scopeAccountFingerprint: account, externalRecurringPlans: async () => [],
    now: () => '2026-02-02T01:00:00Z' });
  const route = (id, command, overrides = {}) => routeUpdate({ headerEntries: headers,
    body: body(id, command), secret, allowedUserIds: [42], gateway, ...overrides });
  return { db, route };
}

test('commands: set policy, review a proposal, apply it once with its token', async () => {
  const { db, route } = await operating();
  const legacy = (await route(300, '/모으기')).reply;
  assert.ok(!legacy.includes('목표·한도 점검'));
  assert.match((await route(301, '/목표')).reply, /설정된 예산·상한·목표가 없습니다/);
  assert.match((await route(302, '/목표 섹터 반도체 50%')).reply, /반도체 상한 50%/);
  assert.match((await route(302, '/목표 섹터 반도체 50%')).reply, /설정 버전 1/);
  assert.equal(db.rows('cloud_policy_revisions').length, 1);
  assert.match((await route(303, '/모으기')).reply, /반도체 60\.0% &gt; 상한 50%/);
  const shown = (await route(304, '/추천')).reply;
  assert.match(shown, /US:NVDA<\/b> 4,000원 → 2,000원/);
  const token = /\/추천적용 ([0-9a-f]{8})/.exec(shown)[1];
  assert.match((await route(305, '/추천적용 00000000')).reply, /변경된 것은 없습니다/);
  assert.equal(db.rows('cloud_plan_revisions').length, 3);
  const applied = await route(306, `/추천적용 ${token}`);
  assert.match(applied.reply, /추천 1종목 반영/);
  const nvda = db.rows('cloud_plans').find(row => row.ticker === 'NVDA');
  assert.equal(nvda.daily_amount, '2000');
  assert.equal(nvda.version, 2);
  assert.equal(db.rows('cloud_plan_revisions').length, 4);
  // Replay of the same update re-renders; the stale token no longer applies.
  assert.match((await route(306, `/추천적용 ${token}`)).reply, /추천 1종목 반영/);
  assert.match((await route(307, `/추천적용 ${token}`)).reply, /변경된 것은 없습니다/);
  assert.equal(db.rows('cloud_plan_revisions').length, 4);
  assert.match((await route(308, '/추천')).reply, /변경할 종목이 없습니다/);
});

test('a plan edited after the proposal invalidates its token', async () => {
  const { db, route } = await operating();
  await route(400, '/목표 섹터 반도체 50%');
  const token = /\/추천적용 ([0-9a-f]{8})/.exec((await route(401, '/추천')).reply)[1];
  assert.equal((await route(402, '/recurring_set fixture:AVGO 3000 KRW ON 1')).status, 'READY');
  assert.match((await route(403, `/추천적용 ${token}`)).reply, /변경된 것은 없습니다/);
  assert.equal(db.rows('cloud_plans').find(row => row.ticker === 'NVDA').daily_amount, '4000');
});

test('atomic delivery stages the policy reply with its revision', async () => {
  const { db, route } = await operating();
  const result = await route(500, '/목표 종목 40%', { atomicDelivery: true,
    now: () => '2026-02-02T01:00:00Z' });
  assert.equal(result.status, 'READY');
  assert.equal(result.mutationAttempted, true);
  assert.equal(db.rows('cloud_outbox').length, 1);
  assert.equal(db.rows('cloud_update_claims').length, 1);
  assert.match(JSON.parse(db.rows('cloud_outbox')[0].payload_json).text, /종목 상한 40%/);
});
