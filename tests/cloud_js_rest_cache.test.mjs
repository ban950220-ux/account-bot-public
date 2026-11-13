import test from 'node:test';
import assert from 'node:assert/strict';
import { FakeD1 } from './cloud_js_fake_d1.mjs';
import { getMarketCache, putMarketCache, getMarketCaches, putMarketCaches } from '../deploy/cloudflare-js/market-cache.mjs';
import { normalizeKiwoomPositions, normalizeKiwoomCash, normalizeTossAccounts,
  normalizeTossPositions, normalizeTossCash } from '../deploy/cloudflare-js/rest-normalizers.mjs';

const identity = { broker: 'kiwoom_main', accountId: 'local-main' };
const domestic = { acnt_evlt_remn_indv_tot: [{ stk_cd: 'A123456', rmnd_qty: '2',
  pur_pric: '-1,001.25', cur_prc: '-1,002.50', pur_amt: '2,002.50',
  evlt_amt: '2,005.00', evltv_prft: '2.50', prft_rt: '0.124', stk_nm: 'Fixture' }] };
const toss = { result: { items: [{ marketCountry: 'US', currency: 'USD', symbol: 'TEST',
  quantity: '0.125', averagePurchasePrice: '80.10', lastPrice: '81.20',
  marketValue: { purchaseAmount: '10.0125', amount: '10.1500' },
  profitLoss: { amount: '0.1375', rate: '0.0137328' } }] } };

test('REST responses retain exact decimal values and local source scope', () => {
  const k = normalizeKiwoomPositions([domestic], [{ result_list: [] }], identity);
  assert.equal(k[0].instrument.ticker, '123456');
  assert.equal(k[0].averagePrice, '1001.25');
  assert.equal(k[0].marketValue, '2005.00');
  assert.deepEqual(normalizeKiwoomCash([{ entr: '1,000' }], [{ result_list: [] }], identity),
    [{ ...identity, currency: 'KRW', amount: '1000' }]);
  assert.deepEqual(normalizeTossAccounts({ result: [{ accountSeq: '987654321' }] },
    () => 'local-toss'), [{ accountId: 'local-toss', accountType: '계좌' }]);
  const position = normalizeTossPositions(toss, 'local-toss')[0];
  assert.equal(position.returnPct, '1.3732800');
  assert.equal(position.marketValue, '10.1500');
  assert.equal(JSON.stringify(position).includes('987654321'), false);
  assert.deepEqual(normalizeTossCash({ result: { cashBuyingPower: '5.50' } },
    'local-toss', 'USD'), { broker: 'toss', accountId: 'local-toss', currency: 'USD',
    amount: '5.50' });
});

test('partial or malformed broker payload cannot become zero or a complete result', () => {
  assert.throws(() => normalizeKiwoomPositions([{}], [], identity), /required list/);
  assert.throws(() => normalizeKiwoomCash([{}], [], identity), /required broker amount/);
  assert.throws(() => normalizeTossPositions({ result: {} }, 'local-toss'), /required list/);
  assert.throws(() => normalizeTossPositions({ result: { items: [
    { ...toss.result.items[0], marketValue: { amount: '10' } }] } }, 'local-toss'),
  /required broker amount/);
  assert.throws(() => normalizeTossAccounts({ result: [{ accountSeq: '987654321' }] },
    seq => seq), /local account alias/);
});

test('D1 market cache keeps public values, TTL and explicit stale state', async () => {
  const db = new FakeD1();
  const row = { provider: 'fixture', namespace: 'WEB_FX', cacheKey: 'USD:KRW',
    value: { rate: '1234.5678', asOf: '2026-02-01T00:00:00Z' },
    fetchedAt: '2026-02-01T00:00:00Z', expiresAt: '2026-02-01T00:10:00Z',
    staleUntil: '2026-02-08T00:00:00Z' };
  await putMarketCache(db, row);
  assert.equal((await getMarketCache(db, { ...row, now: '2026-02-01T00:10:00Z' })).isStale, false);
  assert.equal(await getMarketCache(db, { ...row, now: '2026-02-02T00:00:00Z' }), null);
  assert.deepEqual((await getMarketCache(db, { ...row, now: '2026-02-02T00:00:00Z',
    allowStale: true })).value, row.value);
  assert.equal(await getMarketCache(db, { ...row, now: '2026-02-09T00:00:00Z',
    allowStale: true }), null);
  await putMarketCache(db, { ...row, fetchedAt: '2026-01-31T00:00:00Z',
    expiresAt: '2026-01-31T00:10:00Z', value: { rate: '0' } });
  assert.deepEqual((await getMarketCache(db, { ...row, now: '2026-02-01T00:00:00Z' })).value,
    row.value);
  await assert.rejects(() => putMarketCache(db, { ...row, value: { token: 'private' } }),
    /private/);
});

test('bulk public cache reads/writes preserve order, timestamps and all-input validation', async () => {
  const db = new FakeD1();
  let batches = 0;
  const batch = db.batch.bind(db);
  db.batch = statements => { batches++; return batch(statements); };
  const entries = Array.from({ length: 101 }, (_, i) => ({
    provider: 'fixture', namespace: 'price', cacheKey: `FIX:${i}`,
    value: { price: String(i + 1) }, fetchedAt: '2026-02-01T00:00:00Z',
    expiresAt: '2026-02-01T00:15:00Z', staleUntil: '2026-02-04T00:00:00Z',
  }));
  await putMarketCaches(db, entries);
  const fresh = await getMarketCaches(db, entries.map(item => ({ ...item, now: '2026-02-01T00:15:00Z' })));
  assert.equal(batches, 2);
  assert.deepEqual(fresh.map(item => item.value), entries.map(item => item.value));
  await putMarketCaches(db, entries.map(item => ({ ...item, value: { price: '0' },
    fetchedAt: '2026-01-31T00:00:00Z', expiresAt: '2026-01-31T00:15:00Z' })));
  const stale = await getMarketCaches(db, entries.map(item => ({ ...item,
    now: '2026-02-04T00:00:00Z', allowStale: true })));
  assert.ok(stale.every(item => item.isStale));
  assert.deepEqual(stale.map(item => item.value), entries.map(item => item.value));
  assert.deepEqual(await getMarketCaches(db, [{ ...entries[0],
    now: '2026-02-04T00:00:00.001Z', allowStale: true }]), [null]);
  const before = batches;
  await assert.rejects(() => putMarketCaches(db, [...entries, { ...entries[0], value: { token: 'private' } }]), /private/);
  await assert.rejects(() => getMarketCaches(db, [{ ...entries[0], now: 'invalid' }]), /timestamp/);
  assert.equal(batches, before);
  assert.equal((await getMarketCache(db, { ...entries[0], now: '2026-02-01T00:00:00Z' })).value.price, '1');
});
