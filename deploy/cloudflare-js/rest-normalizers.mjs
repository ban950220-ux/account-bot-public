// Pure response mapping for the existing read-only REST contracts. No HTTP or credentials.
import { decimal, exact, multiply } from './finance.mjs';

function requiredRows(page, key) {
  if (!page || !Array.isArray(page[key])) throw new TypeError(`required list missing: ${key}`);
  return page[key];
}
function requiredObject(page, key) {
  if (!page || !page[key] || typeof page[key] !== 'object' || Array.isArray(page[key])) {
    throw new TypeError(`required object missing: ${key}`);
  }
  return page[key];
}
function amount(value, absolute = false) {
  if (value === null || value === undefined || String(value).trim() === '') {
    throw new TypeError('required broker amount missing');
  }
  const parsed = decimal(String(value).trim().replaceAll(',', ''));
  return exact(absolute && parsed.coefficient < 0n ?
    { ...parsed, coefficient: -parsed.coefficient } : parsed);
}
function source(broker, accountId) {
  if (!/^[a-z_]+$/.test(broker) || typeof accountId !== 'string' ||
      !accountId || /^\d{8,}$/.test(accountId)) throw new TypeError('local account alias required');
  return { broker, accountId };
}
function instrument(market, ticker, currency) {
  if (!['KRW', 'USD'].includes(currency) || !/^[A-Z0-9.\-]+$/.test(ticker)) {
    throw new TypeError('unsupported instrument');
  }
  return { market, ticker, assetType: 'EQUITY', currency };
}

export function normalizeKiwoomPositions(domesticPages, usPages, { broker, accountId }) {
  const base = source(broker, accountId);
  const rows = [];
  for (const page of domesticPages) for (const row of requiredRows(page, 'acnt_evlt_remn_indv_tot')) {
    const raw = String(row.stk_cd ?? '').trim().toUpperCase();
    const ticker = /^A\d+$/.test(raw) ? raw.slice(1) : raw;
    if (!ticker) continue; // Matches the existing Python adapter.
    rows.push({ ...base, instrument: instrument('KRX', ticker, 'KRW'),
      quantity: amount(row.rmnd_qty), averagePrice: amount(row.pur_pric, true),
      currentPrice: amount(row.cur_prc, true), costBasis: amount(row.pur_amt, true),
      marketValue: amount(row.evlt_amt, true), unrealizedProfit: amount(row.evltv_prft),
      returnPct: amount(row.prft_rt), companyName: String(row.stk_nm ?? '').trim() });
  }
  for (const page of usPages) for (const row of requiredRows(page, 'result_list')) {
    const ticker = String(row.stk_cd ?? '').trim().toUpperCase();
    if (!ticker) continue;
    const exchange = String(row.stex_nm || 'US').trim().toUpperCase();
    const market = exchange.includes('NASDAQ') || exchange === 'NAS' ? 'NASDAQ' :
      exchange.includes('NYSE') ? 'NYSE' : exchange.includes('AMEX') ? 'AMEX' : 'US';
    rows.push({ ...base, instrument: instrument(market, ticker,
      String(row.crnc_code || 'USD').toUpperCase()), quantity: amount(row.poss_qty),
    averagePrice: amount(row.frgn_stk_book_uv, true), currentPrice: amount(row.now_pric, true),
    costBasis: amount(row.frgn_stk_book_amt, true), marketValue: amount(row.evlt_amt, true),
    unrealizedProfit: amount(row.pl_amt), returnPct: amount(row.pl_rt),
    companyName: String(row.frgn_stk_nm ?? '').trim() });
  }
  return rows;
}

export function normalizeKiwoomCash(domesticPages, usPages, identity) {
  const base = source(identity.broker, identity.accountId);
  if (domesticPages.length !== 1) throw new TypeError('unexpected Kiwoom cash pagination');
  const rows = [{ ...base, currency: 'KRW', amount: amount(domesticPages[0].entr) }];
  rows.push(...normalizeKiwoomUsCash(usPages, identity));
  return rows;
}

export function normalizeKiwoomUsCash(usPages, identity) {
  const base = source(identity.broker, identity.accountId);
  const rows = [];
  for (const page of usPages) for (const row of requiredRows(page, 'result_list')) {
    const currency = String(row.crnc_code ?? '').trim().toUpperCase();
    if (currency === 'KRW' || currency === 'USD') {
      rows.push({ ...base, currency, amount: amount(row.fc_entra) });
    }
  }
  return rows;
}

export function normalizeTossAccounts(page, aliasForSeq) {
  if (typeof aliasForSeq !== 'function') throw new TypeError('account alias resolver required');
  const rows = requiredRows(page, 'result').map(row => {
    const sequence = row?.accountSeq;
    if (sequence === null || sequence === undefined || String(sequence).trim() === '') {
      throw new TypeError('Toss account sequence missing');
    }
    const accountId = aliasForSeq(String(sequence));
    source('toss', accountId);
    if (accountId === String(sequence)) throw new TypeError('raw account sequence is not an alias');
    return { accountId, accountType: String(row.accountType || '계좌') };
  });
  if (!rows.length) throw new TypeError('no linked Toss account');
  return rows;
}

export function normalizeTossPositions(page, accountId) {
  const base = source('toss', accountId);
  return requiredRows(requiredObject(page, 'result'), 'items').map(row => {
    const country = String(row.marketCountry || '').toUpperCase();
    const market = ['KR', 'KOR'].includes(country) ? 'KRX' :
      ['US', 'USA'].includes(country) ? 'US' : null;
    if (!market) throw new TypeError('unsupported Toss market');
    const currency = String(row.currency || '').toUpperCase();
    const value = requiredObject(row, 'marketValue');
    const pnl = requiredObject(row, 'profitLoss');
    return { ...base, instrument: instrument(market,
      String(row.symbol || '').trim().toUpperCase(), currency),
    quantity: amount(row.quantity), averagePrice: amount(row.averagePurchasePrice, true),
    currentPrice: amount(row.lastPrice, true), costBasis: amount(value.purchaseAmount, true),
    marketValue: amount(value.amount, true), unrealizedProfit: amount(pnl.amount),
    returnPct: exact(multiply(decimal(amount(pnl.rate)), decimal('100'))),
    companyName: String(row.name || '').trim() };
  });
}

export function normalizeTossCash(page, accountId, currency) {
  if (!['KRW', 'USD'].includes(currency)) throw new TypeError('unsupported currency');
  return { ...source('toss', accountId), currency,
    amount: amount(requiredObject(page, 'result').cashBuyingPower) };
}
