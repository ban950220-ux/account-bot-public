import assert from 'node:assert/strict';
import { test } from 'node:test';
import { FakeD1 } from './cloud_js_fake_d1.mjs';
import { createSignals, signalSymbol, trendFromWeeklyCloses, valuationFromTimeseries } from
  '../deploy/cloudflare-js/signals.mjs';
import { editPolicy, evaluate, minimumFor, normalizePolicy } from
  '../deploy/cloudflare-js/allocation-policy.mjs';
import { formatRecurring } from '../deploy/cloudflare-js/recurring-report.mjs';

const instrument = (ticker, assetType = 'EQUITY') => ({ market: 'US', ticker, assetType,
  currency: 'USD' });
const stored = (ticker, assetType) => ({ instrument: instrument(ticker, assetType), enabled: true,
  currency: 'KRW', dailyAmount: '2000', scheduledWeekdays: [0, 1, 2, 3, 4] });

function series(type, values) {
  return { meta: { type: [type] }, [type]: values.map((raw, index) => ({
    asOfDate: `2026-${String(index + 1).padStart(2, '0')}-28`, reportedValue: { raw } })) };
}
const timeseries = { timeseries: { result: [
  series('trailingForwardPeRatio', [30, 28, 26, 24, 20]),
  series('trailingPegRatio', [1.2]), series('trailingEnterprisesValueEBITDARatio', [18]),
  series('trailingMarketCap', [1000]), series('annualFreeCashFlow', [50]),
  series('quarterlyDilutedAverageShares', [100, 100, 101, 101, 102]),
] } };
const rising = Array.from({ length: 261 }, (_, index) => 100 * 1.002 ** index);

function fakeYahoo(calls, { failing = new Set() } = {}) {
  return async (url, init) => {
    calls.push(url);
    // The live host rejects requests without a browser-style User-Agent.
    if (!/^Mozilla\//.test(init?.headers?.['user-agent'] || '')) {
      return { ok: false, status: 429, headers: new Map() };
    }
    const symbol = decodeURIComponent(/\/(?:chart|timeseries)\/([^?]+)/.exec(url)[1]);
    if (failing.has(symbol)) return { ok: false, status: 404, headers: new Map() };
    const body = url.includes('/v8/finance/chart/') ? { chart: { result: [{
      meta: { instrumentType: symbol === 'FUND' ? 'ETF' : 'EQUITY' },
      indicators: { adjclose: [{ adjclose: rising }] } }] } } : timeseries;
    return { ok: true, headers: new Map(), json: async () => body };
  };
}

test('trend and valuation are derived from public series only', () => {
  const trend = trendFromWeeklyCloses(rising);
  assert.equal(trend.observationCount, 261);
  assert.equal(trend.above200dAverage, true);
  assert.equal(trend.maxDrawdown3y, '0.00000000');
  assert.ok(Number(trend.return1y) > 0.10 && Number(trend.return1y) < 0.12);
  assert.ok(Number(trend.cagr3y) > 0.10 && Number(trend.cagr3y) < 0.12);
  assert.equal(trendFromWeeklyCloses(rising.slice(0, 40)), null);
  const valuation = valuationFromTimeseries(timeseries);
  assert.equal(valuation.forwardPe, '20.00000000');
  assert.equal(valuation.historicalForwardPeMedian, '26.00000000');
  assert.equal(valuation.freeCashFlowYield, '0.05000000');
  assert.equal(valuation.dilutionPct, '2.00000000');
  assert.equal(valuationFromTimeseries({ timeseries: { result: [] } }), null);
  assert.equal(signalSymbol(instrument('PBR.A')), 'PBR-A');
  assert.throws(() => signalSymbol(instrument('BAD TICKER')), TypeError);
});

test('refresh spends a bounded request budget and resumes on the next call', async () => {
  const db = new FakeD1();
  const calls = [];
  let clock = '2026-11-13T00:00:00.000Z';
  const signals = createSignals({ db, fetcher: fakeYahoo(calls, { failing: new Set(['BAD']) }),
    now: () => clock });
  const plans = [stored('AAA'), stored('BBB'), stored('FUND', 'ETF'), stored('BAD')];
  // Budget 3: one company (2 requests) and the fund (1); the rest wait.
  const first = await signals.refresh(plans, 3);
  assert.deepEqual(first, { symbols: 4, refreshed: 2, failed: 0, remaining: 2, reasons: [] });
  assert.equal(calls.length, 3);
  const second = await signals.refresh(plans, 16);
  assert.equal(second.refreshed, 1);
  assert.equal(second.failed, 1);
  assert.deepEqual(second.reasons, ['HTTP 404']);
  assert.equal(second.remaining, 1);
  const read = await signals.read(plans);
  assert.equal(read.size, 3);
  assert.equal(read.get('US:FUND:ETF:USD').valuationScore, null);
  assert.ok(Number(read.get('US:FUND:ETF:USD').trendScore) > 50);
  assert.ok(Number(read.get('US:AAA:EQUITY:USD').valuationScore) > 50);
  const before = calls.length;
  assert.equal((await signals.refresh(plans.slice(0, 3), 16)).refreshed, 0);
  assert.equal(calls.length, before);
  // After a week the entries are stale but still readable; after three weeks they are gone.
  clock = '2026-11-23T00:00:00.000Z';
  assert.equal((await signals.read(plans)).get('US:AAA:EQUITY:USD').stale, true);
  clock = '2026-12-10T00:00:00.000Z';
  assert.equal((await signals.read(plans)).size, 0);
  await assert.rejects(() => signals.refresh(plans, 99), TypeError);
});

const plan = (ticker, dailyAmount, group, extra = {}) => ({ id: `k:${ticker}`,
  planKey: `k:${ticker}`, version: 1, ticker, label: `US:${ticker}`, currency: 'KRW',
  dailyAmount, days: 5, adjustable: true, groups: [[group, '1']], broker: 'kiwoom_screenshot',
  signal: null, ...extra });
const amounts = result => Object.fromEntries(result.changes.map(change =>
  [change.plan.ticker, change.to]));

test('valuation moves an amount by one step and respects minimums and caps', () => {
  const policy = normalizePolicy({});
  const [result] = evaluate([
    plan('CHEAP', '2000', 'A', { signal: { valuationScore: '82.5' } }),
    plan('DEAR', '3000', 'B', { signal: { valuationScore: '20' } }),
    plan('FLOOR', '2000', 'C', { signal: { valuationScore: '10' } }),
    plan('MID', '2000', 'D', { signal: { valuationScore: '50' } }),
    plan('FUND', '2000', 'E', { signal: { valuationScore: null, trendScore: '90' } }),
  ], policy);
  assert.equal(result.feasible, true);
  assert.deepEqual(amounts(result), { CHEAP: '3000', DEAR: '2000' });
  assert.deepEqual(result.heldAtMinimum, ['US:FLOOR 10.0점']);
  assert.match(result.changes.find(change => change.plan.ticker === 'CHEAP').reasons[0],
    /Valuation 82\.5점 · 가격 매력/);
  const capped = evaluate([plan('CHEAP', '2000', 'A', { signal: { valuationScore: '90' } }),
    plan('OTHER', '2000', 'B')], normalizePolicy({ tickerCap: '50' }));
  assert.deepEqual(capped[0].changes, []);
  const overBudget = evaluate([plan('CHEAP', '2000', 'A', { signal: { valuationScore: '90' } })],
    normalizePolicy({ dailyBudget: { KRW: '2000' } }));
  assert.deepEqual(overBudget[0].changes, []);
});

test('minimum amounts follow the broker unless the owner overrides them', () => {
  const kiwoom = plan('K', '2000', 'A');
  const meritz = plan('M', '2000', 'A', { broker: 'meritz' });
  assert.equal(minimumFor(kiwoom, normalizePolicy({})), '2000');
  assert.equal(minimumFor(meritz, normalizePolicy({})), '1000');
  const global = normalizePolicy({ minimum: { KRW: '3000' } });
  assert.equal(minimumFor(meritz, global), '3000');
  const edited = editPolicy(global, ['최소', '메리츠', '1,000'], []).policy;
  assert.equal(minimumFor(meritz, edited), '1000');
  assert.equal(minimumFor(kiwoom, edited), '3000');
  assert.deepEqual(editPolicy(edited, ['최소', '메리츠', '없음'], []).policy.brokerMinimum, {});
  // Only the Meritz plan may drop below 2,000 to satisfy the cap.
  const [result] = evaluate([kiwoom, meritz, plan('B', '2000', 'B')],
    normalizePolicy({ groupCaps: { A: '60' } }));
  assert.deepEqual(amounts(result), { M: '1000' });
});

test('the recurring report shows cached scores on one line', () => {
  const plans = [stored('AAA'), stored('FUND', 'ETF')];
  const signals = new Map([['US:AAA:EQUITY:USD', { valuationScore: '61.25', trendScore: '70' }],
    ['US:FUND:ETF:USD', { valuationScore: null, trendScore: '88.04' }]]);
  const report = formatRecurring(plans, {}, signals);
  assert.match(report, /Valuation 61\.3 \/ 100 · 장기추세 70\.0 \/ 100/);
  assert.match(report, /\n장기추세 88\.0 \/ 100/);
  assert.ok(!report.includes('종목 분석 데이터가 없어'));
  assert.ok(formatRecurring(plans, {}).includes('종목 분석 데이터가 없어'));
});
