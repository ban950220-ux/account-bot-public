import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createKiwoomReadOnlyProvider } from '../deploy/cloudflare-js/kiwoom-readonly.mjs';
import { createOperatingWorker } from '../deploy/cloudflare-js/operating-worker.mjs';
import { FakeD1 } from './cloud_js_fake_d1.mjs';
import { createOperatingGateway } from '../deploy/cloudflare-js/operating-gateway.mjs';
import { SyntheticCloudGateway } from '../deploy/cloudflare-js/cloud-gateway.mjs';
import { importAccount } from '../deploy/cloudflare-js/storage.mjs';

const accountId = 'local-main';
const make = fetcher => createKiwoomReadOnlyProvider({ name: 'kiwoom_main', accountId,
  appKey: 'synthetic-key', appSecret: 'synthetic-secret', fetcher });
const response = (payload, headers = {}, status = 200) => ({ ok: status === 200,
  status, headers: new Headers(headers), json: async () => payload });

test('read-only Kiwoom calls use the fixed origin, paths and TRs', async () => {
  const calls = [];
  const broker = make(async (url, options) => {
    calls.push({ url, method: options.method, headers: options.headers });
    if (url.endsWith('/oauth2/token')) return response({ token: 'synthetic-token' });
    if (options.headers['api-id'] === 'kt00018') return response({
      acnt_evlt_remn_indv_tot: [{ stk_cd: 'A005930', rmnd_qty: '1', pur_pric: '2',
        cur_prc: '3', pur_amt: '2', evlt_amt: '3', evltv_prft: '1', prft_rt: '50' }] });
    if (options.headers['api-id'] === 'ust21070') return response({ result_list: [] });
    if (options.headers['api-id'] === 'kt00001') return response({ entr: '100' });
    return response({ result_list: [] });
  });
  const holdings = await broker.getPositions();
  const cash = await broker.getCash();
  assert.equal(holdings.status, 'ok');
  assert.equal(holdings.items[0].instrument.ticker, '005930');
  assert.equal(cash.items[0].amount, '100');
  assert.equal(calls.filter(x => x.url.endsWith('/oauth2/token')).length, 1);
  assert.ok(calls.every(x => x.method === 'POST' &&
    x.url.startsWith('https://api.kiwoom.com/') && !x.url.includes('ordr')));
  assert.deepEqual(new Set(calls.slice(1).map(x => x.headers['api-id'])),
    new Set(['kt00018', 'ust21070', 'kt00001', 'ust21110']));
});

test('failed domestic read does not become zero and surviving US read stays partial', async () => {
  const broker = make(async (url, options) => {
    if (url.endsWith('/oauth2/token')) return response({ token: 'synthetic-token' });
    if (options.headers['api-id'] === 'kt00001') return response({}, {}, 429);
    return response({ result_list: [{ crnc_code: 'USD', fc_entra: '7.25' }] });
  });
  const cash = await broker.getCash();
  assert.equal(cash.status, 'partial');
  assert.equal(cash.items.length, 1);
  assert.equal(cash.items[0].amount, '7.25');
  assert.deepEqual(cash.missingAccounts, [accountId]);
});

test('malformed provider data and unsupported origins fail closed', async () => {
  assert.throws(() => createKiwoomReadOnlyProvider({ name: 'kiwoom_main', accountId,
    appKey: 'a', appSecret: 'b', origin: 'https://untrusted.invalid' }));
  const broker = make(async url => url.endsWith('/oauth2/token') ?
    response({ token: 'synthetic-token' }) : response({}));
  const result = await broker.getPositions();
  assert.equal(result.status, 'unavailable');
  assert.deepEqual(result.items, []);
});

test('same-day executions are read only, stable across retries and preserve missing fees',
  async () => {
    const calls = [];
    const broker = createKiwoomReadOnlyProvider({ name: 'kiwoom_main', accountId,
      appKey: 'fixture-key', appSecret: 'fixture-secret',
      now: () => '2026-02-02T00:30:00Z', fetcher: async (url, options) => {
        calls.push({ url, options });
        if (url.endsWith('/oauth2/token')) return response({ token: 'fixture-token' });
        if (options.headers['api-id'] === 'ka10076') return response({ cntr: [{
          stk_cd: 'A005930', cntr_qty: '0.125', cntr_pric: '80.10',
          io_tp_nm: '매수', ord_tm: '093000', ord_no: 'fixture-order',
        }] });
        return response({ result_list: [] });
      } });
    const range = { startDate: '2026-02-02', endDate: '2026-02-02' };
    const first = await broker.getTransactions(range);
    const replay = await broker.getTransactions(range);
    assert.equal(first.status, 'ok');
    assert.equal(first.historyScope, 'SAME_DAY_ONLY');
    assert.equal(first.items.length, 1);
    assert.equal(first.items[0].quantity, '0.125');
    assert.equal(first.items[0].grossAmount, '10.01250');
    assert.equal(first.items[0].fee, null);
    assert.equal(first.items[0].tradedAt, '2026-02-02T00:30:00.000Z');
    assert.equal(first.items[0].sourceKey, replay.items[0].sourceKey);
    assert.ok(calls.every(call => call.options.method === 'POST' &&
      call.options.redirect === 'manual' && !call.url.includes('ordr')));
    const before = calls.length;
    assert.deepEqual((await broker.getTransactions({ startDate: '2026-02-01',
      endDate: '2026-02-01' })).items, []);
    assert.equal(calls.length, before);
  });

test('partial execution response cannot become complete history or duplicate fills', async () => {
  const broker = createKiwoomReadOnlyProvider({ name: 'kiwoom_main', accountId,
    appKey: 'fixture-key', appSecret: 'fixture-secret',
    now: () => '2026-02-02T00:30:00Z', fetcher: async (url, options) => {
      if (url.endsWith('/oauth2/token')) return response({ token: 'fixture-token' });
      if (options.headers['api-id'] === 'ka10076') return response({ cntr: [{
        stk_cd: 'A005930', cntr_qty: '1', cntr_pric: '100', io_tp_nm: '매수',
        ord_tm: '093000', ord_no: 'fixture-order',
      }] });
      return response({}, {}, 429);
    } });
  const result = await broker.getTransactions({ startDate: '2026-02-02',
    endDate: '2026-02-02' });
  assert.equal(result.status, 'partial');
  assert.equal(result.items.length, 1);
  assert.deepEqual(result.missingAccounts, [accountId]);
});

test('gateway stores same-day fills idempotently in the exact account scope', async () => {
  const db = new FakeD1();
  const fingerprint = `fixture_${'d'.repeat(64)}`;
  await importAccount(db, { fingerprint, broker: 'kiwoom_main', baseCurrency: 'KRW' });
  const provider = { name: 'kiwoom_main', accountId,
    getTransactions: async () => ({ broker: 'kiwoom_main', status: 'partial',
      historyScope: 'SAME_DAY_ONLY', items: [{ broker: 'kiwoom_main', accountId,
        sourceKey: 'fixture-fill', source: 'kiwoom_main',
        instrument: { market: 'KRX', ticker: '005930', assetType: 'EQUITY',
          currency: 'KRW' }, currency: 'KRW', transactionType: 'BUY', status: 'FILLED',
        tradedAt: '2026-02-02T00:30:00Z', settledAt: null, quantity: '0.125',
        unitPrice: '80.10', grossAmount: '10.01250', fee: null, tax: null,
        netAmount: null }] }) };
  const gateway = new SyntheticCloudGateway({ db, providers: [provider],
    scopeAccountFingerprint: fingerprint, mainAccountFingerprint: fingerprint,
    externalRecurringPlans: async () => [] });
  const range = { startDate: '2026-02-02', endDate: '2026-02-02' };
  assert.deepEqual(await gateway.refreshTransactions(range), { status: 'partial',
    imported: 1, observed: 1, historyScope: 'SAME_DAY_ONLY' });
  assert.equal((await gateway.refreshTransactions(range)).imported, 0);
  assert.equal(db.rows('cloud_transactions').length, 1);
  assert.equal(db.rows('cloud_transactions')[0].fee, null);
  assert.equal(db.rows('cloud_transactions')[0].account_fingerprint, fingerprint);
  provider.getTransactions = async () => ({ broker: 'kiwoom_main', status: 'ok',
    historyScope: 'SAME_DAY_ONLY', items: [{ ...db.rows('cloud_transactions').map(row => ({
      broker: 'kiwoom_main', accountId, sourceKey: row.source_key, source: row.source,
      instrument: { market: row.market, ticker: row.ticker, assetType: row.asset_type,
        currency: row.currency }, currency: row.currency,
      transactionType: row.transaction_type, status: row.status,
      tradedAt: row.traded_at, settledAt: row.settled_at, quantity: row.quantity,
      unitPrice: row.unit_price, grossAmount: '999', fee: row.fee,
      tax: row.tax, netAmount: row.net_amount }))[0] }] });
  await assert.rejects(() => gateway.refreshTransactions(range), /source key conflict/);
});

test('manual edit route is absent by default and owner admission precedes D1 writes', async () => {
  const db = new FakeD1();
  const gatewayFactory = async () => { throw new Error('not needed'); };
  const request = () => new Request('https://fixture.invalid/owner/manual-input', {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: '{bad-json' });
  const absent = createOperatingWorker({ gatewayFactory, fetcher: async () => {},
    now: () => '2026-02-02T00:00:00Z' });
  assert.equal((await absent.fetch(request(), { DB: db })).status, 404);
  const guarded = createOperatingWorker({ gatewayFactory, fetcher: async () => {},
    now: () => '2026-02-02T00:00:00Z', authorizeManualOwner: async () => false });
  assert.equal((await guarded.fetch(request(), { DB: db })).status, 403);
  assert.equal(db.rows('cloud_manual_input_revisions').length, 0);
});

test('owner manual revision and read-only command share D1 service, with stale edit rejected',
  async () => {
    const db = new FakeD1();
    const fingerprint = `fixture_${'f'.repeat(64)}`;
    await importAccount(db, { fingerprint, broker: 'synthetic', baseCurrency: 'KRW' });
    const env = { DB: db, KIWOOM_MAIN_APP_KEY: 'fixture-key',
      KIWOOM_MAIN_APP_SECRET: 'fixture-secret', KIWOOM_MAIN_ACCOUNT_ALIAS: 'main-alias',
      KIWOOM_ACCUM_FINGERPRINT: fingerprint,
      TELEGRAM_BOT_TOKEN: `123456:${'a'.repeat(36)}`,
      TELEGRAM_WEBHOOK_SECRET: 'synthetic-secret', TELEGRAM_ALLOWED_USER_IDS: '42',
      TELEGRAM_CHAT_ID: '42', REPORT_TIMEZONE: 'Asia/Seoul', DAILY_REPORT_TIME: '10:00',
      WEEKLY_REPORT_TIME: '18:00', WEEKLY_REPORT_WEEKDAY: '5' };
    const brokerCalls = [];
    const brokerFetch = async (url, options) => {
      brokerCalls.push(url);
      if (url.endsWith('/oauth2/token')) return response({ token: 'synthetic-token' });
      if (options.headers['api-id'] === 'kt00018') return response({ acnt_evlt_remn_indv_tot: [] });
      if (options.headers['api-id'] === 'kt00001') return response({ entr: '100.25' });
      return response({ result_list: [] });
    };
    const messages = [];
    const worker = createOperatingWorker({ gatewayFactory: input =>
      createOperatingGateway(input, { fetcher: brokerFetch,
        now: () => '2026-02-02T01:00:00Z' }),
    fetcher: async (_url, options) => { messages.push(JSON.parse(options.body).text);
      return response({ ok: true, result: { message_id: 1 } }); },
    authorizeManualOwner: async request => request.headers.get('x-fixture-owner') === 'yes',
    now: () => '2026-02-02T01:00:00Z' });
    const document = { as_of: '2026-02-02T00:00:00Z',
      accounts: [{ account_id: 'meritz-alias', base_currency: 'KRW' }],
      positions: [], cash: [{ account_id: 'meritz-alias', currency: 'KRW', amount: '12.5' }],
      transactions: [] };
    const upload = (revision, owner = 'yes', mutationKey = 'edit-1') => new Request(
      'https://fixture.invalid/owner/manual-input', { method: 'POST',
        headers: { 'content-type': 'application/json', 'x-fixture-owner': owner },
        body: JSON.stringify({ kind: 'ACCOUNT_SNAPSHOT', document,
          expectedRevision: revision, mutationKey }) });
    assert.equal((await worker.fetch(upload(0, 'no'), env)).status, 403);
    assert.equal(db.rows('cloud_manual_input_revisions').length, 0);
    assert.equal((await worker.fetch(upload(0), env)).status, 200);
    assert.equal((await worker.fetch(upload(0), env)).status, 200);
    assert.equal((await worker.fetch(upload(0, 'yes', 'stale-edit'), env)).status, 409);
    assert.equal(db.rows('cloud_manual_input_revisions').length, 1);
    const update = new Request('https://fixture.invalid/telegram/webhook', { method: 'POST',
      headers: { 'X-Telegram-Bot-Api-Secret-Token': 'synthetic-secret' },
      body: JSON.stringify({ update_id: 1001, message: { from: { id: 42 },
        chat: { id: 42, type: 'private' }, text: '/assets' } }) });
    assert.equal((await worker.fetch(update, env)).status, 200);
    assert.equal(messages.length, 1);
    assert.ok(messages[0].includes('100'));
    assert.ok(brokerCalls.length > 0);
    assert.equal(db.rows('cloud_outbox')[0].status, 'SENT');
  });
