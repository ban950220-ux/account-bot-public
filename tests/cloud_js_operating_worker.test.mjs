import assert from 'node:assert/strict';
import { test } from 'node:test';
import { FakeD1 } from './cloud_js_fake_d1.mjs';
import { importAccount } from '../deploy/cloudflare-js/storage.mjs';
import { SyntheticCloudGateway } from '../deploy/cloudflare-js/cloud-gateway.mjs';
import { createOperatingWorker } from '../deploy/cloudflare-js/operating-worker.mjs';
import { splitTelegramMessage } from '../deploy/cloudflare-js/telegram-transport.mjs';

const account = `fixture_${'e'.repeat(64)}`;
const base = db => ({ DB: db, TELEGRAM_BOT_TOKEN: `123456:${'a'.repeat(36)}`,
  TELEGRAM_WEBHOOK_SECRET: 'synthetic-secret', TELEGRAM_ALLOWED_USER_IDS: '42',
  TELEGRAM_CHAT_ID: '42', REPORT_TIMEZONE: 'Asia/Seoul',
  DAILY_REPORT_TIME: '10:00', WEEKLY_REPORT_TIME: '18:00',
  WEEKLY_REPORT_WEEKDAY: '5' });
const provider = { name: 'kiwoom_main',
  getPositions: async () => ({ broker: 'kiwoom_main', status: 'ok', items: [] }),
  getCash: async () => ({ broker: 'kiwoom_main', status: 'ok', items: [] }),
  getAccounts: async () => ({ broker: 'kiwoom_main', status: 'ok', items: [] }) };
const gatewayFactory = async env => new SyntheticCloudGateway({ db: env.DB,
  providers: [provider], scopeAccountFingerprint: account,
  externalRecurringPlans: async () => [], now: () => '2026-02-02T01:00:00Z' });
const now = () => '2026-02-02T01:00:00Z';
function request(id, command, owner = 42, secret = 'synthetic-secret') {
  return new Request('https://fixture.invalid/telegram/webhook', { method: 'POST',
    headers: { 'X-Telegram-Bot-Api-Secret-Token': secret },
    body: JSON.stringify({ update_id: id, message: { from: { id: owner },
      chat: { id: owner, type: 'private' }, text: command } }) });
}
async function database() {
  const db = new FakeD1();
  await importAccount(db, { fingerprint: account, broker: 'synthetic', baseCurrency: 'KRW' });
  return db;
}

test('owner command mutation, outbox and send are replay safe across Worker restarts', async () => {
  const db = await database();
  let sends = 0;
  const fetcher = async () => { sends += 1; return { ok: true, json: async () =>
    ({ ok: true, result: { message_id: sends } }) }; };
  const build = () => createOperatingWorker({ gatewayFactory, fetcher, now });
  const env = base(db);
  assert.equal((await build().fetch(request(900, '/recurring_add TEST 20USD 8%'), env)).status, 200);
  assert.equal(db.rows('cloud_plan_revisions').length, 1);
  assert.equal(db.rows('cloud_outbox')[0].status, 'SENT');
  assert.equal(db.rows('cloud_update_claims').length, 1);
  assert.equal(sends, 1);
  assert.equal((await build().fetch(request(900, '/recurring_add TEST 20USD 8%'), env)).status, 200);
  assert.equal(sends, 1);
  assert.equal(db.rows('cloud_plan_revisions').length, 1);
});

test('missing secret and wrong owner or chat fail before D1 and transport', async () => {
  const db = await database();
  let gatewayCalls = 0;
  const worker = createOperatingWorker({ gatewayFactory: async () => {
    gatewayCalls += 1; throw new Error('must not run');
  }, fetcher: async () => { throw new Error('must not send'); }, now });
  const env = base(db);
  assert.equal((await worker.fetch(request(901, '/start', 42, 'bad-secret'), env)).status, 403);
  assert.equal((await worker.fetch(request(902, '/start', 43), env)).status, 403);
  assert.equal((await worker.fetch(request(903, '/start'), { ...env,
    TELEGRAM_BOT_TOKEN: '' })).status, 503);
  assert.equal(gatewayCalls, 0);
  assert.equal(db.rows('cloud_update_claims').length, 0);
});

test('every allowlisted private owner receives their own command reply', async () => {
  const db = await database();
  const recipients = [];
  const worker = createOperatingWorker({ gatewayFactory,
    fetcher: async (_url, options) => {
      recipients.push(JSON.parse(options.body).chat_id);
      return { ok: true, json: async () => ({ ok: true, result: { message_id: 1 } }) };
    }, now });
  const env = { ...base(db), TELEGRAM_ALLOWED_USER_IDS: '42,43' };
  assert.equal((await worker.fetch(request(908, '/start', 43), env)).status, 200);
  assert.deepEqual(recipients, ['43']);
  const spoof = new Request('https://fixture.invalid/telegram/webhook', { method: 'POST',
    headers: { 'X-Telegram-Bot-Api-Secret-Token': 'synthetic-secret' },
    body: JSON.stringify({ update_id: 909, message: { from: { id: 43 },
      chat: { id: 42, type: 'private' }, text: '/start' } }) });
  assert.equal((await worker.fetch(spoof, env)).status, 403);
  assert.deepEqual(recipients, ['43']);
});

test('outbox insert failure rolls plan change back; next attempt may safely succeed', async () => {
  const db = await database();
  db.sqlite.exec(`CREATE TRIGGER fail_outbox BEFORE INSERT ON cloud_outbox
    BEGIN SELECT RAISE(ABORT, 'synthetic failed outbox'); END`);
  let sends = 0;
  const worker = createOperatingWorker({ gatewayFactory, fetcher: async () => {
    sends += 1; return { ok: true, json: async () => ({ ok: true, result: { message_id: 1 } }) };
  }, now });
  const env = base(db);
  assert.equal((await worker.fetch(request(904, '/recurring_add TEST 20USD 8%'), env)).status, 503);
  assert.equal(db.rows('cloud_plans').length, 0);
  assert.equal(db.rows('cloud_outbox').length, 0);
  db.sqlite.exec('DROP TRIGGER fail_outbox');
  assert.equal((await worker.fetch(request(904, '/recurring_add TEST 20USD 8%'), env)).status, 200);
  assert.equal(db.rows('cloud_plan_revisions').length, 1);
  assert.equal(sends, 1);
});

test('accepted send with failed status write remains SENDING and never resends', async () => {
  const db = await database();
  const prepare = db.prepare.bind(db);
  let failFinish = true;
  db.prepare = sql => {
    if (failFinish && sql.includes("UPDATE cloud_outbox SET status = ?, finished_at = ?")) {
      return { bind: () => ({ run: async () => { failFinish = false;
        throw new Error('synthetic status write failure'); } }) };
    }
    return prepare(sql);
  };
  let sends = 0;
  const build = () => createOperatingWorker({ gatewayFactory, fetcher: async () => {
    sends += 1; return { ok: true, json: async () => ({ ok: true, result: { message_id: 1 } }) };
  }, now });
  const env = base(db);
  assert.equal((await build().fetch(request(905, '/start'), env)).status, 200);
  assert.equal(db.rows('cloud_outbox')[0].status, 'SENDING');
  assert.equal((await build().fetch(request(905, '/start'), env)).status, 200);
  assert.equal(sends, 1);
});

test('ambiguous transport result stays UNKNOWN and duplicate or concurrent requests do not resend',
  async () => {
    const db = await database();
    let sends = 0;
    const worker = createOperatingWorker({ gatewayFactory, fetcher: async () => {
      sends += 1; throw new Error('synthetic timeout');
    }, now });
    const env = base(db);
    await Promise.all([worker.fetch(request(906, '/start'), env),
      worker.fetch(request(906, '/start'), env)]);
    assert.equal(db.rows('cloud_outbox')[0].status, 'UNKNOWN');
    await worker.fetch(request(906, '/start'), env);
    assert.equal(sends, 1);
  });

test('scheduled report is claimed once and delivered once across duplicate ticks', async () => {
  const db = await database();
  let sends = 0;
  const worker = createOperatingWorker({ gatewayFactory, fetcher: async () => {
    sends += 1; return { ok: true, json: async () => ({ ok: true, result: { message_id: sends } }) };
  }, now });
  const env = base(db);
  await worker.scheduled({ scheduledTime: Date.parse('2026-02-02T01:00:00Z') }, env);
  await worker.scheduled({ scheduledTime: Date.parse('2026-02-02T01:00:00Z') }, env);
  assert.equal(db.rows('cloud_report_claims').length, 1);
  assert.equal(db.rows('cloud_outbox').length, 1);
  assert.equal(sends, 1);
});

test('concurrent copies of the same changing update never double-edit or double-send',
  async () => {
    const db = await database();
    let sends = 0;
    const worker = createOperatingWorker({ gatewayFactory, fetcher: async () => {
      sends += 1; return { ok: true, json: async () =>
        ({ ok: true, result: { message_id: 1 } }) };
    }, now });
    const env = base(db);
    await Promise.all([worker.fetch(request(907, '/recurring_add TEST 20USD 8%'), env),
      worker.fetch(request(907, '/recurring_add TEST 20USD 8%'), env)]);
    assert.equal(db.rows('cloud_plans').length, 1);
    assert.equal(db.rows('cloud_plan_revisions').length, 1);
    assert.equal(db.rows('cloud_outbox').length, 1);
    assert.equal(sends, 1);
  });

test('uncertain scheduled send is not repeated by a later tick', async () => {
  const db = await database();
  let sends = 0;
  const worker = createOperatingWorker({ gatewayFactory, fetcher: async () => {
    sends += 1; throw new Error('synthetic timeout');
  }, now });
  const env = base(db);
  const tick = { scheduledTime: Date.parse('2026-02-02T01:00:00Z') };
  await worker.scheduled(tick, env);
  await worker.scheduled(tick, env);
  assert.equal(db.rows('cloud_outbox')[0].status, 'UNKNOWN');
  assert.equal(sends, 1);
});

test('long Unicode report chunks stay bounded and keep complete code points', () => {
  const text = `Header\n${'😀'.repeat(5000)}\nTail`;
  const chunks = splitTelegramMessage(text);
  assert.ok(chunks.length > 1);
  assert.ok(chunks.every(chunk => chunk.length <= 3900));
  assert.equal(chunks.join('').replaceAll('\n', ''), text.replaceAll('\n', ''));
  assert.ok(chunks.every(chunk => !/[\uD800-\uDBFF]$|^[\uDC00-\uDFFF]/u.test(chunk)));
});

test('long weekly report stages and sends ordered chunks once', async () => {
  const db = await database();
  const plans = Array.from({ length: 200 }, (_, index) => ({
    instrument: { market: 'KOSPI', ticker: String(900000 + index),
      assetType: 'EQUITY', currency: 'KRW' },
    broker: 'meritz', currency: 'KRW', dailyAmount: '1000', enabled: true,
    targetWeightPct: null, scheduledWeekdays: [0, 1, 2, 3, 4],
    investmentThesis: '',
  }));
  const factory = async env => new SyntheticCloudGateway({ db: env.DB,
    providers: [provider], scopeAccountFingerprint: account,
    externalRecurringPlans: async () => plans });
  const sent = [];
  const worker = createOperatingWorker({ gatewayFactory: factory,
    fetcher: async (_url, options) => {
      sent.push(JSON.parse(options.body).text);
      return { ok: true, json: async () => ({ ok: true,
        result: { message_id: sent.length } }) };
    }, now: () => '2026-02-07T01:00:00Z' });
  const env = { ...base(db), WEEKLY_REPORT_TIME: '10:00' };
  const tick = { scheduledTime: Date.parse('2026-02-07T01:00:00Z') };
  await worker.scheduled(tick, env);
  assert.ok(sent.length > 1);
  assert.equal(db.rows('cloud_outbox').length, sent.length);
  assert.ok(db.rows('cloud_outbox').every(row => row.status === 'SENT'));
  await worker.scheduled(tick, env);
  assert.equal(db.rows('cloud_outbox').length, sent.length);
});

test('an uncertain first report chunk blocks later chunks and replay', async () => {
  const db = await database();
  const plans = Array.from({ length: 200 }, (_, index) => ({
    instrument: { market: 'KOSPI', ticker: String(900000 + index),
      assetType: 'EQUITY', currency: 'KRW' },
    broker: 'meritz', currency: 'KRW', dailyAmount: '1000', enabled: true,
    targetWeightPct: null, scheduledWeekdays: [0, 1, 2, 3, 4], investmentThesis: '',
  }));
  const factory = async env => new SyntheticCloudGateway({ db: env.DB,
    providers: [provider], scopeAccountFingerprint: account,
    externalRecurringPlans: async () => plans });
  let calls = 0;
  const worker = createOperatingWorker({ gatewayFactory: factory,
    fetcher: async () => { calls += 1; throw new Error('synthetic timeout'); },
    now: () => '2026-02-07T01:00:00Z' });
  const env = { ...base(db), WEEKLY_REPORT_TIME: '10:00' };
  const tick = { scheduledTime: Date.parse('2026-02-07T01:00:00Z') };
  await worker.scheduled(tick, env);
  assert.ok(db.rows('cloud_outbox').length > 1);
  assert.equal(db.rows('cloud_outbox')[0].status, 'UNKNOWN');
  assert.ok(db.rows('cloud_outbox').slice(1).every(row => row.status === 'PENDING'));
  await worker.scheduled(tick, env);
  assert.equal(calls, 1);
});
