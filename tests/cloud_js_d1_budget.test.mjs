import assert from 'node:assert/strict';
import { test } from 'node:test';
import { FakeD1 } from './cloud_js_fake_d1.mjs';
import { createD1QueryBudget } from '../deploy/cloudflare-js/d1-budget.mjs';

test('D1 budget counts bound/unbound operations and a multi-statement batch once', async () => {
  const fixture = new FakeD1();
  const budget = createD1QueryBudget(fixture, 4);
  const db = budget.db;
  assert.equal((await db.prepare('SELECT 1 AS n').first()).n, 1);
  assert.equal((await db.prepare('SELECT ? AS n').bind(2).first()).n, 2);
  const rows = await db.batch(Array.from({ length: 100 }, (_, i) => db.prepare('SELECT ? AS n').bind(i)));
  assert.equal(rows.length, 100);
  assert.equal(budget.used, 3);
  budget.assertAvailable(1);
  assert.throws(() => budget.assertAvailable(2), /budget unavailable/);
  await db.prepare('SELECT 3 AS n').all();
  await assert.rejects(() => db.prepare('SELECT 4 AS n').all(), /budget unavailable/);
  assert.equal(budget.used, 4);
});

test('failed and concurrent operations consume reservations without crossing the cap', async () => {
  const fixture = new FakeD1();
  const budget = createD1QueryBudget(fixture, 3);
  await assert.rejects(() => budget.db.prepare('SELECT * FROM absent').all());
  const settled = await Promise.allSettled(Array.from({ length: 3 }, () =>
    budget.db.prepare('SELECT 1').all()));
  assert.equal(settled.filter(item => item.status === 'fulfilled').length, 2);
  assert.equal(budget.used, 3);
});
