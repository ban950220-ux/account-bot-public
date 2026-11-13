// Private, offline-testable Meritz document boundary. No public HTTP route is attached.
// A future upload adapter must authenticate the owner before passing bytes here.
import { compare, decimal, exact } from './finance.mjs';

const FORBIDDEN = new Set(['accountnumber', 'accountno', 'apikey', 'apisecret',
  'accesstoken', 'refreshtoken', 'password', 'authorization', 'clientsecret',
  'credential', 'pin']);
const TYPES = new Set(['EQUITY', 'ETF', 'FUND', 'BOND', 'CASH', 'OTHER']);
const CURRENCIES = new Set(['KRW', 'USD']);

function object(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new TypeError('document object required');
  }
  return value;
}
function rows(value, name) {
  if (!Array.isArray(value)) throw new TypeError(`${name} list required`);
  return value.map(object);
}
function sensitive(value) {
  if (Array.isArray(value)) return value.forEach(sensitive);
  if (value && typeof value === 'object') for (const [key, child] of Object.entries(value)) {
    const normalized = key.toLowerCase().replaceAll(/[^a-z0-9]/g, '');
    if (FORBIDDEN.has(normalized) || normalized.endsWith('secret') ||
        normalized.endsWith('token')) throw new TypeError('sensitive field forbidden');
    sensitive(child);
  }
}
function alias(value) {
  if (typeof value !== 'string' || !value.trim() || /^\d[\d-]{5,}$/.test(value.trim())) {
    throw new TypeError('local account alias required');
  }
  return value.trim();
}
function currency(value, fallback) {
  const code = String(value ?? fallback).toUpperCase();
  if (!CURRENCIES.has(code)) throw new TypeError('unsupported currency');
  return code;
}
function amount(value, { optional = false, nonnegative = false, positive = false } = {}) {
  if (value === null || value === undefined || String(value).trim() === '') {
    if (optional) return null;
    throw new TypeError('required amount missing');
  }
  const parsed = decimal(String(value).trim().replaceAll(',', ''));
  if ((nonnegative && compare(parsed, decimal('0')) < 0) ||
      (positive && compare(parsed, decimal('0')) <= 0)) throw new TypeError('invalid amount');
  return exact(parsed);
}
function iso(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T/.test(value) ||
      !/(?:Z|[+-]\d\d:\d\d)$/.test(value)) throw new TypeError('timestamp required');
  const timestamp = new Date(value);
  if (!Number.isFinite(timestamp.getTime())) throw new TypeError('invalid timestamp');
  return timestamp.toISOString();
}
function day(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value) ||
      new Date(`${value}T00:00:00Z`).toISOString().slice(0, 10) !== value) {
    throw new TypeError('invalid date');
  }
  return value;
}
function instrument(row, prefix = '') {
  const market = String(row.market ?? '').trim().toUpperCase();
  const ticker = String(row.ticker ?? '').trim().toUpperCase();
  const assetType = String(row.asset_type ?? 'EQUITY').toUpperCase();
  if (!/^[A-Z0-9_.:-]{1,40}$/.test(market) ||
      !/^[A-Z0-9.:-]{1,40}$/.test(ticker) || !TYPES.has(assetType)) {
    throw new TypeError(`${prefix} invalid instrument`);
  }
  return { market, ticker, asset_type: assetType };
}

export function normalizeMeritzAccountSnapshot(input) {
  const doc = object(input);
  sensitive(doc);
  const accounts = rows(doc.accounts, 'accounts').map(row => ({
    account_id: alias(row.account_id), display_name: String(row.display_name ?? ''),
    base_currency: currency(row.base_currency, 'KRW'),
  }));
  if (!accounts.length || new Set(accounts.map(row => row.account_id)).size !== accounts.length) {
    throw new TypeError('missing or duplicate account');
  }
  const ids = new Set(accounts.map(row => row.account_id));
  const scoped = row => {
    const account_id = alias(row.account_id);
    if (!ids.has(account_id)) throw new TypeError('row account not in snapshot accounts');
    return account_id;
  };
  const positions = rows(doc.positions, 'positions').map(row => ({
    account_id: scoped(row), ...instrument(row), currency: currency(row.currency, 'KRW'),
    quantity: amount(row.quantity, { nonnegative: true }),
    average_price: amount(row.average_price, { optional: true, nonnegative: true }),
    current_price: amount(row.current_price, { optional: true, nonnegative: true }),
    cost_basis: amount(row.cost_basis, { optional: true, nonnegative: true }),
    market_value: amount(row.market_value, { optional: true, nonnegative: true }),
    company_name: String(row.company_name ?? ''),
    as_of: row.as_of == null ? null : iso(row.as_of),
  }));
  // Older explicit exports may omit whole sections. Preserve omission as unknown,
  // never normalize it to an observed empty balance or empty transaction history.
  const hasCash = Object.hasOwn(doc, 'cash');
  const hasTransactions = Object.hasOwn(doc, 'transactions');
  const cash = hasCash ? rows(doc.cash, 'cash').map(row => ({
    account_id: scoped(row), currency: currency(row.currency, 'KRW'),
    amount: amount(row.amount), as_of: row.as_of == null ? null : iso(row.as_of),
  })) : null;
  const transactions = hasTransactions ? rows(doc.transactions, 'transactions').map(row => {
    const side = String(row.side ?? '').toUpperCase();
    if (!['BUY', 'SELL'].includes(side)) throw new TypeError('invalid trade side');
    const transaction_id = String(row.transaction_id ?? '').trim();
    if (!transaction_id || transaction_id.length > 160) throw new TypeError('trade id required');
    return { account_id: scoped(row), ...instrument(row),
      currency: currency(row.currency, 'KRW'), transaction_id, side,
      trade_date: day(row.trade_date), quantity: amount(row.quantity, { positive: true }),
      price: amount(row.price, { nonnegative: true }), gross_amount: amount(row.gross_amount),
      fee: amount(row.fee), tax: amount(row.tax),
      executed_at: row.executed_at == null ? null : iso(row.executed_at) };
  }) : null;
  const observed = iso(doc.as_of);
  const rowTimes = [...positions, ...(cash ?? [])].map(row => row.as_of).filter(Boolean);
  const dataAsOf = [observed, ...rowTimes].sort()[0];
  return { kind: 'ACCOUNT_SNAPSHOT', dataAsOf,
    document: { as_of: observed, accounts, positions,
      ...(hasCash ? { cash } : {}), ...(hasTransactions ? { transactions } : {}) } };
}

export function normalizeMeritzRecurringPlans(input) {
  const doc = object(input);
  sensitive(doc);
  if (Object.keys(doc).some(key => !['as_of', 'plans'].includes(key))) {
    throw new TypeError('unexpected recurring document field');
  }
  const allowed = new Set(['market', 'ticker', 'asset_type', 'instrument_currency',
    'daily_amount', 'amount_currency', 'broker', 'started_at', 'memo', 'enabled']);
  const plans = rows(doc.plans, 'plans').map(row => {
    if (Object.keys(row).some(key => !allowed.has(key))) {
      throw new TypeError('unexpected recurring plan field');
    }
    const broker = String(row.broker ?? 'meritz').trim().toLowerCase();
    if (broker !== 'meritz') throw new TypeError('unexpected recurring source');
    if (row.enabled !== undefined && typeof row.enabled !== 'boolean') {
      throw new TypeError('boolean enabled state required');
    }
    return { ...instrument(row), instrument_currency: currency(row.instrument_currency, 'USD'),
      daily_amount: amount(row.daily_amount, { positive: true }),
      amount_currency: currency(row.amount_currency, 'KRW'), broker,
      started_at: row.started_at == null ? null : day(row.started_at),
      memo: String(row.memo ?? ''), enabled: row.enabled ?? true };
  });
  const keys = plans.map(row => `${row.broker}:${row.market}:${row.ticker}:${row.asset_type}:` +
    row.instrument_currency);
  if (new Set(keys).size !== keys.length) throw new TypeError('duplicate recurring instrument');
  const observed = iso(doc.as_of);
  return { kind: 'RECURRING_PLANS', dataAsOf: observed,
    document: { as_of: observed, plans } };
}

export async function storeMeritzRevision(db, normalized, { expectedRevision,
  mutationKey, storedAt }) {
  if (!['ACCOUNT_SNAPSHOT', 'RECURRING_PLANS'].includes(normalized?.kind) ||
      !Number.isSafeInteger(expectedRevision) || expectedRevision < 0 ||
      !/^[A-Za-z0-9_:-]{1,100}$/.test(mutationKey)) {
    throw new TypeError('invalid manual input revision');
  }
  const timestamp = iso(storedAt);
  const encoded = JSON.stringify(normalized.document);
  if (!encoded || encoded.length > 262144) throw new TypeError('manual document too large');
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(encoded));
  const hash = [...new Uint8Array(digest)].map(x => x.toString(16).padStart(2, '0')).join('');
  const current = await db.prepare(`SELECT revision, content_hash FROM cloud_manual_input_revisions
    WHERE source = 'meritz' AND kind = ? AND mutation_key = ?`).bind(
    normalized.kind, mutationKey).first();
  if (current) {
    if (current.content_hash !== hash) throw new TypeError('mutation replay content mismatch');
    return { revision: current.revision, replay: true };
  }
  const result = await db.prepare(`INSERT INTO cloud_manual_input_revisions
    (source, kind, revision, mutation_key, content_hash, data_as_of, stored_at, document_json)
    SELECT 'meritz', ?, ?, ?, ?, ?, ?, ?
    WHERE (SELECT COALESCE(MAX(revision), 0) FROM cloud_manual_input_revisions
      WHERE source = 'meritz' AND kind = ?) = ?
    ON CONFLICT DO NOTHING`).bind(normalized.kind, expectedRevision + 1, mutationKey,
    hash, normalized.dataAsOf, timestamp, encoded, normalized.kind,
    expectedRevision).run();
  if (result.meta.changes !== 1) throw new TypeError('stale manual input revision');
  return { revision: expectedRevision + 1, replay: false };
}

export async function latestMeritzRevision(db, kind) {
  if (!['ACCOUNT_SNAPSHOT', 'RECURRING_PLANS'].includes(kind)) throw new TypeError('invalid kind');
  const row = await db.prepare(`SELECT revision, data_as_of, stored_at, document_json
    FROM cloud_manual_input_revisions WHERE source = 'meritz' AND kind = ?
    ORDER BY revision DESC LIMIT 1`).bind(kind).first();
  return row ? { revision: row.revision, dataAsOf: row.data_as_of,
    storedAt: row.stored_at, document: JSON.parse(row.document_json) } : null;
}

export function manualSnapshotFreshness(revision, now, maximumAgeDays = 3) {
  if (!revision || revision.dataAsOf == null) return 'NOT_CONFIGURED';
  const observed = Date.parse(iso(revision.dataAsOf));
  const current = Date.parse(iso(now));
  if (!Number.isInteger(maximumAgeDays) || maximumAgeDays < 1 || maximumAgeDays > 30) {
    throw new TypeError('invalid maximum age');
  }
  return current - observed > maximumAgeDays * 86400000 || observed - current > 300000 ?
    'STALE' : 'OK';
}

// Deliberately unmounted. The caller must supply an independently verified
// owner admission callback before the document is parsed or D1 is touched.
export function createManualUploadHandler({ authorizeOwner, db, now }) {
  if (typeof authorizeOwner !== 'function' || !db || typeof now !== 'function') {
    throw new TypeError('owner admission, D1 and clock required');
  }
  return async request => {
    if (request.method !== 'POST' || new URL(request.url).pathname !== '/owner/manual-input') {
      return new Response('not found', { status: 404 });
    }
    try {
      if (await authorizeOwner(request) !== true) return new Response('forbidden', { status: 403 });
    } catch { return new Response('forbidden', { status: 403 }); }
    if (request.headers.get('content-type')?.split(';')[0] !== 'application/json' ||
        Number(request.headers.get('content-length')) > 262144) {
      return new Response('invalid request', { status: 413 });
    }
    if (!request.body) return new Response('invalid request', { status: 400 });
    const reader = request.body.getReader();
    const chunks = [];
    let size = 0;
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > 262144) { await reader.cancel(); return new Response('invalid request', { status: 413 }); }
        chunks.push(value);
      }
    } catch { return new Response('invalid request', { status: 400 }); }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    let raw;
    try { raw = new TextDecoder('utf-8', { fatal: true }).decode(bytes); }
    catch { return new Response('invalid request', { status: 400 }); }
    try {
      const upload = object(JSON.parse(raw));
      const normalized = upload.kind === 'ACCOUNT_SNAPSHOT' ?
        normalizeMeritzAccountSnapshot(upload.document) :
        upload.kind === 'RECURRING_PLANS' ?
          normalizeMeritzRecurringPlans(upload.document) : null;
      if (!normalized) throw new TypeError('invalid kind');
      const storedAt = now();
      const saved = await storeMeritzRevision(db, normalized, {
        expectedRevision: upload.expectedRevision, mutationKey: upload.mutationKey,
        storedAt,
      });
      return Response.json({ revision: saved.revision, replay: saved.replay,
        dataAsOf: normalized.dataAsOf, storedAt });
    } catch { return new Response('invalid or stale document', { status: 409 }); }
  };
}
