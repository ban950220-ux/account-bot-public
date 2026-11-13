import assert from 'node:assert/strict';
import { test } from 'node:test';
import { importAccount, importPlanHistory, recordTransactionOnce, storeSnapshot } from
  '../deploy/cloudflare-js/storage.mjs';
import { FakeD1 } from './cloud_js_fake_d1.mjs';
import { formatAccounts, formatPortfolio } from '../deploy/cloudflare-js/portfolio-report.mjs';
import { formatAssets } from '../deploy/cloudflare-js/assets-report.mjs';
import { rebalanceReply } from '../deploy/cloudflare-js/dynamic-commands.mjs';
import { buildPeriodicReport } from '../deploy/cloudflare-js/periodic-report.mjs';

const account = `fixture_${'a'.repeat(64)}`;
const secondAccount = `fixture_${'b'.repeat(64)}`;
const instrument = { market: 'US', ticker: 'TEST', assetType: 'ETF', currency: 'USD' };

async function seeded() {
  const db = new FakeD1();
  assert.equal(await importAccount(db, {
    fingerprint: account, broker: 'synthetic', baseCurrency: 'KRW',
    maskedLabel: '***fixture',
  }), true);
  return db;
}

test('plan transfer preserves version, source and append-only revisions', async () => {
  const db = await seeded();
  const plan = { planKey: 'manual:US:TEST', accountFingerprint: account,
    source: 'TELEGRAM', instrument, amountCurrency: 'KRW', dailyAmount: '5000',
    targetWeight: '0.1', enabled: true, timezoneName: 'Asia/Seoul',
    startedAt: '2026-01-01T00:00:00Z', memo: '[schedule=MON]', version: 2 };
  const revisions = [
    { revisionNo: 1, changeHash: 'a'.repeat(64), source: 'TELEGRAM',
      accountFingerprint: account, enabled: true, dailyAmount: '2000',
      targetWeight: '0.1', effectiveFrom: '2026-01-01T00:00:00Z', memo: null,
      instrument: { ...instrument, ticker: 'OLD' }, timezoneName: 'UTC',
      startedAt: '2025-12-01T00:00:00Z' },
    { revisionNo: 2, changeHash: 'b'.repeat(64), source: 'TELEGRAM',
      accountFingerprint: account, enabled: true, dailyAmount: '5000',
      targetWeight: '0.1', effectiveFrom: '2026-02-01T00:00:00Z',
      memo: '[schedule=MON]', instrument, timezoneName: 'Asia/Seoul',
      startedAt: '2026-01-01T00:00:00Z' },
  ];
  await importPlanHistory(db, plan, revisions);
  assert.equal(db.rows('cloud_plans')[0].daily_amount, '5000');
  assert.equal(db.rows('cloud_plans')[0].version, 2);
  assert.equal(db.rows('cloud_plan_revisions').length, 2);
  assert.equal(db.rows('cloud_plan_revisions')[0].ticker, 'OLD');
  assert.equal(db.rows('cloud_plan_revisions')[0].timezone_name, 'UTC');
  await assert.rejects(() => importPlanHistory(db, plan, revisions));
  assert.equal(db.rows('cloud_plan_revisions').length, 2);
  await assert.rejects(() => importPlanHistory(new FakeD1(),
    { ...plan, accountFingerprint: '12345678' }, revisions));
  await assert.rejects(() => importAccount(db, { fingerprint: secondAccount,
    broker: 'synthetic', baseCurrency: 'KRW', maskedLabel: '12345678' }));
  await assert.rejects(() => importAccount(db, { fingerprint: secondAccount,
    broker: 'synthetic', baseCurrency: 'KRW', maskedLabel: 'Account 1234 5678' }));
  await assert.rejects(() => importAccount(db, { fingerprint: secondAccount,
    broker: 'synthetic', baseCurrency: 'KRW', maskedLabel: '***1234567890123456' }));
});

test('transaction key is scoped to account and duplicate never creates a new ledger row', async () => {
  const db = await seeded();
  await importAccount(db, { fingerprint: secondAccount, broker: 'synthetic',
    baseCurrency: 'KRW' });
  const row = { accountFingerprint: account, sourceKey: 'fill-1', source: 'synthetic',
    instrument, currency: 'USD', transactionType: 'BUY', status: 'FILLED',
    tradedAt: '2026-02-02T00:00:00Z', settledAt: null, quantity: '0.1',
    unitPrice: '10.125', grossAmount: '1.0125', fee: '0', tax: '0',
    netAmount: '1.0125' };
  assert.equal(await recordTransactionOnce(db, row), true);
  assert.equal(await recordTransactionOnce(db, row), false);
  await assert.rejects(() => recordTransactionOnce(db,
    { ...row, grossAmount: '999' }), /source key conflict/);
  assert.equal(await recordTransactionOnce(db, { ...row, accountFingerprint: secondAccount }), true);
  assert.equal(db.rows('cloud_transactions').length, 2);
  assert.equal(db.rows('cloud_transactions')[0].gross_amount, '1.0125');
  await assert.rejects(() => recordTransactionOnce(db, { ...row, accountFingerprint: '12345' }));
});

test('partial snapshot preserves source failure and never records a complete total', async () => {
  const db = await seeded();
  const snapshot = { key: 'daily:fixture', capturedAt: '2026-02-02T01:00:00Z',
    dataAsOf: '2026-02-02T00:59:00Z', finalizedAt: '2026-02-02T01:01:00Z',
    status: 'PARTIAL', baseCurrency: 'KRW', totalMarketValue: null,
    totalCostBasis: null, sources: [
      { key: 'kiwoom_main:positions', accountFingerprint: account,
        status: 'SUCCESS', observedAt: '2026-02-02T00:59:00Z', safeErrorType: null },
      { key: 'meritz:positions', accountFingerprint: null,
        status: 'FAILED', observedAt: null, safeErrorType: 'SnapshotUnavailable' },
    ], positions: [{ accountFingerprint: account, instrument, quantity: '1',
      costBasis: '10.2', marketValue: '11.3', valueInBase: null }],
    cash: [{ accountFingerprint: account, currency: 'KRW', amount: '100',
      valueInBase: '100' }] };
  await storeSnapshot(db, snapshot);
  assert.equal(db.rows('cloud_snapshots')[0].total_market_value, null);
  assert.deepEqual(db.rows('cloud_snapshot_sources').map(row => row.status),
    ['SUCCESS', 'FAILED']);
  assert.equal(db.rows('cloud_position_snapshots')[0].market_value, '11.3');
  await assert.rejects(() => storeSnapshot(db, snapshot));
  assert.equal(db.rows('cloud_snapshots').length, 1);
  await assert.rejects(() => storeSnapshot(db, { ...snapshot, key: 'bad',
    totalMarketValue: '111.3' }));
  await assert.rejects(() => storeSnapshot(db, { ...snapshot, key: 'false-complete',
    status: 'COMPLETE', totalMarketValue: '111.3' }));
  await assert.rejects(() => storeSnapshot(db, { ...snapshot, key: 'wrong-total',
    status: 'COMPLETE', sources: snapshot.sources.slice(0, 1),
    positions: [{ ...snapshot.positions[0], valueInBase: '11.3' }],
    totalMarketValue: '111.4' }));
  assert.equal(db.rows('cloud_snapshots').length, 1);
});

test('account identifiers stay hidden and combined sources have an honest title', () => {
  const accounts = formatAccounts([{ broker: 'kiwoom_main', status: 'ok', items: [
    { displayName: 'Account 1234 5678', accountId: '12345678' },
  ] }], '2026-02-02T01:00:00Z', true);
  assert.equal(accounts.includes('1234'), false);
  assert.equal(accounts.includes('12345678'), false);
  const snapshot = { expectedBrokers: ['kiwoom_main', 'meritz'], isComplete: true,
    totalMarketValue: '0', includedMarketValue: '0', totalCostBasis: null,
    baseCurrency: 'KRW',
    positions: [], cashBalances: [] };
  assert.match(formatPortfolio(snapshot), /선택 출처 포트폴리오/);
  assert.match(formatAssets(snapshot, { positionResults: [], cashResults: [],
    collectedAt: '2026-02-02T01:00:00Z' }), /선택 출처 전체자산/);
});

test('rebalance reply fails closed without targets or a complete valuation', () => {
  const target = { enabled: true, instrument, targetWeightPct: '100' };
  const snapshot = { baseCurrency: 'USD', isComplete: false, totalMarketValue: null,
    positions: [] };
  assert.match(rebalanceReply('NaN', snapshot, [target]), /사용법/);
  assert.match(rebalanceReply('-1', snapshot, [target]), /0 이상/);
  assert.match(rebalanceReply('100', snapshot, []), /목표 비중/);
  assert.match(rebalanceReply('100', snapshot, [target]), /제안 보류/);
});

test('nonempty weekly report withholds missing FX and rejects incomplete analysis', () => {
  const snapshot = { expectedBrokers: ['kiwoom_main'], isComplete: true,
    totalMarketValue: '0', includedMarketValue: '0', totalCostBasis: null,
    baseCurrency: 'KRW', positions: [], cashBalances: [] };
  const refresh = { positionResults: [{ broker: 'kiwoom_main', status: 'ok', items: [] }],
    cashResults: [], fxQuote: null, collectedAt: '2026-02-02T01:00:00Z' };
  const plans = [{ instrument, dailyAmount: '10', currency: 'USD', enabled: true,
    broker: 'synthetic', scheduledWeekdays: [0], targetWeightPct: null }];
  assert.match(buildPeriodicReport('weekly', snapshot, refresh, plans), /배분안은 보류/);
  assert.throws(() => buildPeriodicReport('daily', snapshot, refresh, plans,
    { 'US:TEST:ETF:USD': { finalScore: {} } }), /incomplete recurring analysis/);
});
