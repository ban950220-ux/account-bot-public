// Public market signals for recurring plans: long-term trend and valuation scores.
// Read-only public endpoints; only a ticker symbol is ever sent. Results are cached
// in D1 so one invocation never has to fetch every symbol (subrequest and CPU limits).
import { getMarketCaches, putMarketCaches } from './market-cache.mjs';
import { recurringSuitability, valuationComponent } from './scoring.mjs';

const HOST = 'https://query1.finance.yahoo.com';
const SYMBOL = /^[A-Z0-9.^=-]{1,48}$/;
const PROVIDER = 'yahoo-signal';
// The host answers 429 to requests without a browser-style User-Agent.
const HEADERS = { 'user-agent': 'Mozilla/5.0 (compatible; account-bot)',
  accept: 'application/json' };
const DAY = 86400000;
const FRESH_DAYS = 7;
const USABLE_DAYS = 21;
const SERIES = ['trailingForwardPeRatio', 'trailingPegRatio',
  'trailingEnterprisesValueEBITDARatio', 'trailingMarketCap', 'annualFreeCashFlow',
  'quarterlyDilutedAverageShares'];
const US = new Set(['US', 'USA', 'NASDAQ', 'NAS', 'NYSE', 'AMEX', 'ARCA', 'NYSEARCA']);

// Indicators are statistics, not money: binary floats are acceptable here and are
// converted to fixed decimal strings before they reach the exact scoring code.
const fixed = value => Number.isFinite(value) ? value.toFixed(8) : null;

export function signalSymbol(instrument) {
  const market = String(instrument.market).toUpperCase();
  let symbol = String(instrument.ticker).toUpperCase();
  if (US.has(market)) symbol = symbol.replaceAll('.', '-');
  else if (market === 'KOSPI' && /^\d+$/.test(symbol)) symbol = `${symbol.padStart(6, '0')}.KS`;
  else if (market === 'KOSDAQ' && /^\d+$/.test(symbol)) symbol = `${symbol.padStart(6, '0')}.KQ`;
  if (!SYMBOL.test(symbol)) throw new TypeError('invalid public symbol');
  return symbol;
}

// Weekly adjusted closes, oldest first. One year is 52 weeks, three years 156,
// and the 200-day average is approximated by the latest 40 weekly closes.
export function trendFromWeeklyCloses(closes) {
  const values = closes.filter(value => Number.isFinite(value) && value > 0);
  if (values.length < 60) return null;
  const year = values.slice(-53);
  const three = values.slice(-157);
  const years = (three.length - 1) / 52;
  let peak = three[0];
  let drawdown = 0;
  for (const value of three) {
    peak = Math.max(peak, value);
    drawdown = Math.min(drawdown, value / peak - 1);
  }
  const returns = year.slice(1).map((value, index) => value / year[index] - 1);
  const mean = returns.reduce((sum, value) => sum + value, 0) / returns.length;
  const variance = returns.reduce((sum, value) => sum + (value - mean) ** 2, 0) /
    Math.max(1, returns.length - 1);
  const recent = values.slice(-40);
  return { observationCount: values.length,
    return1y: fixed(year.at(-1) / year[0] - 1),
    cagr3y: years > 0 ? fixed(Math.exp(Math.log(three.at(-1) / three[0]) / years) - 1) : null,
    maxDrawdown3y: fixed(drawdown),
    annualizedVolatility1y: fixed(Math.sqrt(variance) * Math.sqrt(52)),
    above200dAverage: values.at(-1) >= recent.reduce((sum, value) => sum + value, 0) / recent.length };
}

export function valuationFromTimeseries(payload) {
  const series = new Map();
  for (const item of payload?.timeseries?.result || []) {
    const type = item?.meta?.type?.[0];
    const points = (item?.[type] || []).filter(point =>
      Number.isFinite(point?.reportedValue?.raw) && typeof point.asOfDate === 'string')
      .sort((a, b) => a.asOfDate < b.asOfDate ? -1 : 1);
    if (type && points.length) series.set(type, points);
  }
  const latest = type => series.get(type)?.at(-1)?.reportedValue.raw ?? null;
  const positive = value => value !== null && value > 0 ? value : null;
  const forward = (series.get('trailingForwardPeRatio') || [])
    .map(point => point.reportedValue.raw).filter(value => value > 0);
  const sortedForward = [...forward].sort((a, b) => a - b);
  const median = sortedForward.length >= 4 ?
    (sortedForward[Math.floor((sortedForward.length - 1) / 2)] +
      sortedForward[Math.ceil((sortedForward.length - 1) / 2)]) / 2 : null;
  const cap = positive(latest('trailingMarketCap'));
  const cash = latest('annualFreeCashFlow');
  const shares = series.get('quarterlyDilutedAverageShares') || [];
  const dilution = shares.length >= 5 && shares.at(-5).reportedValue.raw > 0 ?
    (shares.at(-1).reportedValue.raw / shares.at(-5).reportedValue.raw - 1) * 100 : null;
  const value = { forwardPe: fixed(positive(latest('trailingForwardPeRatio')) ?? NaN),
    peg: fixed(positive(latest('trailingPegRatio')) ?? NaN),
    enterpriseToEbitda: fixed(positive(latest('trailingEnterprisesValueEBITDARatio')) ?? NaN),
    freeCashFlowYield: cap !== null && cash !== null ? fixed(cash / cap) : null,
    historicalForwardPeMedian: fixed(median ?? NaN),
    dilutionPct: fixed(dilution ?? NaN) };
  return Object.values(value).some(item => item !== null) ? value : null;
}

const cacheKey = symbol => [...symbol].map(char => char.charCodeAt(0).toString(16)
  .padStart(2, '0')).join('');
const planId = plan => `${plan.instrument.market}:${plan.instrument.ticker}:` +
  `${plan.instrument.assetType}:${plan.instrument.currency}`;

function targets(plans) {
  const bySymbol = new Map();
  for (const plan of plans) {
    let symbol;
    try { symbol = signalSymbol(plan.instrument); } catch { continue; }
    const entry = bySymbol.get(symbol) || { symbol, ids: [], equity: false };
    entry.ids.push(planId(plan));
    if (plan.instrument.assetType === 'EQUITY') entry.equity = true;
    bySymbol.set(symbol, entry);
  }
  return [...bySymbol.values()];
}

function scored(value) {
  const trend = value.trend ? recurringSuitability(value.trend) : { score: null, reasons: [] };
  const valuation = value.valuation ? valuationComponent(value.valuation) : null;
  return { symbol: value.symbol, asOf: value.asOf, trendScore: trend.score,
    trendReasons: trend.reasons, valuationScore: valuation, forwardPe: value.valuation?.forwardPe ?? null };
}

export function createSignals({ db, fetcher = fetch, now = () => new Date().toISOString() }) {
  if (!db || typeof fetcher !== 'function' || typeof now !== 'function') {
    throw new TypeError('signal cache, transport and clock required');
  }
  const options = (symbol, allowStale) => ({ provider: PROVIDER, namespace: 'signal',
    cacheKey: cacheKey(symbol), now: now(), allowStale });
  async function json(url) {
    const response = await fetcher(url, { method: 'GET', redirect: 'manual',
      headers: HEADERS });
    if (!response?.ok || Number(response.headers?.get('content-length')) > 1048576) {
      const error = new TypeError('public signal source unavailable');
      error.reason = Number.isInteger(response?.status) ? `HTTP ${response.status}` : '응답 없음';
      throw error;
    }
    return response.json();
  }
  async function load(target) {
    const chart = await json(`${HOST}/v8/finance/chart/${encodeURIComponent(target.symbol)}` +
      '?range=5y&interval=1wk');
    const result = chart?.chart?.result?.[0];
    const closes = result?.indicators?.adjclose?.[0]?.adjclose ||
      result?.indicators?.quote?.[0]?.close || [];
    const trend = trendFromWeeklyCloses(closes);
    let valuation = null;
    // Funds publish no company valuation series; a wrongly typed fund is detected by the source.
    if (target.equity && result?.meta?.instrumentType === 'EQUITY') {
      const end = Math.floor(new Date(now()).getTime() / 1000);
      try {
        valuation = valuationFromTimeseries(await json(`${HOST}/ws/fundamentals-timeseries/v1/` +
          `finance/timeseries/${encodeURIComponent(target.symbol)}?type=${SERIES.join(',')}` +
          `&period1=${end - 3 * 365 * 86400}&period2=${end}`));
      } catch { /* Trend alone is still a usable signal. */ }
    }
    if (!trend && !valuation) throw new TypeError('public signal source returned no usable data');
    return { symbol: target.symbol, asOf: now(), trend, valuation };
  }
  return {
    // Cached signals by plan id. Entries past their usable window are omitted.
    async read(plans) {
      const list = targets(plans);
      const rows = await getMarketCaches(db, list.map(item => options(item.symbol, true)));
      const out = new Map();
      list.forEach((item, index) => {
        if (!rows[index]) return;
        const signal = { ...scored(rows[index].value), stale: rows[index].isStale };
        for (const id of item.ids) out.set(id, signal);
      });
      return out;
    },
    // Refreshes the stalest symbols first, spending at most `budget` HTTP requests
    // (a fund costs one, a company two). Call repeatedly to cover every plan.
    async refresh(plans, budget = 10) {
      if (!Number.isSafeInteger(budget) || budget < 0 || budget > 40) {
        throw new TypeError('invalid signal refresh budget');
      }
      const list = targets(plans);
      const rows = await getMarketCaches(db, list.map(item => options(item.symbol, true)));
      const due = list.map((item, index) => ({ item, row: rows[index] }))
        .filter(({ row }) => !row || row.isStale)
        .sort((a, b) => (a.row?.fetchedAt || '') < (b.row?.fetchedAt || '') ? -1 : 1);
      const chosen = [];
      let spent = 0;
      for (const { item } of due) {
        const cost = item.equity ? 2 : 1;
        if (spent + cost > budget) continue;
        spent += cost;
        chosen.push(item);
      }
      let refreshed = 0;
      let failed = 0;
      const reasons = new Set();
      // Each group is stored before the next starts, so an interrupted run keeps
      // its progress and a retry continues with the remaining symbols.
      for (let offset = 0; offset < chosen.length; offset += 4) {
        const writes = [];
        await Promise.all(chosen.slice(offset, offset + 4).map(async item => {
          try {
            const value = await load(item);
            const at = new Date(now()).getTime();
            writes.push({ provider: PROVIDER, namespace: 'signal', cacheKey: cacheKey(item.symbol),
              value, fetchedAt: new Date(at).toISOString(),
              expiresAt: new Date(at + FRESH_DAYS * DAY).toISOString(),
              staleUntil: new Date(at + USABLE_DAYS * DAY).toISOString() });
          } catch (error) {
            failed += 1;
            reasons.add(typeof error?.reason === 'string' ? error.reason : '데이터 부족');
          }
        }));
        await putMarketCaches(db, writes);
        refreshed += writes.length;
      }
      return { symbols: list.length, refreshed, failed, remaining: due.length - refreshed,
        reasons: [...reasons].sort() };
    },
  };
}
