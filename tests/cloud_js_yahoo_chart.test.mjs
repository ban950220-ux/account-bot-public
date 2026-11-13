import assert from 'node:assert/strict';
import { test } from 'node:test';
import { FakeD1 } from './cloud_js_fake_d1.mjs';
import { createYahooChartReadOnly } from '../deploy/cloudflare-js/yahoo-chart-readonly.mjs';
import { putMarketCache } from '../deploy/cloudflare-js/market-cache.mjs';

const chart = (price, currency) => ({ chart: { result: [{ meta: {
  regularMarketPrice: price, regularMarketTime: 1769990400, currency,
} }] } });
const response = value => ({ ok: true, headers: new Headers(), json: async () => value });

test('public chart read uses fixed host, caches price and exact FX, then marks stale', async () => {
  const db = new FakeD1();
  let clock = '2026-02-02T00:00:00Z';
  const urls = [];
  let fail = false;
  const provider = createYahooChartReadOnly({ db, now: () => clock,
    fetcher: async (url, options) => {
      assert.equal(options.method, 'GET');
      assert.equal(options.redirect, 'manual');
      assert.ok(url.startsWith('https://query1.finance.yahoo.com/v8/finance/chart/'));
      urls.push(url);
      if (fail) throw new Error('synthetic outage');
      return response(chart(url.includes('KRW%3DX') ? 1420.125 : 101.25,
        url.includes('KRW%3DX') ? 'KRW' : 'USD'));
    } });
  const quote = await provider.getPrice('NASDAQ', 'FIXTURE');
  assert.equal(quote.price, '101.25');
  assert.equal(quote.stale, false);
  assert.equal((await provider.getFx('USD', 'KRW')).rate, '1420.125');
  assert.equal(urls.length, 2);
  fail = true;
  assert.equal((await provider.getPrice('NASDAQ', 'FIXTURE')).stale, false);
  assert.equal(urls.length, 2);
  clock = '2026-02-02T00:16:00Z';
  const stale = await provider.getPrice('NASDAQ', 'FIXTURE');
  assert.equal(stale.stale, true);
  assert.equal(urls.length, 3);
  assert.equal(db.rows('cloud_market_cache').length, 2);
});

test('missing chart time, price and pair fail without creating a fake quote', async () => {
  const db = new FakeD1();
  const provider = createYahooChartReadOnly({ db, now: () => '2026-02-02T00:00:00Z',
    fetcher: async () => response({ chart: { result: [{ meta: {
      regularMarketPrice: null, currency: 'USD',
    } }] } }) });
  await assert.rejects(() => provider.getPrice('NASDAQ', 'FIXTURE'), /unavailable/);
  await assert.rejects(() => provider.getFx('EUR', 'KRW'), /unsupported/);
  assert.equal(db.rows('cloud_market_cache').length, 0);
});

test('concurrent normalized aliases share one in-flight request and bulk cache round trips', async () => {
  const db = new FakeD1();
  const batch = db.batch.bind(db);
  let batches = 0;
  db.batch = statements => { batches++; return batch(statements); };
  const urls = [];
  const provider = createYahooChartReadOnly({ db, now: () => '2026-02-02T00:00:00Z',
    fetcher: async url => { urls.push(url); return response(chart(101.25, 'USD')); } });
  const rows = await Promise.all([provider.getPrice('US', 'fixture'),
    provider.getPrice('NASDAQ', 'FIXTURE'), provider.getPrice('NYSE', 'OTHER')]);
  assert.equal(urls.length, 2);
  assert.equal(batches, 2);
  assert.equal(rows[0].price, rows[1].price);
  assert.equal(rows[0].market, 'US');
  assert.equal(rows[1].market, 'NASDAQ');
  assert.equal(db.rows('cloud_market_cache').length, 2);
});

test('provider outage re-reads a concurrent fresh cache update instead of its old stale value', async () => {
  const db = new FakeD1();
  const key = [...'FIXTURE'].map(c => c.charCodeAt(0).toString(16).padStart(2, '0')).join('');
  const entry = { provider: 'yahoo-chart', namespace: 'price', cacheKey: key,
    value: { price: '80', currency: 'USD', provider: 'yahoo-chart', asOf: '2026-02-01T00:00:00Z' },
    fetchedAt: '2026-02-01T00:00:00Z', expiresAt: '2026-02-01T00:15:00Z',
    staleUntil: '2026-02-04T00:00:00Z' };
  await putMarketCache(db, entry);
  const provider = createYahooChartReadOnly({ db, now: () => '2026-02-02T00:00:00Z',
    fetcher: async () => {
      await putMarketCache(db, { ...entry, value: { ...entry.value, price: '120' },
        fetchedAt: '2026-02-02T00:00:00Z', expiresAt: '2026-02-02T00:15:00Z' });
      throw new Error('synthetic request lost to another invocation');
    } });
  const quote = await provider.getPrice('NASDAQ', 'FIXTURE');
  assert.equal(quote.price, '120');
  assert.equal(quote.stale, false);
});

test('mixed provider failure and failed cache persistence keep fresh quotes and stale fallback distinct', async () => {
  const db = new FakeD1();
  const key = [...'STALE'].map(c => c.charCodeAt(0).toString(16).padStart(2, '0')).join('');
  await putMarketCache(db, { provider: 'yahoo-chart', namespace: 'price', cacheKey: key,
    value: { price: '80', currency: 'USD', provider: 'yahoo-chart', asOf: '2026-02-01T00:00:00Z' },
    fetchedAt: '2026-02-01T00:00:00Z', expiresAt: '2026-02-01T00:15:00Z',
    staleUntil: '2026-02-04T00:00:00Z' });
  const batch = db.batch.bind(db);
  db.batch = statements => {
    if (statements.some(s => s.fixtureWrite)) throw new Error('synthetic persistence outage');
    return batch(statements);
  };
  const prepare = db.prepare.bind(db);
  db.prepare = sql => {
    const statement = prepare(sql);
    if (!sql.includes('INSERT INTO cloud_market_cache')) return statement;
    return { bind: (...values) => ({ ...statement.bind(...values), fixtureWrite: true }) };
  };
  const provider = createYahooChartReadOnly({ db, now: () => '2026-02-02T00:00:00Z',
    fetcher: async url => {
      if (url.includes('/STALE?')) throw new Error('synthetic provider outage');
      return response(chart(120, 'USD'));
    } });
  const [fresh, stale] = await Promise.all([provider.getPrice('US', 'FRESH'), provider.getPrice('US', 'STALE')]);
  assert.equal(fresh.price, '120');
  assert.equal(fresh.stale, false);
  assert.equal(stale.price, '80');
  assert.equal(stale.stale, true);
  assert.equal(db.rows('cloud_market_cache').length, 1);
});

test('failed fallback read does not discard another symbol fetched successfully in the same group', async () => {
  const db = new FakeD1();
  const batch = db.batch.bind(db);
  let batches = 0;
  db.batch = statements => {
    if (++batches === 3) throw new Error('synthetic fallback read failed');
    return batch(statements);
  };
  const provider = createYahooChartReadOnly({ db, now: () => '2026-02-02T00:00:00Z',
    fetcher: async url => {
      if (url.includes('/MISSING?')) throw new Error('synthetic provider outage');
      return response(chart(120, 'USD'));
    } });
  const [fresh, missing] = await Promise.allSettled([provider.getPrice('US', 'FRESH'), provider.getPrice('US', 'MISSING')]);
  assert.equal(fresh.status, 'fulfilled');
  assert.equal(fresh.value.price, '120');
  assert.equal(missing.status, 'rejected');
});
