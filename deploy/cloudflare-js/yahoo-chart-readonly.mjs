// Candidate parity with the existing Python Yahoo chart read. No private data is sent.
import { compare, decimal, exact } from './finance.mjs';
import { getMarketCaches, putMarketCaches } from './market-cache.mjs';

const HOST = 'https://query1.finance.yahoo.com';
const SYMBOL = /^[A-Z0-9.^=\-]{1,48}$/;
function yahooSymbol(market, ticker) {
  const name = String(market).toUpperCase();
  let symbol = String(ticker).toUpperCase();
  if (['KOSPI', 'KOSDAQ', 'KRX', 'NXT'].includes(name) && /^A\d+$/.test(symbol)) {
    symbol = symbol.slice(1);
  }
  if (name === 'KOSPI' && /^\d+$/.test(symbol)) symbol = `${symbol.padStart(6, '0')}.KS`;
  if (name === 'KOSDAQ' && /^\d+$/.test(symbol)) symbol = `${symbol.padStart(6, '0')}.KQ`;
  if (!SYMBOL.test(symbol)) throw new TypeError('invalid public symbol');
  return symbol;
}

export function createYahooChartReadOnly({ db, fetcher = fetch,
  now = () => new Date().toISOString() }) {
  if (!db || typeof fetcher !== 'function' || typeof now !== 'function') {
    throw new TypeError('public market cache and clock required');
  }
  function cacheOptions(symbol, allowStale = false) {
    const cacheKey = [...symbol].map(char => char.charCodeAt(0)
      .toString(16).padStart(2, '0')).join('');
    return { provider: 'yahoo-chart', namespace: 'price', cacheKey, now: now(), allowStale };
  }
  async function fetchQuote(symbol) {
    const url = `${HOST}/v8/finance/chart/${encodeURIComponent(symbol)}` +
      '?range=5d&interval=1d';
    const response = await fetcher(url, { method: 'GET', redirect: 'manual' });
    if (!response?.ok || Number(response.headers?.get('content-length')) > 1048576) {
      throw new TypeError('public chart unavailable');
    }
    const payload = await response.json();
    const result = payload?.chart?.result?.[0];
    const meta = result?.meta;
    let rawPrice = meta?.regularMarketPrice;
    if (rawPrice === null || rawPrice === undefined ||
        compare(decimal(String(rawPrice)), decimal('0')) <= 0) {
      rawPrice = [...(result?.indicators?.quote?.[0]?.close || [])].reverse()
        .find(value => value != null && compare(decimal(String(value)), decimal('0')) > 0);
    }
    if (rawPrice === null || rawPrice === undefined ||
        compare(decimal(String(rawPrice)), decimal('0')) <= 0 ||
        !Number.isSafeInteger(meta?.regularMarketTime) || meta.regularMarketTime <= 0 ||
        !['USD', 'KRW'].includes(String(meta.currency).toUpperCase())) {
      throw new TypeError('public chart missing price, time or currency');
    }
    const value = { price: exact(decimal(String(rawPrice))),
      currency: String(meta.currency).toUpperCase(),
      asOf: new Date(meta.regularMarketTime * 1000).toISOString(),
      provider: 'yahoo-chart' };
    return value;
  }

  // The gateway requests all plan analyses concurrently. Coalesce that group into
  // one read/write batch and share in-flight work for aliases of the same symbol.
  // This state belongs to this provider instance (one operating invocation).
  const pending = new Map();
  let queued = [];
  async function loadGroup(group) {
    const cached = await getMarketCaches(db, group.map(item => cacheOptions(item.symbol)));
    const writes = [];
    const failures = [];
    await Promise.all(group.map(async (item, index) => {
      if (cached[index]) { item.value = { ...cached[index].value, stale: false }; return; }
      try {
        const value = await fetchQuote(item.symbol);
        const fetched = new Date(now());
        writes.push({ ...cacheOptions(item.symbol), value, fetchedAt: fetched.toISOString(),
          expiresAt: new Date(fetched.getTime() + 15 * 60000).toISOString(),
          staleUntil: new Date(fetched.getTime() + 3 * 86400000).toISOString() });
        item.value = { ...value, stale: false };
      } catch { failures.push(item); }
    }));
    try { await putMarketCaches(db, writes); }
    catch { /* Fresh public quotes remain usable if cache persistence fails. */ }
    if (failures.length) {
      // Re-read after provider failures: another invocation may have refreshed
      // the cache while this request was in flight. Preserve its freshness.
      let fallbacks;
      try {
        fallbacks = await getMarketCaches(db,
          failures.map(item => cacheOptions(item.symbol, true)));
      } catch (error) {
        for (const item of failures) item.error = error;
        return;
      }
      failures.forEach((item, index) => {
        const fallback = fallbacks[index];
        if (fallback) item.value = { ...fallback.value, stale: fallback.isStale };
        else item.error = new TypeError('public chart unavailable');
      });
    }
  }
  async function flush() {
    const group = queued;
    queued = [];
    try { await loadGroup(group); }
    catch (error) { for (const item of group) item.error = error; }
    for (const item of group) {
      pending.delete(item.symbol);
      if (item.error) item.reject(item.error);
      else item.resolve(item.value);
    }
  }
  function read(symbol) {
    if (pending.has(symbol)) return pending.get(symbol);
    const promise = new Promise((resolve, reject) => queued.push({ symbol, resolve, reject }));
    pending.set(symbol, promise);
    if (queued.length === 1) queueMicrotask(flush);
    return promise;
  }
  return {
    name: 'yahoo-chart',
    getPrice: async (market, ticker) => ({ market, ticker,
      ...await read(yahooSymbol(market, ticker)) }),
    getFx: async (base, quote) => {
      if (base !== 'USD' || quote !== 'KRW') throw new TypeError('unsupported FX pair');
      const value = await read('KRW=X');
      if (value.currency !== 'KRW') throw new TypeError('FX quote currency mismatch');
      return { base, quote, rate: value.price, asOf: value.asOf,
        provider: value.provider, isStale: value.stale };
    },
  };
}
