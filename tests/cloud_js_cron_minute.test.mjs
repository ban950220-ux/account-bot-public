import assert from 'node:assert/strict';
import { test } from 'node:test';
import { FakeD1 } from './cloud_js_fake_d1.mjs';
import { dueJobs } from '../deploy/cloudflare-js/boundary.mjs';
import { createOperatingWorker } from '../deploy/cloudflare-js/operating-worker.mjs';
import { SyntheticCloudGateway } from '../deploy/cloudflare-js/cloud-gateway.mjs';
import { importAccount } from '../deploy/cloudflare-js/storage.mjs';

const account = `fixture_${'c'.repeat(64)}`;
const parts = ['00', '01', '49', '59'];
const milliseconds = ['000', '001', '999'];

test('daily, weekly and valuation slots accept seconds/ms within only their scheduled minute', () => {
  for (const [minute, expected] of [
    ['2026-11-01T23:00:', ['daily']],
    ['2026-11-07T00:00:', ['weekly']],
    ['2026-11-06T23:30:', ['weekly_valuation']],
  ]) for (const second of parts) for (const ms of milliseconds) {
    assert.deepEqual(dueJobs(`${minute}${second}.${ms}Z`, 'Asia/Seoul', '08:00:00', '09:00:00', 5), expected);
  }
  assert.deepEqual(dueJobs('2026-11-02T08:00:49.999+09:00', 'Asia/Seoul', '08:00', '09:00', 5), ['daily']);
});

test('rounding down never admits an adjacent minute, hour, wrong weekday or timezone', () => {
  for (const iso of ['2026-11-01T22:59:59.999Z', '2026-11-01T23:01:00Z',
    '2026-11-01T22:00:49Z', '2026-10-31T23:00:49Z', '2026-11-02T00:00:49Z']) {
    assert.deepEqual(dueJobs(iso, 'Asia/Seoul', '08:00', '09:00', 5), []);
  }
  assert.deepEqual(dueJobs('2026-11-06T00:00:49Z', 'Asia/Seoul', '08:00', '09:00', 5), []);
  assert.deepEqual(dueJobs('2026-11-01T23:00:49Z', 'UTC', '08:00', '09:00', 5), []);
  assert.throws(() => dueJobs('2026-11-01T23:00:49Z', 'invalid/zone', '08:00', '09:00', 5));
  // A configured nonzero second is never silently rounded to an earlier send.
  assert.deepEqual(dueJobs('2026-11-01T23:00:30Z', 'Asia/Seoul', '08:00:30', '09:00', 5), []);
});

async function fixture(t, { clock = '2026-11-01T23:00:59.999Z', daily = '08:00:00',
  legacyHold = false, state = 'ACTIVE', owner = '42', failSend = false } = {}) {
  const db = new FakeD1();
  t.after(() => db.sqlite.close());
  await importAccount(db, { fingerprint: account, broker: 'synthetic', baseCurrency: 'KRW' });
  if (legacyHold) db.sqlite.prepare('INSERT INTO cloud_legacy_report_holds VALUES (?,?,?,?)')
    .run('daily:20261102', 'daily', '2026-11-02', '["UNKNOWN"]');
  db.sqlite.exec("INSERT INTO cloud_core_import_runs VALUES ('fixture','{}','{}','VERIFIED')");
  db.sqlite.prepare(`INSERT INTO cloud_cutover (singleton,run_id,epoch,state,owner_user_id,
    target_id,scope_fingerprint,source_sha256,report_timezone,portfolio_id,reports_after,
    edits_enabled,sends_enabled) VALUES (1,'fixture',2,?,?,'fixture-worker',?,'fixture',
    'Asia/Seoul',1,'2026-10-01T00:00:00Z',1,1)`).run(state, owner, account);
  const env = { DB: db, TELEGRAM_BOT_TOKEN: `123456:${'a'.repeat(36)}`,
    TELEGRAM_WEBHOOK_SECRET: 'synthetic-secret', TELEGRAM_ALLOWED_USER_IDS: '42',
    TELEGRAM_CHAT_ID: '42', REPORT_TIMEZONE: 'Asia/Seoul', DAILY_REPORT_TIME: daily,
    WEEKLY_REPORT_TIME: '09:00:00', WEEKLY_REPORT_WEEKDAY: '5',
    CLOUD_TARGET_ID: 'fixture-worker', CLOUD_PORTFOLIO_ID: '1', KIWOOM_ACCUM_FINGERPRINT: account };
  const provider = { name: 'fixture',
    getPositions: async () => ({ broker: 'fixture', status: 'ok', items: [] }),
    getCash: async () => ({ broker: 'fixture', status: 'ok', items: [] }),
    getAccounts: async () => ({ broker: 'fixture', status: 'ok', items: [] }) };
  let gatewayCalls = 0;
  let sends = 0;
  const now = () => clock;
  const build = () => createOperatingWorker({ requireCutover: true, now,
    gatewayFactory: async input => {
      gatewayCalls++;
      return new SyntheticCloudGateway({ db: input.DB, providers: [provider],
        scopeAccountFingerprint: account, externalRecurringPlans: async () => [], now });
    },
    providerFetcher: async () => assert.fail('no real provider requests'),
    fetcher: async () => {
      sends++;
      if (failSend) throw new Error('synthetic timeout');
      return { ok: true, json: async () => ({ ok: true, result: { message_id: sends } }) };
    } });
  return { db, env, build, get sends() { return sends; }, get gatewayCalls() { return gatewayCalls; },
    setClock(value) { clock = value; },
    tick: iso => ({ cron: '0 23 * * SUN-THU', scheduledTime: Date.parse(iso) }) };
}

test('second/ms variants and concurrent repeats keep one period claim/outbox/send across restarts', async t => {
  const f = await fixture(t);
  await Promise.all(parts.map(second => f.build().scheduled(f.tick(`2026-11-01T23:00:${second}.999Z`), f.env)));
  await f.build().scheduled(f.tick('2026-11-01T23:00:01.001Z'), f.env);
  assert.equal(f.sends, 1);
  assert.deepEqual(f.db.rows('cloud_report_claims').map(r => r.period_key), ['daily:20261102']);
  assert.deepEqual(f.db.rows('cloud_outbox').map(r => [r.event_key, r.status]),
    [['report:daily:20261102:0', 'SENT']]);
});

test('a one-day-late controller event cannot backfill or claim the current day instead', async t => {
  const f = await fixture(t, { clock: '2026-11-02T23:00:49Z' });
  await f.build().scheduled(f.tick('2026-11-01T23:00:49Z'), f.env);
  assert.equal(f.gatewayCalls, 0);
  assert.equal(f.sends, 0);
  assert.equal(f.db.rows('cloud_report_claims').length, 0);
  assert.equal(f.db.rows('cloud_outbox').length, 0);
  // A genuinely current-day event remains independent of the stale copy.
  await f.build().scheduled(f.tick('2026-11-02T23:00:49Z'), f.env);
  assert.deepEqual(f.db.rows('cloud_report_claims').map(r => r.period_key), ['daily:20261103']);
  assert.equal(f.sends, 1);
});

test('controller time governs the job even when arrival is in a different minute', async t => {
  const f = await fixture(t, { clock: '2026-11-01T23:00:49Z' });
  await f.build().scheduled(f.tick('2026-11-01T22:59:59.999Z'), f.env);
  assert.equal(f.gatewayCalls, 0);
  f.setClock('2026-11-01T23:01:10Z');
  await f.build().scheduled(f.tick('2026-11-01T23:00:49.999Z'), f.env);
  assert.equal(f.sends, 1);
  assert.equal(f.db.rows('cloud_report_claims')[0].period_key, 'daily:20261102');
});

test('UTC midnight does not change the KST period, while a late KST-date crossing is refused', async t => {
  const f = await fixture(t, { clock: '2026-11-02T00:00:01Z', daily: '08:59:00' });
  await f.build().scheduled(f.tick('2026-11-01T23:59:59.999Z'), f.env);
  assert.equal(f.sends, 1);
  assert.equal(f.db.rows('cloud_report_claims')[0].period_key, 'daily:20261102');
  const late = await fixture(t, { clock: '2026-11-02T15:00:01Z', daily: '23:59:00' });
  await late.build().scheduled(late.tick('2026-11-02T14:59:59.999Z'), late.env);
  assert.equal(late.gatewayCalls, 0);
  assert.equal(late.sends, 0);
});

test('legacy hold and uncertain sends remain non-replayable throughout the minute', async t => {
  const held = await fixture(t, { legacyHold: true });
  for (const second of parts) await held.build().scheduled(held.tick(`2026-11-01T23:00:${second}Z`), held.env);
  assert.equal(held.sends, 0);
  assert.equal(held.db.rows('cloud_report_claims').length, 0);
  assert.equal(held.db.rows('cloud_outbox').length, 0);
  const uncertain = await fixture(t, { failSend: true });
  for (const second of parts) await uncertain.build().scheduled(uncertain.tick(`2026-11-01T23:00:${second}Z`), uncertain.env);
  assert.equal(uncertain.sends, 1);
  assert.equal(uncertain.db.rows('cloud_outbox')[0].status, 'UNKNOWN');
});

test('minute matching cannot bypass paused cutover or its owner fence', async t => {
  for (const options of [{ state: 'PAUSED' }, { owner: '43' }]) {
    const f = await fixture(t, options);
    await assert.rejects(() => f.build().scheduled(f.tick('2026-11-01T23:00:49Z'), f.env), /fence closed/);
    assert.equal(f.gatewayCalls, 0);
    assert.equal(f.sends, 0);
    assert.equal(f.db.rows('cloud_report_claims').length, 0);
  }
});

test('nonzero-second report settings are rejected before D1, providers or delivery', async t => {
  const f = await fixture(t);
  for (const [key, value] of [['DAILY_REPORT_TIME', '08:00:30'], ['WEEKLY_REPORT_TIME', '09:00:01']]) {
    const env = { ...f.env, [key]: value,
      DB: { prepare: () => assert.fail('invalid config must not query D1') } };
    await assert.rejects(() => f.build().scheduled(f.tick('2026-11-01T23:00:49Z'), env),
      /configuration unavailable/);
  }
  assert.equal(f.gatewayCalls, 0);
  assert.equal(f.sends, 0);
  assert.equal(f.db.rows('cloud_report_claims').length, 0);
  assert.equal(f.db.rows('cloud_outbox').length, 0);
});

test('weekly Cron repeats use a single local Saturday period', async t => {
  const f = await fixture(t, { clock: '2026-11-07T00:01:10Z' });
  for (const second of parts) await f.build().scheduled({ cron: '0 0 * * SAT',
    scheduledTime: Date.parse(`2026-11-07T00:00:${second}.999Z`) }, f.env);
  assert.equal(f.sends, 1);
  assert.deepEqual(f.db.rows('cloud_report_claims').map(r => r.period_key), ['weekly:20261107']);
  assert.ok(f.db.rows('cloud_outbox').every(r => r.status === 'SENT'));
});
