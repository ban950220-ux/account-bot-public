import assert from 'node:assert/strict';
import { test } from 'node:test';
import { routeUpdate } from '../deploy/cloudflare-js/command-router.mjs';
import { aggregateCoverage } from '../deploy/cloudflare-js/aggregation.mjs';
import { START } from '../deploy/cloudflare-js/static-commands.mjs';

const secret = 'synthetic-secret';
const headerEntries = [['X-Telegram-Bot-Api-Secret-Token', secret]];
const encoder = new TextEncoder();
function message(command, userId = 42, updateId = 100) {
  return encoder.encode(JSON.stringify({ update_id: updateId, message: {
    from: { id: userId }, chat: { id: userId, type: 'private' }, text: command,
  } }));
}
const request = (body, gateway, extra = {}) => routeUpdate({ headerEntries, body,
  secret, allowedUserIds: [42], gateway, ...extra });

test('auth rejects another user before any gateway access', async () => {
  let reads = 0;
  const gateway = { refresh: async () => { reads += 1; throw new Error('unreachable'); } };
  assert.deepEqual(await request(message('/assets', 43), gateway),
    { status: 'DENIED', updateId: null, reply: null });
  assert.equal(reads, 0);
  const missing = await routeUpdate({ headerEntries: [], body: message('/start'),
    secret, allowedUserIds: [42], gateway });
  assert.equal(missing.status, 'DENIED');
});

test('button callback payloads cannot mutate the command-only prototype', async () => {
  let mutations = 0;
  const body = encoder.encode(JSON.stringify({ update_id: 102, callback_query: {
    from: { id: 42 }, message: { chat: { id: 42, type: 'private' } },
    data: 'edit:save:plan-1',
  } }));
  const result = await request(body, { changeGroup: async () => { mutations += 1; } });
  assert.equal(result.status, 'IGNORED');
  assert.equal(mutations, 0);
});

test('actual registered command aliases and invalid input use Python reply boundaries', async () => {
  assert.equal((await request(message('/start'), {})).reply, START);
  assert.equal((await request(message('/도움말'), {})).status, 'READY');
  assert.equal((await request(message('/recurring_add KOSPI:005930 1USD 8%'), {})).reply,
    '한국 종목 적립금은 KRW로 입력해 주세요.');
  assert.equal((await request(message('/analyze'), {})).reply,
    '사용법: <code>/analyze AMD</code>');
  assert.equal((await request(message('/analyze TEST'), {})).reply,
    '시장데이터 provider가 설정되지 않아 분석을 보류합니다.');
  assert.equal((await request(message('/unknown'), {})).status, 'IGNORED');
  assert.equal((await request(message('/start@other_bot'), {},
    { botUsername: 'actual_bot' })).status, 'IGNORED');
});

test('partial source refresh is visibly incomplete and DB errors remain generic', async () => {
  const positionResults = [{ broker: 'kiwoom_main', status: 'partial',
    safeErrorType: 'SyntheticMissing', items: [] }];
  const snapshot = aggregateCoverage({ baseCurrency: 'KRW',
    expectedBrokers: ['kiwoom_main'], positionResults });
  const gateway = { refresh: async () => ({ snapshot, positionResults,
    cashResults: [], collectedAt: '2026-02-02T01:00:00Z', fxQuote: null }) };
  const partial = await request(message('/assets'), gateway);
  assert.equal(partial.status, 'READY');
  assert.match(partial.reply, /부분 합계/);
  const failed = await request(message('/assets'), { refresh: async () => {
    throw new Error('synthetic private database detail');
  } });
  assert.equal(failed.status, 'ERROR');
  assert.equal(failed.reply.includes('private database'), false);
});

test('cancellation prevents a plan mutation and completed writes are acknowledged', async () => {
  let writes = 0;
  const gateway = { createPlan: async ({ market, ticker }) => {
    writes += 1;
    return { instrument: { market, ticker }, dailyAmount: '20', currency: 'USD',
      targetWeightPct: '8' };
  } };
  const controller = new AbortController();
  controller.abort();
  const cancelled = await request(message('/recurring_add TEST 20USD 8%'), gateway,
    { signal: controller.signal });
  assert.equal(cancelled.status, 'CANCELLED');
  assert.equal(writes, 0);
  const success = await request(message('/recurring_add TEST 20USD 8%'), gateway);
  assert.equal(success.status, 'READY');
  assert.match(success.reply, /주문은 실행되지 않습니다/);
  assert.equal(writes, 1);
});
