// The only network surface in this adapter is token issuance and account reads.
import { normalizeKiwoomCash, normalizeKiwoomPositions,
  normalizeKiwoomUsCash } from './rest-normalizers.mjs';
import { compare, decimal, exact, multiply } from './finance.mjs';

const ORIGINS = new Set(['https://api.kiwoom.com', 'https://mockapi.kiwoom.com']);
const QUERIES = new Map([
  ['kt00018', ['/api/dostk/acnt', { qry_tp: '1', dmst_stex_tp: 'KRX' }]],
  ['kt00001', ['/api/dostk/acnt', { qry_tp: '2' }]],
  ['ust21070', ['/api/us/acnt', { stex_tp: '', stk_cd: '' }]],
  ['ust21110', ['/api/us/acnt', {}]],
  ['ka10076', ['/api/dostk/acnt', { stk_cd: '', qry_tp: '0', sell_tp: '0',
    ord_no: '', stex_tp: '0' }]],
  ['ust21510', ['/api/us/acnt', { slby_tp: '0', stex_tp: '', stk_cd: '' }]],
]);

function classified(error) {
  if (error?.status === 401 || error?.status === 403) return 'auth_failed';
  if (error?.status === 429) return 'rate_limited';
  return 'unavailable';
}

export function createKiwoomReadOnlyProvider({ name, accountId, appKey, appSecret,
  origin = 'https://api.kiwoom.com', fetcher = fetch, mock = false,
  now = () => new Date().toISOString() }) {
  if (!['kiwoom_main', 'kiwoom_accum'].includes(name) ||
      typeof accountId !== 'string' || !accountId || /^\d{8,}$/.test(accountId) ||
      typeof appKey !== 'string' || !appKey || typeof appSecret !== 'string' || !appSecret ||
      !ORIGINS.has(origin) || (mock && origin !== 'https://mockapi.kiwoom.com') ||
      typeof fetcher !== 'function' || typeof now !== 'function') {
    throw new TypeError('Kiwoom read-only configuration required');
  }
  let token = null;
  let expiresAt = 0;
  async function post(path, body, headers = {}) {
    const response = await fetcher(`${origin}${path}`, { method: 'POST', redirect: 'manual',
      headers: { 'Content-Type': 'application/json;charset=UTF-8', ...headers },
      body: JSON.stringify(body) });
    if (!response?.ok) {
      const error = new Error('Kiwoom read failed'); error.status = response?.status;
      throw error;
    }
    const payload = await response.json();
    if (!payload || typeof payload !== 'object' ||
        (payload.return_code !== undefined && payload.return_code !== 0)) {
      throw new TypeError('invalid Kiwoom response');
    }
    return { payload, headers: response.headers };
  }
  async function accessToken() {
    if (token && Date.now() < expiresAt) return token;
    const { payload } = await post('/oauth2/token', { grant_type: 'client_credentials',
      appkey: appKey, secretkey: appSecret });
    if (typeof payload.token !== 'string' || !payload.token) {
      throw new TypeError('incomplete Kiwoom token response');
    }
    token = payload.token;
    expiresAt = Date.now() + 23 * 3600_000;
    return token;
  }
  async function pages(tr) {
    const query = QUERIES.get(tr);
    if (!query) throw new TypeError('read-only TR required');
    const bearer = await accessToken();
    const output = [];
    let continued = 'N';
    let nextKey = '';
    for (let index = 0; index < 50; index += 1) {
      const response = await post(query[0], query[1], { authorization: `Bearer ${bearer}`,
        'cont-yn': continued, 'next-key': nextKey, 'api-id': tr });
      output.push(response.payload);
      if (response.headers.get('cont-yn')?.toUpperCase() !== 'Y') return output;
      nextKey = response.headers.get('next-key') || '';
      if (!nextKey) throw new TypeError('Kiwoom pagination key missing');
      continued = 'Y';
    }
    throw new TypeError('Kiwoom pagination limit exceeded');
  }
  async function collect(domestic, overseas, normalizeDomestic, normalizeOverseas) {
    let items = [];
    const errors = [];
    let successes = 0;
    try { items.push(...await normalizeDomestic(await pages(domestic))); successes += 1; }
    catch (error) { errors.push(classified(error)); }
    if (!mock) {
      try { items.push(...await normalizeOverseas(await pages(overseas))); successes += 1; }
      catch (error) { errors.push(classified(error)); }
    }
    return { broker: name, status: errors.length ? successes ? 'partial' :
      errors.includes('auth_failed') ? 'auth_failed' :
        errors.includes('rate_limited') ? 'rate_limited' : 'unavailable' : 'ok',
    items, ...(errors.length ? { safeErrorType: 'KiwoomReadFailed',
      missingAccounts: [accountId] } : {}) };
  }
  const identity = { broker: name, accountId };
  function localDay() {
    const parts = Object.fromEntries(new Intl.DateTimeFormat('en-US', {
      timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit',
    }).formatToParts(new Date(now())).map(part => [part.type, part.value]));
    return `${parts.year}-${parts.month}-${parts.day}`;
  }
  async function fills(rawPages, key, marketRegion, day) {
    const output = [];
    const seen = new Set();
    for (const page of rawPages) {
      if (!Array.isArray(page?.[key])) throw new TypeError('Kiwoom executions missing');
      for (const row of page[key]) {
        const quantity = decimal(String(row.cntr_qty ?? ''));
        if (compare(quantity, decimal('0')) <= 0) continue;
        const domestic = marketRegion === 'KR';
        const rawTicker = String(row.stk_cd ?? '').trim().toUpperCase();
        const ticker = domestic && /^A\d+$/.test(rawTicker) ? rawTicker.slice(1) : rawTicker;
        if (!/^[A-Z0-9.\-]{1,40}$/.test(ticker)) throw new TypeError('invalid execution ticker');
        const clock = String(domestic ? row.ord_tm ?? '' :
          row.cntr_time ?? row.ord_time ?? '').replaceAll(':', '').trim();
        if (!/^\d{6}$/.test(clock) || Number(clock.slice(0, 2)) > 23 ||
            Number(clock.slice(2, 4)) > 59 || Number(clock.slice(4, 6)) > 59) {
          throw new TypeError('execution time unavailable');
        }
        const tradedAt = new Date(`${day}T${clock.slice(0, 2)}:${clock.slice(2, 4)}:` +
          `${clock.slice(4, 6)}+09:00`).toISOString();
        const sideText = String(domestic ? row.io_tp_nm ?? '' :
          row.slby_tp_nm ?? row.slby_tp ?? '');
        const transactionType = sideText.includes('매수') || !domestic && sideText === '2' ?
          'BUY' : sideText.includes('매도') || !domestic && sideText === '1' ? 'SELL' : null;
        if (!transactionType) throw new TypeError('execution side unavailable');
        const currency = domestic ? 'KRW' : String(row.crnc_code ?? 'USD').toUpperCase();
        if (!['KRW', 'USD'].includes(currency)) throw new TypeError('execution currency unsupported');
        const price = decimal(String(domestic ? row.cntr_pric ?? '' : row.cntr_uv ?? ''));
        if (compare(price, decimal('0')) < 0) throw new TypeError('negative execution price');
        const exchange = String(row.stex_nm || 'US').toUpperCase();
        const market = domestic ? 'KRX' : exchange.includes('NASDAQ') ? 'NASDAQ' :
          exchange.includes('NYSE') ? 'NYSE' : exchange.includes('AMEX') ? 'AMEX' : 'US';
        const orderId = String(row.ord_no ?? '').trim();
        if (!orderId) throw new TypeError('execution order identifier unavailable');
        const identityText = JSON.stringify([day, marketRegion, orderId, clock, ticker,
          exact(quantity), exact(price), transactionType]);
        if (seen.has(identityText)) throw new TypeError('ambiguous duplicate execution');
        seen.add(identityText);
        const keyBytes = new Uint8Array(await crypto.subtle.digest('SHA-256',
          new TextEncoder().encode(identityText)));
        const sourceKey = `kiwoom:${marketRegion}:` + [...keyBytes]
          .map(byte => byte.toString(16).padStart(2, '0')).join('');
        const optional = value => value === null || value === undefined || value === '' ?
          null : exact(decimal(String(value)));
        output.push({ broker: name, accountId, sourceKey, source: name,
          instrument: { market, ticker, assetType: 'EQUITY', currency },
          currency, transactionType, status: 'FILLED', tradedAt,
          settledAt: null, quantity: exact(quantity), unitPrice: exact(price),
          grossAmount: exact(multiply(quantity, price)),
          fee: domestic ? optional(row.tdy_trde_cmsn) : null,
          tax: domestic ? optional(row.tdy_trde_tax) : null, netAmount: null });
      }
    }
    return output;
  }
  return { name, accountId,
    getAccounts: async () => ({ broker: name, status: 'ok', items: [
      { broker: name, accountId, displayName: '등록 계좌', baseCurrency: 'KRW' }] }),
    getPositions: () => collect('kt00018', 'ust21070', kr =>
      normalizeKiwoomPositions(kr, [], identity), us =>
      normalizeKiwoomPositions([], us, identity)),
    getCash: () => collect('kt00001', 'ust21110', kr =>
      normalizeKiwoomCash(kr, [], identity), us => normalizeKiwoomUsCash(us, identity)),
    getTransactions: ({ startDate, endDate }) => {
      if (typeof startDate !== 'string' || typeof endDate !== 'string' ||
          !/^\d{4}-\d{2}-\d{2}$/.test(startDate) ||
          !/^\d{4}-\d{2}-\d{2}$/.test(endDate) || startDate > endDate) {
        throw new TypeError('valid execution date range required');
      }
      const day = localDay();
      if (day < startDate || day > endDate) return Promise.resolve({
        broker: name, status: 'ok', items: [], historyScope: 'SAME_DAY_ONLY' });
      return collect('ka10076', 'ust21510', kr => fills(kr, 'cntr', 'KR', day),
        us => fills(us, 'result_list', 'US', day)).then(result => ({ ...result,
        historyScope: 'SAME_DAY_ONLY' }));
    },
  };
}
