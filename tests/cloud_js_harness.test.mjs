import assert from 'node:assert/strict';
import { test } from 'node:test';
import harness from '../deploy/cloudflare-js/synthetic-harness.mjs';
import { FakeD1 } from './cloud_js_fake_d1.mjs';

test('scheduled-only synthetic harness is bounded, replay safe and has read-only HTTP', async () => {
  const db = new FakeD1();
  const health = () => harness.fetch(new Request('https://fixture.invalid/health'), { DB: db });
  const blocked = await harness.fetch(new Request('https://fixture.invalid/run', {
    method: 'POST', body: '{"sql":"DROP TABLE cloud_accounts"}',
  }), { DB: db, TEST_WRITES_ENABLED: '1' });
  assert.equal(blocked.status, 404);
  assert.equal(db.rows('cloud_accounts').length, 0);
  await harness.scheduled({}, { DB: db, TEST_WRITES_ENABLED: '0' });
  assert.equal(db.rows('cloud_accounts').length, 0);
  await Promise.all([
    harness.scheduled({}, { DB: db, TEST_WRITES_ENABLED: '1' }),
    harness.scheduled({}, { DB: db, TEST_WRITES_ENABLED: '1' }),
  ]);
  assert.deepEqual(await (await health()).json(), { status: 'PASS' });
  assert.equal(db.rows('cloud_accounts').length, 1);
  assert.equal(db.rows('cloud_transactions').length, 1);
  assert.equal(db.rows('cloud_outbox').length, 1);
  assert.equal(db.rows('cloud_outbox')[0].status, 'UNKNOWN');
  await harness.scheduled({}, { DB: db, TEST_WRITES_ENABLED: '1' });
  await harness.scheduled({}, { DB: db, TEST_WRITES_ENABLED: '0' });
  assert.equal(db.rows('cloud_transactions').length, 1);
  assert.equal(db.rows('cloud_outbox').length, 1);
});
