import assert from 'node:assert/strict';
import { test } from 'node:test';
import { FakeD1 } from './cloud_js_fake_d1.mjs';
import { importAccount, importPlanHistory } from '../deploy/cloudflare-js/storage.mjs';
import { appendThesis, applyGroupChanges, applyPlanChange,
  createPlanOnce, listCurrentPlans } from
  '../deploy/cloudflare-js/cloud-write.mjs';

const account = `fixture_${'d'.repeat(64)}`;
const instrument = { market: 'NASDAQ', ticker: 'TEST', assetType: 'ETF', currency: 'USD' };
const planKey = 'manual:NASDAQ:TEST';
const initial = { instrument, accountFingerprint: account, source: 'TELEGRAM',
  amountCurrency: 'USD', dailyAmount: '10', targetWeight: '0.1', enabled: true,
  timezoneName: 'Asia/Seoul', startedAt: '2026-01-01T00:00:00Z', memo: null };

async function seeded() {
  const db = new FakeD1();
  await importAccount(db, { fingerprint: account, broker: 'synthetic', baseCurrency: 'KRW' });
  await importPlanHistory(db, { ...initial, planKey, version: 1 }, [{ ...initial,
    revisionNo: 1, changeHash: 'a'.repeat(64), effectiveFrom: '2026-01-01T00:00:00Z' }]);
  return db;
}

test('plan update uses version CAS and preserves exactly one append-only revision', async () => {
  const db = await seeded();
  const first = applyPlanChange(db, planKey, 1, 'fixture-change-a',
    { ...initial, dailyAmount: '12' }, '2026-02-01T00:00:00Z');
  const second = applyPlanChange(db, planKey, 1, 'fixture-change-b',
    { ...initial, dailyAmount: '15' }, '2026-02-01T00:00:00Z');
  assert.deepEqual((await Promise.all([first, second])).sort(), ['CHANGED', 'CONFLICT']);
  const plans = await listCurrentPlans(db);
  assert.equal(plans.length, 1);
  assert.equal(plans[0].version, 2);
  assert.equal(db.rows('cloud_plan_revisions').length, 2);
  assert.equal(db.rows('cloud_plan_revisions')[1].daily_amount, plans[0].dailyAmount);
  assert.equal(await applyPlanChange(db, planKey, 1, 'fixture-change-a',
    { ...initial, dailyAmount: '12' }, '2026-02-01T00:00:00Z'), 'CONFLICT');
  assert.equal(db.rows('cloud_plan_revisions').length, 2);
});

test('thesis revisions deduplicate and reject stale concurrent writes', async () => {
  const db = new FakeD1();
  const payload = { market: 'NASDAQ', ticker: 'TEST', currency: 'USD',
    text: 'synthetic thesis', revisionKey: 'telegram:TEST:a',
    mutationKey: 'fixture-thesis-a', expectedRevision: 0,
    effectiveAt: '2026-02-01T00:00:00Z' };
  const statuses = await Promise.all([
    appendThesis(db, payload),
    appendThesis(db, { ...payload, text: 'other synthetic thesis',
      revisionKey: 'telegram:TEST:b', mutationKey: 'fixture-thesis-b' }),
  ]);
  assert.deepEqual(statuses.sort(), ['CHANGED', 'CONFLICT']);
  assert.equal(db.rows('cloud_theses')[0].current_revision_no, 1);
  assert.equal(db.rows('cloud_thesis_revisions').length, 1);
  assert.equal(await appendThesis(db, payload), 'CONFLICT');
  assert.equal(db.rows('cloud_thesis_revisions').length, 1);
});

test('D1 failure does not claim a successful plan change', async () => {
  const db = await seeded();
  const original = db.batch.bind(db);
  db.batch = async () => { throw new Error('synthetic D1 unavailable'); };
  await assert.rejects(() => applyPlanChange(db, planKey, 1, 'fixture-failure',
    { ...initial, dailyAmount: '12' }, '2026-02-01T00:00:00Z'));
  db.batch = original;
  assert.equal(db.rows('cloud_plans')[0].version, 1);
  assert.equal(db.rows('cloud_plan_revisions').length, 1);
});

test('plan edits stay in one account and reject malformed amounts', async () => {
  const db = await seeded();
  const otherAccount = `fixture_${'e'.repeat(64)}`;
  await importAccount(db, { fingerprint: otherAccount, broker: 'synthetic',
    baseCurrency: 'KRW' });
  const otherKey = 'manual:NASDAQ:TEST:other';
  const other = { ...initial, accountFingerprint: otherAccount };
  await importPlanHistory(db, { ...other, planKey: otherKey, version: 1 }, [{
    ...other, revisionNo: 1, changeHash: 'b'.repeat(64),
    effectiveFrom: '2026-01-01T00:00:00Z',
  }]);
  await assert.rejects(() => applyPlanChange(db, planKey, 1, 'fixture-transfer',
    { ...initial, accountFingerprint: otherAccount }, '2026-02-01T00:00:00Z'));
  await assert.rejects(() => applyPlanChange(db, planKey, 1, 'fixture-bad-money',
    { ...initial, dailyAmount: 'NaN' }, '2026-02-01T00:00:00Z'));
  assert.equal(await applyPlanChange(db, planKey, 1, 'fixture-only-main',
    { ...initial, enabled: false }, '2026-02-01T00:00:00Z'), 'CHANGED');
  assert.equal(db.rows('cloud_plans').find(row => row.plan_key === otherKey).enabled, 1);
  assert.equal(db.rows('cloud_plan_revisions').filter(row => row.plan_key === otherKey).length, 1);
});

test('create plan replay is idempotent and a changed payload conflicts', async () => {
  const db = new FakeD1();
  await importAccount(db, { fingerprint: account, broker: 'synthetic', baseCurrency: 'KRW' });
  assert.equal(await createPlanOnce(db, planKey, 'update:731', initial,
    '2026-02-01T00:00:00Z'), 'CREATED');
  assert.equal(await createPlanOnce(db, planKey, 'update:731', initial,
    '2026-02-01T00:00:00Z'), 'REPLAY');
  assert.equal(await createPlanOnce(db, planKey, 'update:731',
    { ...initial, dailyAmount: '99' }, '2026-02-01T00:00:00Z'), 'CONFLICT');
  assert.equal(db.rows('cloud_plans').length, 1);
  assert.equal(db.rows('cloud_plan_revisions').length, 1);
});

test('group edit commits every selected plan or rolls back on a late version change', async () => {
  const db = await seeded();
  const secondKey = 'manual:NASDAQ:OTHER';
  const second = { ...initial, instrument: { ...instrument, ticker: 'OTHER' } };
  await importPlanHistory(db, { ...second, planKey: secondKey, version: 1 }, [{
    ...second, revisionNo: 1, changeHash: 'f'.repeat(64),
    effectiveFrom: '2026-01-01T00:00:00Z',
  }]);
  const changes = [
    { planKey, expectedVersion: 1, nextState: { ...initial, enabled: false } },
    { planKey: secondKey, expectedVersion: 1,
      nextState: { ...second, enabled: false } },
  ];
  const original = db.batch.bind(db);
  db.batch = async statements => {
    db.sqlite.prepare('UPDATE cloud_plans SET version = 2 WHERE plan_key = ?').run(secondKey);
    return original(statements);
  };
  await assert.rejects(() => applyGroupChanges(db, changes, 'group-fixture-1',
    '2026-02-01T00:00:00Z'));
  db.batch = original;
  assert.equal(db.rows('cloud_plans').find(row => row.plan_key === planKey).version, 1);
  assert.equal(db.rows('cloud_plan_revisions').length, 2);
  assert.equal(db.rows('cloud_mutation_checks').length, 0);
  db.sqlite.prepare('UPDATE cloud_plans SET version = 1 WHERE plan_key = ?').run(secondKey);
  assert.equal(await applyGroupChanges(db, changes, 'group-fixture-2',
    '2026-02-01T00:00:00Z'), 'CHANGED');
  assert.equal(db.rows('cloud_plan_revisions').length, 4);
  assert.deepEqual(db.rows('cloud_plans').map(row => row.enabled), [0, 0]);
});
