import assert from 'node:assert/strict';
import { test } from 'node:test';
import { beginSend, enqueueReport, enqueueUpdate, finishSend } from '../deploy/cloudflare-js/outbox.mjs';
import { FakeD1 } from './cloud_js_fake_d1.mjs';

test('update claim and outbox are atomic and duplicate safe', async () => {
  const db = new FakeD1();
  assert.equal(await enqueueUpdate(db, 73, 'fixture', '{"text":"hi"}', 't0'), true);
  assert.equal(await enqueueUpdate(db, 73, 'fixture', '{"text":"changed"}', 't1'), false);
  assert.equal(db.rows('cloud_outbox').length, 1);
  assert.equal(db.rows('cloud_outbox')[0].payload_json, '{"text":"hi"}');
  assert.equal(await beginSend(db, 'update:73:0'), true);
  assert.equal(await beginSend(db, 'update:73:0'), false);
  assert.equal(db.rows('cloud_outbox')[0].status, 'SENDING');
  assert.equal(await finishSend(db, 'update:73:0', false, 't2'), true);
  assert.equal(db.rows('cloud_outbox')[0].status, 'UNKNOWN');
  assert.equal(await beginSend(db, 'update:73:0'), false);
});

test('report batch collision rolls back claim and never replays UNKNOWN', async () => {
  const db = new FakeD1();
  assert.equal(await enqueueReport(db, 'weekly:fixture', [['a', 'one'], ['b', 'two']], 't0'), true);
  assert.equal(await enqueueReport(db, 'weekly:fixture', [['c', 'three']], 't1'), false);
  assert.equal(db.rows('cloud_outbox').length, 2);
  assert.equal(await beginSend(db, 'report:weekly:fixture:0'), true);
  assert.equal(await finishSend(db, 'report:weekly:fixture:0', false, 't2'), true);
  assert.equal(await beginSend(db, 'report:weekly:fixture:0'), false);

  db.sqlite.prepare(`INSERT INTO cloud_outbox
    (event_key, destination_key, payload_json, created_at) VALUES (?, ?, ?, ?)`)
    .run('report:collision:0', 'fixture', 'old', 't0');
  await assert.rejects(() => enqueueReport(db, 'collision', [['a', 'new']], 't1'));
  assert.equal(db.rows('cloud_report_claims').some(row => row.period_key === 'collision'), false);
  assert.equal(db.rows('cloud_outbox').find(row => row.event_key === 'report:collision:0').payload_json, 'old');
});
