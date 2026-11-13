import assert from 'node:assert/strict';
import { test } from 'node:test';
import { admittedUpdateId, dueJobs } from '../deploy/cloudflare-js/boundary.mjs';
import { aggregateCoverage } from '../deploy/cloudflare-js/aggregation.mjs';
import { decimal, display, evaluateRecurringBuy, weeklyBudget } from '../deploy/cloudflare-js/finance.mjs';

const secret = 'synthetic-secret';
const header = ['X-Telegram-Bot-Api-Secret-Token', secret];
const message = { update_id: 73, message: {
  from: { id: 42 }, chat: { id: 42, type: 'private' },
} };
const encoded = update => new TextEncoder().encode(JSON.stringify(update));

test('webhook admission fails closed on ambiguous and malformed inputs', () => {
  assert.equal(admittedUpdateId([header], encoded(message), secret, [42]), 73);
  assert.equal(admittedUpdateId([header, header], encoded(message), secret, [42]), null);
  assert.equal(admittedUpdateId([header], encoded(message), secret, []), null);
  assert.equal(admittedUpdateId([header], new Uint8Array([255]), secret, [42]), null);
  assert.equal(admittedUpdateId([header], new Uint8Array(65537), secret, [42]), null);
  assert.equal(admittedUpdateId([header], encoded({ ...message, update_id: 2 ** 53 }), secret, [42]), null);
  assert.equal(admittedUpdateId([header], encoded({ update_id: 74, callback_query: {
    from: { id: 42 }, message: { chat: { id: 42, type: 'private' } },
  } }), secret, [42]), 74);
});

test('dates, missing money and incomplete ledger status never become zero', () => {
  assert.deepEqual(dueJobs('2026-10-30T23:30:00Z', 'Asia/Seoul', '08:00', '09:00', 5),
    ['weekly_valuation']);
  assert.deepEqual(dueJobs('2026-10-30T23:30:30Z', 'Asia/Seoul', '08:00', '09:00', 5),
    ['weekly_valuation']);
  assert.throws(() => dueJobs('2026-10-30T23:30:00', 'Asia/Seoul', '08:00', '09:00', 5));
  assert.equal(weeklyBudget([{ enabled: true, dailyAmount: '10', currency: 'USD',
    scheduledWeekdays: [0] }]), null);
  assert.throws(() => weeklyBudget([{ enabled: true, dailyAmount: '10', currency: 'USD',
    scheduledWeekdays: [0] }], '-1400'));
  assert.throws(() => weeklyBudget([{ enabled: true, dailyAmount: '10', currency: 'USD',
    scheduledWeekdays: [0] }], '0'));
  assert.throws(() => weeklyBudget([{ dailyAmount: '10', currency: 'KRW',
    scheduledWeekdays: [0] }]));
  assert.equal(display(decimal('1.005'), 2), '1.01');
  const plan = { enabled: true, dailyAmount: '100', currency: 'KRW',
    instrument: { market: 'US', ticker: 'TEST', assetType: 'ETF', currency: 'KRW' },
    broker: 'fixture', accountId: 'fixture', scheduledWeekdays: [0] };
  const options = { targetDate: '2026-10-26', isTradingDay: true,
    dataComplete: false, cutoffPassed: true };
  assert.equal(evaluateRecurringBuy(plan, [], options).status, 'unverifiable');
  assert.throws(() => evaluateRecurringBuy(plan, [], { ...options, dataComplete: undefined }));
  assert.throws(() => evaluateRecurringBuy(plan, [], { ...options, targetDate: '2026-02-31' }));
  const unrestricted = { ...plan, broker: undefined, accountId: undefined };
  const filled = evaluateRecurringBuy(unrestricted, [{
    transactionId: 'fixture', broker: 'other', accountId: 'other', side: 'BUY',
    tradeDate: '2026-10-26', instrument: plan.instrument, grossAmount: '100',
    currency: 'KRW',
  }], { ...options, dataComplete: true });
  assert.equal(filled.status, 'filled');
  assert.throws(() => aggregateCoverage({ positionResults: [{
    broker: 'fixture', status: 'ok', items: [{ broker: 'fixture', quantity: '1',
      instrument: { market: 'US', currency: 'KRW', assetType: 'ETF' },
      marketValue: '100' }],
  }] }));
  const inverse = aggregateCoverage({ baseCurrency: 'KRW',
    fxQuotes: [{ base: 'KRW', quote: 'USD', rate: '0.0007' }],
    positionResults: [{ broker: 'fixture', status: 'ok', items: [{
      broker: 'fixture', accountId: 'synthetic', quantity: '1',
      instrument: { market: 'US', ticker: 'TEST', assetType: 'ETF', currency: 'USD' },
      marketValue: '100',
    }] }],
  });
  assert.equal(inverse.isComplete, true);
  assert.equal(inverse.totalMarketValue, '142857.1428571428571428571429');
});
