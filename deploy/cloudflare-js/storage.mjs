// D1 storage boundary for synthetic rehearsal and future private transfer.
// Amounts are exact decimal strings; account references must be fingerprints.
import { add, compare, decimal } from './finance.mjs';

const FINGERPRINT = /^[a-z][a-z0-9_]{0,30}_[0-9a-f]{64}$/;
const IDENTIFIER = /^[A-Z0-9][A-Z0-9._:-]{0,159}$/;
const HASH = /^[0-9a-f]{64}$/;
const CURRENCY = /^[A-Z]{3}$/;
const SAFE_ERROR = /^[A-Za-z][A-Za-z0-9_.]{0,95}$/;
const SNAPSHOT_STATUS = new Set(['COMPLETE', 'PARTIAL', 'FAILED']);
const SOURCE_STATUS = new Set(['PENDING', 'SUCCESS', 'PARTIAL', 'FAILED', 'STALE', 'UNSUPPORTED']);
const MASKED_LABEL = /^\*{3,}(?:[A-Za-z_-]{1,16}|[0-9]{1,4})$/;

function required(value, field) {
  if (typeof value !== 'string' || !value.trim()) throw new TypeError(`${field} required`);
  return value;
}

function fingerprint(value) {
  if (!FINGERPRINT.test(value)) throw new TypeError('opaque account fingerprint required');
  return value;
}

function amount(value, field, minimum = null) {
  if (value === null || value === undefined) return null;
  const parsed = decimal(value);
  if (minimum !== null && compare(parsed, decimal(minimum)) < 0) {
    throw new TypeError(`${field} below minimum`);
  }
  return value;
}

function currency(value) {
  if (typeof value !== 'string' || !CURRENCY.test(value)) {
    throw new TypeError('invalid currency code');
  }
  return value;
}

function instrument(value) {
  for (const field of ['market', 'ticker', 'assetType']) {
    if (!IDENTIFIER.test(required(value[field], field))) throw new TypeError('invalid instrument');
  }
  currency(value.currency);
  return value;
}

function sameInstrument(left, right) {
  return ['market', 'ticker', 'assetType', 'currency'].every(field =>
    left[field] === right[field]);
}

export async function importAccount(db, row) {
  fingerprint(row.fingerprint);
  required(row.broker, 'broker');
  currency(row.baseCurrency);
  if (row.maskedLabel !== null && row.maskedLabel !== undefined &&
      (typeof row.maskedLabel !== 'string' || !MASKED_LABEL.test(row.maskedLabel))) {
    throw new TypeError('invalid masked label');
  }
  const result = await db.prepare(`INSERT INTO cloud_accounts
    (account_fingerprint, broker, masked_label, base_currency) VALUES (?, ?, ?, ?)`)
    .bind(row.fingerprint, row.broker, row.maskedLabel ?? null, row.baseCurrency).run();
  return result.meta.changes === 1;
}

export async function importPlanHistory(db, plan, revisions) {
  if (required(plan.planKey, 'planKey').length > 160 ||
      !Number.isSafeInteger(plan.version) || plan.version < 1 ||
      !Array.isArray(revisions) || revisions.length !== plan.version ||
      typeof plan.enabled !== 'boolean') throw new TypeError('invalid plan history');
  if (plan.accountFingerprint !== null) fingerprint(plan.accountFingerprint);
  instrument(plan.instrument);
  currency(plan.amountCurrency);
  amount(required(plan.dailyAmount, 'dailyAmount'), 'dailyAmount', '0.00000001');
  if (plan.targetWeight !== null) amount(plan.targetWeight, 'targetWeight', '0');
  if (plan.targetWeight !== null && compare(decimal(plan.targetWeight), decimal('1')) > 0) {
    throw new TypeError('target weight above one');
  }
  for (const [index, revision] of revisions.entries()) {
    if (revision.revisionNo !== index + 1 || !HASH.test(revision.changeHash) ||
        typeof revision.enabled !== 'boolean') {
      throw new TypeError('invalid revision sequence');
    }
    amount(required(revision.dailyAmount, 'revision amount'),
      'revision amount', '0.00000001');
    amount(revision.targetWeight, 'revision targetWeight', '0');
    if (revision.targetWeight !== null &&
        compare(decimal(revision.targetWeight), decimal('1')) > 0) {
      throw new TypeError('revision target weight above one');
    }
    required(revision.effectiveFrom, 'effectiveFrom');
    if (revision.accountFingerprint !== null) fingerprint(revision.accountFingerprint);
    instrument(revision.instrument);
    required(revision.timezoneName, 'revision timezoneName');
    required(revision.startedAt, 'revision startedAt');
  }
  const latest = revisions.at(-1);
  if (latest.dailyAmount !== plan.dailyAmount || latest.enabled !== plan.enabled ||
      latest.targetWeight !== plan.targetWeight ||
      latest.source !== plan.source || latest.accountFingerprint !== plan.accountFingerprint ||
      !sameInstrument(latest.instrument, plan.instrument) ||
      latest.timezoneName !== plan.timezoneName || latest.startedAt !== plan.startedAt) {
    throw new TypeError('current plan and final revision disagree');
  }
  const statements = [db.prepare(`INSERT INTO cloud_plans
    (plan_key, account_fingerprint, source, market, ticker, asset_type,
     instrument_currency, amount_currency, daily_amount, target_weight,
     enabled, timezone_name, started_at, memo, version)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`)
    .bind(plan.planKey, plan.accountFingerprint, required(plan.source, 'source'),
      plan.instrument.market, plan.instrument.ticker, plan.instrument.assetType,
      plan.instrument.currency, plan.amountCurrency, plan.dailyAmount, plan.targetWeight,
      plan.enabled ? 1 : 0, required(plan.timezoneName, 'timezoneName'),
      required(plan.startedAt, 'startedAt'), plan.memo, plan.version)];
  for (const revision of revisions) {
    statements.push(db.prepare(`INSERT INTO cloud_plan_revisions
      (plan_key, revision_no, change_hash, source, account_fingerprint,
       market, ticker, asset_type, instrument_currency, enabled, daily_amount,
       amount_currency, target_weight, timezone_name, started_at, effective_from, memo)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`)
      .bind(plan.planKey, revision.revisionNo, revision.changeHash,
        required(revision.source, 'revision source'), revision.accountFingerprint,
        revision.instrument.market, revision.instrument.ticker,
        revision.instrument.assetType, revision.instrument.currency,
        revision.enabled ? 1 : 0, revision.dailyAmount, plan.amountCurrency,
        revision.targetWeight, revision.timezoneName, revision.startedAt,
        revision.effectiveFrom, revision.memo));
  }
  await db.batch(statements); // D1 batch rolls back on a duplicate or any failed revision.
}

export async function recordTransactionOnce(db, row) {
  fingerprint(row.accountFingerprint);
  if (required(row.sourceKey, 'sourceKey').length > 160) {
    throw new TypeError('sourceKey too long');
  }
  required(row.source, 'source');
  currency(row.currency);
  if (row.instrument !== null) instrument(row.instrument);
  if (row.instrument !== null && row.instrument.currency !== row.currency) {
    throw new TypeError('instrument and transaction currency disagree');
  }
  amount(required(row.quantity, 'quantity'), 'quantity', '0');
  amount(row.unitPrice, 'unitPrice', '0');
  for (const field of ['grossAmount', 'fee', 'tax']) amount(row[field], field);
  amount(row.netAmount, 'netAmount');
  const result = await db.prepare(`INSERT INTO cloud_transactions
    (account_fingerprint, source_key, source, market, ticker, asset_type,
     currency, transaction_type, status, traded_at, settled_at, quantity,
     unit_price, gross_amount, fee, tax, net_amount)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(account_fingerprint, source_key) DO NOTHING`)
    .bind(row.accountFingerprint, row.sourceKey, row.source,
      row.instrument?.market ?? null, row.instrument?.ticker ?? null,
      row.instrument?.assetType ?? null, row.currency,
      required(row.transactionType, 'transactionType'), required(row.status, 'status'),
      required(row.tradedAt, 'tradedAt'), row.settledAt, row.quantity,
      row.unitPrice, row.grossAmount, row.fee, row.tax, row.netAmount).run();
  if (result.meta.changes === 1) return true;
  const existing = await db.prepare(`SELECT source, market, ticker, asset_type,
    currency, transaction_type, status, traded_at, settled_at, quantity,
    unit_price, gross_amount, fee, tax, net_amount FROM cloud_transactions
    WHERE account_fingerprint = ? AND source_key = ?`).bind(
    row.accountFingerprint, row.sourceKey).first();
  const expected = [row.source, row.instrument?.market ?? null,
    row.instrument?.ticker ?? null, row.instrument?.assetType ?? null,
    row.currency, row.transactionType, row.status, row.tradedAt,
    row.settledAt ?? null, row.quantity, row.unitPrice ?? null,
    row.grossAmount ?? null, row.fee ?? null, row.tax ?? null,
    row.netAmount ?? null];
  const actual = existing && [existing.source, existing.market, existing.ticker,
    existing.asset_type, existing.currency, existing.transaction_type,
    existing.status, existing.traded_at, existing.settled_at,
    existing.quantity, existing.unit_price, existing.gross_amount,
    existing.fee, existing.tax, existing.net_amount];
  if (!actual || expected.some((value, index) => value !== actual[index])) {
    throw new TypeError('transaction source key conflict');
  }
  return false;
}

export async function storeSnapshot(db, snapshot) {
  if (!SNAPSHOT_STATUS.has(snapshot.status) || !Array.isArray(snapshot.sources) ||
      !snapshot.sources.length || !Array.isArray(snapshot.positions) ||
      !Array.isArray(snapshot.cash) ||
      (snapshot.status === 'COMPLETE' && (snapshot.totalMarketValue === null ||
        snapshot.sources.some(source => source.status !== 'SUCCESS'))) ||
      (snapshot.status !== 'COMPLETE' &&
        (snapshot.totalMarketValue !== null || snapshot.totalCostBasis !== null))) {
    throw new TypeError('invalid snapshot state');
  }
  currency(snapshot.baseCurrency);
  amount(snapshot.totalMarketValue, 'totalMarketValue', '0');
  amount(snapshot.totalCostBasis, 'totalCostBasis', '0');
  if (snapshot.status === 'COMPLETE') {
    const values = [...snapshot.positions, ...snapshot.cash].map(row => row.valueInBase);
    if (values.some(value => value === null || value === undefined) ||
        compare(values.reduce((sum, value) => add(sum, decimal(value)), decimal('0')),
          decimal(snapshot.totalMarketValue)) !== 0) {
      throw new TypeError('complete snapshot valuation does not reconcile');
    }
  }
  const statements = [db.prepare(`INSERT INTO cloud_snapshots
    (idempotency_key, captured_at, data_as_of, status, base_currency,
     total_market_value, total_cost_basis, finalized_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)`)
    .bind(required(snapshot.key, 'snapshot key'), required(snapshot.capturedAt, 'capturedAt'),
      required(snapshot.dataAsOf, 'dataAsOf'), snapshot.status, snapshot.baseCurrency,
      snapshot.totalMarketValue, snapshot.totalCostBasis,
      required(snapshot.finalizedAt, 'finalizedAt'))];
  const seenSources = new Set();
  for (const source of snapshot.sources) {
    if (!SOURCE_STATUS.has(source.status) || seenSources.has(source.key)) {
      throw new TypeError('invalid or duplicate snapshot source');
    }
    seenSources.add(source.key);
    if (source.accountFingerprint !== null) fingerprint(source.accountFingerprint);
    if (source.safeErrorType !== null && !SAFE_ERROR.test(source.safeErrorType)) {
      throw new TypeError('unsafe source error type');
    }
    statements.push(db.prepare(`INSERT INTO cloud_snapshot_sources
      (snapshot_key, source_key, account_fingerprint, status, observed_at, safe_error_type)
      VALUES (?, ?, ?, ?, ?, ?)`)
      .bind(snapshot.key, required(source.key, 'source key'), source.accountFingerprint,
        source.status, source.observedAt, source.safeErrorType));
  }
  for (const position of snapshot.positions) {
    fingerprint(position.accountFingerprint);
    instrument(position.instrument);
    amount(required(position.quantity, 'quantity'), 'quantity', '0');
    for (const field of ['costBasis', 'marketValue', 'valueInBase']) {
      amount(position[field], field, '0');
    }
    statements.push(db.prepare(`INSERT INTO cloud_position_snapshots
      (snapshot_key, account_fingerprint, instrument_key, currency,
       quantity, cost_basis, market_value, value_in_base)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?)`)
      .bind(snapshot.key, position.accountFingerprint,
        `${position.instrument.market}:${position.instrument.ticker}:` +
        `${position.instrument.assetType}:${position.instrument.currency}`,
        position.instrument.currency, position.quantity, position.costBasis,
        position.marketValue, position.valueInBase));
  }
  for (const cash of snapshot.cash) {
    fingerprint(cash.accountFingerprint);
    currency(cash.currency);
    amount(required(cash.amount, 'amount'), 'amount');
    amount(cash.valueInBase, 'valueInBase');
    statements.push(db.prepare(`INSERT INTO cloud_cash_snapshots
      (snapshot_key, account_fingerprint, currency, amount, value_in_base)
      VALUES (?, ?, ?, ?, ?)`)
      .bind(snapshot.key, cash.accountFingerprint, cash.currency, cash.amount, cash.valueInBase));
  }
  await db.batch(statements);
}
