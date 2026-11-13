import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createExternalRequestBudget } from '../deploy/cloudflare-js/external-budget.mjs';

test('provider and Telegram transports share a cap and cannot follow uncounted redirects', async () => {
  const budget = createExternalRequestBudget(3);
  const seen = [];
  const transport = label => budget.wrap(async (_url, options) => {
    seen.push([label, options.redirect]);
    return { ok: true };
  });
  const provider = transport('provider');
  const telegram = transport('telegram');
  await provider('https://fixture.invalid', { redirect: 'follow' });
  await provider('https://fixture.invalid');
  budget.assertAvailable(1);
  assert.throws(() => budget.assertAvailable(2), /external request budget/);
  await telegram('https://fixture.invalid');
  await assert.rejects(() => telegram('https://fixture.invalid'), /external request budget/);
  assert.equal(budget.used, 3);
  assert.deepEqual(seen, [['provider', 'manual'], ['provider', 'manual'], ['telegram', 'manual']]);
});

test('failed external attempts consume the shared cap and a swallowed overflow stays fatal', async () => {
  const budget = createExternalRequestBudget(1);
  let calls = 0;
  const provider = budget.wrap(async () => { calls++; throw new Error('synthetic outage'); });
  await assert.rejects(() => provider('https://fixture.invalid'), /outage/);
  await assert.rejects(() => provider('https://fixture.invalid'), /external request budget/);
  assert.equal(calls, 1);
  assert.throws(() => budget.assertAvailable(0), /external request budget/);
});
