import assert from 'node:assert/strict';
import { test } from 'node:test';
import harness from '../deploy/cloudflare-js/synthetic-combined-harness.mjs';
import frozen from '../artifacts/cloudflare-test/account-bot-cloud-test-20261027.mjs';
import { FakeD1 } from './cloud_js_fake_d1.mjs';

test('frozen bundle reproduces shared-D1 collision without changing its failed run', async () => {
  const db = new FakeD1();
  for (const [name, status] of [['core', 'PASS'], ['small', 'PASS'],
    ['representative', 'FAIL']]) {
    const env = { DB: db, TEST_CASE: name, TEST_WRITES_ENABLED: '1' };
    await frozen.scheduled({}, env);
    assert.deepEqual(await (await frozen.fetch(
      new Request('https://fixture.invalid/health'), env)).json(), { status });
  }
});

test('v2 cases run beside preserved v1 PASS and FAIL rows on one D1', async () => {
  const db = new FakeD1();
  for (const name of ['core', 'small', 'representative']) {
    await frozen.scheduled({}, { DB: db, TEST_CASE: name, TEST_WRITES_ENABLED: '1' });
  }
  const v1 = db.sqlite.prepare(`SELECT run_key, status FROM cloud_test_runs
    ORDER BY run_key`).all();
  assert.deepEqual(v1.map(row => [row.run_key, row.status]), [
    ['commands-v1:representative', 'FAIL'], ['commands-v1:small', 'PASS'],
    ['synthetic-v2', 'PASS'],
  ]);
  assert.deepEqual(await (await harness.fetch(new Request('https://fixture.invalid/health'),
    { DB: db, TEST_CASE: 'core' })).json(), { status: 'PASS' });
  for (const name of ['small', 'representative', 'large']) {
    const env = { DB: db, TEST_CASE: name, TEST_WRITES_ENABLED: '1' };
    await harness.scheduled({}, env);
    assert.deepEqual(await (await harness.fetch(
      new Request('https://fixture.invalid/health'), env)).json(), { status: 'PASS' });
    const accounts = db.rows('cloud_accounts').length;
    const revisions = db.rows('cloud_plan_revisions').length;
    await harness.scheduled({}, env);
    assert.deepEqual([db.rows('cloud_accounts').length,
      db.rows('cloud_plan_revisions').length], [accounts, revisions]);
  }
  assert.deepEqual(db.sqlite.prepare(`SELECT run_key, status FROM cloud_test_runs
    WHERE run_key LIKE 'commands-v1:%' OR run_key = 'synthetic-v2'
    ORDER BY run_key`).all(), v1);
  assert.deepEqual(db.sqlite.prepare(`SELECT run_key, status FROM cloud_test_runs
    WHERE run_key LIKE 'commands-v2:%' ORDER BY run_key`).all().map(row =>
    [row.run_key, row.status]), [['commands-v2:large', 'PASS'],
    ['commands-v2:representative', 'PASS'], ['commands-v2:small', 'PASS']]);
  assert.equal(db.rows('cloud_accounts').length, 5);
  assert.equal(db.rows('cloud_thesis_revisions').length, 4);
  assert.equal(db.rows('cloud_transactions').length, 1);
});

test('v2 diagnostic exposes only a fixed synthetic stage and code', async () => {
  const db = new FakeD1();
  await db.prepare(`CREATE TABLE IF NOT EXISTS cloud_accounts
    (account_fingerprint TEXT PRIMARY KEY, broker TEXT NOT NULL,
    masked_label TEXT, base_currency TEXT NOT NULL)`).run();
  await db.prepare(`INSERT INTO cloud_accounts
    (account_fingerprint, broker, base_currency) VALUES (?, 'synthetic', 'KRW')`)
    .bind(`fixture_v2_small_${'d'.repeat(64)}`).run();
  const env = { DB: db, TEST_CASE: 'small', TEST_WRITES_ENABLED: '1' };
  await harness.scheduled({}, env);
  assert.deepEqual(await (await harness.fetch(
    new Request('https://fixture.invalid/health'), env)).json(),
  { status: 'FAIL', diagnostic: { stage: 'account_import', code: 'SQL_ERROR' } });
});

for (const [name, size] of [['small', 1], ['representative', 5], ['large', 40]]) {
  test(`fixed ${name} command scenario (${size} source positions/plans) is bounded and replay safe`,
    async () => {
      const db = new FakeD1();
      const env = { DB: db, TEST_CASE: name, TEST_WRITES_ENABLED: '1' };
      const health = async () => (await harness.fetch(
        new Request('https://fixture.invalid/health'), env)).json();
      assert.deepEqual(await health(), { status: 'NOT_RUN' });
      assert.equal((await harness.fetch(new Request('https://fixture.invalid/run', {
        method: 'POST', body: '{}',
      }), env)).status, 404);
      await harness.scheduled({}, { ...env, TEST_WRITES_ENABLED: '0' });
      assert.deepEqual(await health(), { status: 'NOT_RUN' });
      await harness.scheduled({}, env);
      assert.deepEqual(await health(), { status: 'PASS' });
      const counts = [db.rows('cloud_plan_revisions').length, db.rows('cloud_outbox').length];
      await harness.scheduled({}, env);
      assert.deepEqual(await health(), { status: 'PASS' });
      assert.deepEqual([db.rows('cloud_plan_revisions').length,
        db.rows('cloud_outbox').length], counts);
      assert.equal(db.rows('cloud_transactions').length, 0);
    });
}
