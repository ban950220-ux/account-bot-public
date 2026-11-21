import assert from 'node:assert/strict';
import { test } from 'node:test';
import { FakeD1 } from './cloud_js_fake_d1.mjs';
import { importAccount } from '../deploy/cloudflare-js/storage.mjs';
import { SyntheticCloudGateway } from '../deploy/cloudflare-js/cloud-gateway.mjs';
import { createPlanOnce } from '../deploy/cloudflare-js/cloud-write.mjs';
import { routeUpdate } from '../deploy/cloudflare-js/command-router.mjs';
import { parseKstMoment, momentArgs } from '../deploy/cloudflare-js/plan-history.mjs';

const account = `fixture_${'c'.repeat(64)}`;
const secret = 'synthetic-secret';
const headers = [['X-Telegram-Bot-Api-Secret-Token', secret]];
const body = (id, text) => new TextEncoder().encode(JSON.stringify({ update_id: id,
  message: { from: { id: 42 }, chat: { id: 42, type: 'private' }, text } }));

// NVDA 4000, AVGO 2000, VOO 4000 created 2026-02-01 00:00Z. Each command runs at `clock.now`.
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
  const clock = { now: '2026-02-01T00:00:00Z' };
  const gateway = new SyntheticCloudGateway({ db, providers: [],
    scopeAccountFingerprint: account, externalRecurringPlans: async () => [],
    now: () => clock.now });
  const route = (id, command, overrides = {}) => routeUpdate({ headerEntries: headers,
    body: body(id, command), secret, allowedUserIds: [42], gateway, ...overrides });
  const plan = ticker => db.rows('cloud_plans').find(row => row.ticker === ticker);
  const revisions = () => db.rows('cloud_plan_revisions').length;
  // Day 1 (KST 2/2 10:00): NVDA 3000, AVGO OFF. Day 2 (KST 2/3 10:00): NVDA 5000, TSLA added.
  clock.now = '2026-02-02T01:00:00Z';
  assert.match((await route(1, '/모으기설정 NVDA 3000\n/모으기설정 AVGO OFF')).reply, /2종목 변경/);
  clock.now = '2026-02-03T01:00:00Z';
  assert.match((await route(2, '/모으기설정 NVDA 5000')).reply, /5,000원 · ON/);
  assert.equal((await route(3, '/모으기추가 US:TSLA 2000KRW 5%')).status, 'READY');
  clock.now = '2026-02-04T01:00:00Z';
  return { db, route, plan, revisions, clock };
}

test('KST moments: a date alone is the end of that day; bad dates are refused', () => {
  const now = new Date('2026-11-21T00:00:00Z');
  assert.equal(parseKstMoment('2026-11-20', null, now).at.toISOString(), '2026-11-20T14:59:59.000Z');
  assert.equal(parseKstMoment('11/20', '08:00', now).at.toISOString(), '2026-11-19T23:00:00.000Z');
  assert.equal(parseKstMoment('11/20', '08:00:30', now).label, '2026-11-20 08:00 KST');
  // A short date later than today belongs to last year.
  assert.equal(parseKstMoment('12/25', null, now).at.toISOString(), '2025-12-25T14:59:59.000Z');
  for (const [date, time] of [['2/30', null], ['2026-13-01', null], ['11/20', '24:00'],
    ['11/20', '8'], ['yesterday', null]]) {
    assert.equal(parseKstMoment(date, time, now), null, `${date} ${time}`);
  }
  assert.equal(momentArgs(new Date('2026-11-19T23:00:00Z')), '2026-11-20 08:00:00');
});

test('a past moment is previewed, then applied once with its token', async () => {
  const { route, plan, revisions } = await operating();
  const preview = (await route(10, '/모으기시점 2026-02-02')).reply;
  assert.match(preview, /^🕘 <b>2026-02-02 23:59 KST 시점과 비교<\/b> \(다른 계획 2개\)/);
  assert.match(preview, /<b>NVDA<\/b> 5,000원 ON → 3,000원 ON/);
  assert.match(preview, /<b>TSLA<\/b> 2,000원 ON → \(그때 없던 계획\) OFF/);
  assert.ok(!preview.includes('AVGO') && !preview.includes('VOO'));
  // Weekly KRW: now 5000+4000+2000 = 11000/day, then 3000+4000 = 7000/day.
  assert.match(preview, /주간 합계 55,000원 → 35,000원/);
  const before = revisions();
  assert.equal(plan('NVDA').daily_amount, '5000');
  const command = /반영: <code>(\/모으기시점 [^<]+)<\/code>/.exec(preview)[1];
  assert.match(command, /^\/모으기시점 2026-02-02 23:59:59 [0-9a-f]{8}$/);

  assert.match((await route(11, command.replace(/[0-9a-f]{8}$/, '00000000'))).reply,
    /확인 번호가 지금 계산과 맞지 않습니다/);
  assert.equal(revisions(), before);

  const applied = await route(12, command);
  assert.equal(applied.status, 'READY');
  assert.match(applied.reply, /2026-02-02 23:59 KST 시점으로 2종목 되돌림/);
  assert.match(applied.reply, /NVDA 3,000원 ON · TSLA 2,000원 OFF/);
  assert.equal(plan('NVDA').daily_amount, '3000');
  assert.equal(plan('TSLA').enabled, 0);
  assert.equal(revisions(), before + 2);
  // Replay of the same update re-renders without writing; the moment now matches.
  assert.match((await route(12, command)).reply, /2종목 되돌림/);
  assert.equal(revisions(), before + 2);
  assert.match((await route(13, '/모으기시점 2026-02-02')).reply, /바꿀 것이 없습니다/);
});

test('a token goes stale when a plan changes after the preview', async () => {
  const { route, plan } = await operating();
  const preview = (await route(20, '/모으기시점 2026-02-02')).reply;
  const command = /반영: <code>(\/모으기시점 [^<]+)<\/code>/.exec(preview)[1];
  await route(21, '/모으기설정 VOO 6000');
  assert.match((await route(22, command)).reply, /변경된 것은 없습니다/);
  assert.equal(plan('NVDA').daily_amount, '5000');
});

test('before any plan existed everything goes OFF; archived plans are left alone', async () => {
  const { route, plan } = await operating();
  await route(30, '/모으기삭제 VOO');
  const preview = (await route(31, '/모으기시점 2026-01-15')).reply;
  assert.match(preview, /다른 계획 2개/);
  assert.match(preview, /<b>NVDA<\/b> 5,000원 ON → \(그때 없던 계획\) OFF/);
  assert.ok(!preview.includes('VOO'), 'archived plan excluded');
  assert.ok(!preview.includes('AVGO'), 'already OFF');
  const command = /반영: <code>(\/모으기시점 [^<]+)<\/code>/.exec(preview)[1];
  await route(32, command);
  assert.equal(plan('NVDA').enabled, 0);
  assert.equal(plan('NVDA').daily_amount, '5000');
});

test('history lists change events with a way back, and one plan in detail', async () => {
  const { route, plan, clock } = await operating();
  const history = (await route(40, '/모으기이력')).reply;
  // Newest first. Events group revisions by their exact time; this fixture clock runs both
  // 2/3 commands at the same instant, so they show as one (production times carry ms).
  const times = [...history.matchAll(/<b>(\d\d\/\d\d \d\d:\d\d)<\/b>/g)].map(match => match[1]);
  assert.deepEqual(times, ['02/03 10:00', '02/02 10:00', '02/01 09:00']);
  assert.match(history, /<b>02\/03 10:00<\/b> · 2종목 · NVDA, TSLA/);
  assert.match(history, /<b>02\/01 09:00<\/b> · 3종목 · AVGO, NVDA, VOO/);
  assert.match(history, /<b>02\/02 10:00<\/b> · 2종목 · AVGO, NVDA\n이 변경 직전으로: <code>\/모으기시점 2026-02-02 09:59:59<\/code>/);

  // Going back to just before 2/2 10:00 restores the seeds and turns TSLA OFF.
  const preview = (await route(41, '/모으기시점 2026-02-02 09:59:59')).reply;
  assert.match(preview, /<b>AVGO<\/b> 2,000원 OFF → 2,000원 ON/);
  assert.match(preview, /<b>NVDA<\/b> 5,000원 ON → 4,000원 ON/);
  clock.now = '2026-02-05T01:00:00Z';
  await route(42, /반영: <code>(\/모으기시점 [^<]+)<\/code>/.exec(preview)[1]);
  assert.equal(plan('NVDA').daily_amount, '4000');
  assert.equal(plan('AVGO').enabled, 1);

  const nvda = (await route(43, '/모으기이력 NVDA')).reply;
  assert.match(nvda, /^🗂 <b>US:NVDA<\/b> 최근 변경\n<code>fixture:NVDA<\/code>\n\n02\/05 10:00 · 4,000원 ON\n02\/03 10:00 · 5,000원 ON\n02\/02 10:00 · 3,000원 ON\n02\/01 09:00 · 4,000원 ON$/);
  assert.match((await route(44, '/모으기이력 ZZZZ')).reply, /찾지 못했습니다/);
  assert.match((await route(45, '/모으기이력 A B')).reply, /사용법/);
});

test('moment input is validated', async () => {
  const { route } = await operating();
  for (const text of ['/모으기시점', '/모으기시점 어제', '/모으기시점 2/30', '/모으기시점 1 2 3']) {
    assert.match((await route(50, text)).reply, /사용법/, text);
  }
  assert.match((await route(51, '/모으기시점 2099-01-01')).reply, /미래 시점/);
});
