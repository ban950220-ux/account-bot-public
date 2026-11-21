// D1 optimistic writes for owner-only command handlers. No broker/order transport.
import { compare, decimal, exact } from './finance.mjs';
import { mutationDeliveryStatements } from './outbox.mjs';

const FINGERPRINT = /^[a-z][a-z0-9_]{0,30}_[0-9a-f]{64}$/;
const SYMBOL = /^[A-Z0-9][A-Z0-9._:-]{0,159}$/;
const MUTATION = /^[A-Za-z0-9._:-]{1,128}$/;
const encode = new TextEncoder();

async function hash(value) {
  const bytes = new Uint8Array(await crypto.subtle.digest('SHA-256', encode.encode(value)));
  return [...bytes].map(byte => byte.toString(16).padStart(2, '0')).join('');
}

function resultChanges(value) {
  if (!Number.isSafeInteger(value?.meta?.changes)) throw new TypeError('invalid D1 result');
  return value.meta.changes;
}

function required(value, name) {
  if (typeof value !== 'string' || !value.trim()) throw new TypeError(`${name} required`);
  return value;
}

function normalizeState(state) {
  if (!state || !state.instrument || typeof state.enabled !== 'boolean' ||
      !SYMBOL.test(state.instrument.market) || !SYMBOL.test(state.instrument.ticker) ||
      !SYMBOL.test(state.instrument.assetType) ||
      !/^[A-Z]{3}$/.test(state.instrument.currency) ||
      !/^[A-Z]{3}$/.test(state.amountCurrency) ||
      (state.accountFingerprint !== null && !FINGERPRINT.test(state.accountFingerprint)) ||
      (state.memo !== null && (typeof state.memo !== 'string' || state.memo.length > 2000))) {
    throw new TypeError('invalid plan state');
  }
  required(state.source, 'source');
  required(state.timezoneName, 'timezoneName');
  required(state.startedAt, 'startedAt');
  if (compare(decimal(state.dailyAmount), decimal('0.00000001')) < 0 ||
      (state.targetWeight !== null && (compare(decimal(state.targetWeight), decimal('0')) < 0 ||
        compare(decimal(state.targetWeight), decimal('1')) > 0))) {
    throw new TypeError('invalid plan amount or weight');
  }
  return { ...state, dailyAmount: exact(decimal(state.dailyAmount)),
    targetWeight: state.targetWeight === null ? null : exact(decimal(state.targetWeight)) };
}

function fromRow(row) {
  return { instrument: { market: row.market, ticker: row.ticker,
    assetType: row.asset_type, currency: row.instrument_currency },
  accountFingerprint: row.account_fingerprint, source: row.source,
  amountCurrency: row.amount_currency, dailyAmount: row.daily_amount,
  targetWeight: row.target_weight, enabled: row.enabled === 1,
  timezoneName: row.timezone_name, startedAt: row.started_at, memo: row.memo };
}

function statePayload(state) {
  return JSON.stringify([state.instrument.market, state.instrument.ticker,
    state.instrument.assetType, state.instrument.currency, state.accountFingerprint,
    state.source, state.amountCurrency, state.dailyAmount, state.targetWeight,
    state.enabled, state.timezoneName, state.startedAt, state.memo]);
}

const LATEST_ARCHIVE = `(SELECT e.action FROM cloud_plan_archive_events AS e
  WHERE e.plan_key = p.plan_key ORDER BY e.event_no DESC LIMIT 1)`;
const NO_ARCHIVE_TABLE = /no such table: cloud_plan_archive_events/i;

// Archived plans are hidden unless `includeArchived`; each row then carries
// `archived`. Before the additive archive table exists nothing is archived.
export async function listCurrentPlans(db, enabledOnly = false,
  { withThesis = false, includeArchived = false } = {}) {
  // The exact listing key and current revision are the same selectors as currentThesis.
  const query = archive => {
    const where = [...(enabledOnly ? ['p.enabled = 1'] : []),
      ...(archive && !includeArchived ? [`${LATEST_ARCHIVE} IS NOT 'ARCHIVE'`] : [])];
    return `SELECT p.*${withThesis ? ', r.thesis_text' : ''}${archive ?
      `, ${LATEST_ARCHIVE} AS archive_action` : ''} FROM cloud_plans AS p
    ${withThesis ? `LEFT JOIN cloud_theses AS t ON t.instrument_key = p.market || ':' || p.ticker
      LEFT JOIN cloud_thesis_revisions AS r ON r.instrument_key = t.instrument_key
        AND r.revision_no = t.current_revision_no` : ''}
    ${where.length ? `WHERE ${where.join(' AND ')}` : ''} ORDER BY p.plan_key`;
  };
  let results;
  try {
    ({ results } = await db.prepare(query(true)).all());
  } catch (error) {
    if (!NO_ARCHIVE_TABLE.test(String(error?.message))) throw error;
    ({ results } = await db.prepare(query(false)).all());
  }
  return results.map(row => ({ planKey: row.plan_key, version: row.version,
    ...fromRow(row), ...(withThesis ? { investmentThesis: row.thesis_text || '' } : {}),
    ...(includeArchived ? { archived: row.archive_action === 'ARCHIVE' } : {}) }));
}

// Revisions for history and point-in-time views, oldest first, bounded.
export async function listPlanRevisions(db, limit = 5000) {
  const { results } = await db.prepare(`SELECT plan_key, revision_no, market, ticker,
    enabled, daily_amount, amount_currency, effective_from FROM cloud_plan_revisions
    ORDER BY effective_from, plan_key, revision_no LIMIT ?`).bind(limit).all();
  return results.map(row => ({ planKey: row.plan_key, revisionNo: row.revision_no,
    market: row.market, ticker: row.ticker, enabled: row.enabled === 1,
    dailyAmount: row.daily_amount, currency: row.amount_currency,
    effectiveFrom: row.effective_from }));
}

export async function planArchiveInstalled(db) {
  try {
    await db.prepare('SELECT 1 FROM cloud_plan_archive_events LIMIT 1').first();
    return true;
  } catch (error) {
    if (NO_ARCHIVE_TABLE.test(String(error?.message))) return false;
    throw error;
  }
}

// The plan and action an archive mutation already recorded, or null.
export async function archiveEvent(db, mutationKey) {
  if (!MUTATION.test(mutationKey)) throw new TypeError('invalid archive mutation key');
  try {
    return await db.prepare(`SELECT plan_key, action FROM cloud_plan_archive_events
      WHERE mutation_key = ?`).bind(mutationKey).first() ?? null;
  } catch (error) {
    if (NO_ARCHIVE_TABLE.test(String(error?.message))) return null;
    throw error;
  }
}

// ARCHIVE turns the plan OFF (one revision) and records the event in the same
// batch; RESTORE only records the event, so a restored plan comes back OFF.
// The event insert is conditioned on the final plan version, so a lost race
// writes neither. Returns CHANGED, UNCHANGED, REPLAY, MISSING or CONFLICT.
export async function setPlanArchived(db, { planKey, expectedVersion, archived, mutationKey,
  effectiveAt, delivery = null }) {
  if (!MUTATION.test(required(planKey, 'planKey')) || !MUTATION.test(mutationKey) ||
      !Number.isSafeInteger(expectedVersion) || expectedVersion < 1 ||
      typeof archived !== 'boolean') {
    throw new TypeError('invalid plan archive mutation');
  }
  required(effectiveAt, 'effectiveAt');
  if (await archiveEvent(db, mutationKey)) return 'REPLAY';
  const current = await db.prepare(`SELECT p.*, ${LATEST_ARCHIVE} AS archive_action
    FROM cloud_plans AS p WHERE p.plan_key = ?`).bind(planKey).first();
  if (!current) return 'MISSING';
  if (current.version !== expectedVersion) return 'CONFLICT';
  if ((current.archive_action === 'ARCHIVE') === archived) return 'UNCHANGED';
  const turnOff = archived && current.enabled === 1;
  const finalVersion = expectedVersion + (turnOff ? 1 : 0);
  const statements = [];
  const deliveryStatements = mutationDeliveryStatements(db, delivery,
    { kind: 'archive', values: [mutationKey] });
  if (delivery) statements.push(deliveryStatements[0]);
  if (turnOff) {
    const state = normalizeState({ ...fromRow(current), enabled: false });
    const changeHash = await hash(statePayload(state));
    statements.push(db.prepare(`UPDATE cloud_plans SET enabled = 0,
      version = version + 1, last_mutation_key = ?
      WHERE plan_key = ? AND version = ? AND NOT EXISTS (
        SELECT 1 FROM cloud_plan_revisions WHERE plan_key = ? AND mutation_key = ?
      )`).bind(mutationKey, planKey, expectedVersion, planKey, mutationKey));
    statements.push(db.prepare(`INSERT INTO cloud_plan_revisions
      (plan_key, revision_no, change_hash, source, account_fingerprint,
       market, ticker, asset_type, instrument_currency, enabled, daily_amount,
       amount_currency, target_weight, timezone_name, started_at, effective_from,
       memo, mutation_key)
      SELECT plan_key, version, ?, source, account_fingerprint, market, ticker,
       asset_type, instrument_currency, enabled, daily_amount, amount_currency,
       target_weight, timezone_name, started_at, ?, memo, ? FROM cloud_plans
      WHERE plan_key = ? AND version = ? AND last_mutation_key = ?`)
      .bind(changeHash, effectiveAt, mutationKey, planKey, finalVersion, mutationKey));
  }
  statements.push(db.prepare(`INSERT INTO cloud_plan_archive_events
    (plan_key, action, effective_at, mutation_key)
    SELECT plan_key, ?, ?, ? FROM cloud_plans WHERE plan_key = ? AND version = ?`)
    .bind(archived ? 'ARCHIVE' : 'RESTORE', effectiveAt, mutationKey, planKey, finalVersion));
  if (delivery) statements.push(deliveryStatements[1]);
  const results = await db.batch(statements);
  const offset = delivery ? 1 : 0;
  const written = results.slice(offset, offset + (turnOff ? 3 : 1)).map(resultChanges);
  if (written.every(count => count === 0)) return 'CONFLICT';
  if (written.some(count => count !== 1)) throw new Error('plan archive transaction mismatch');
  return 'CHANGED';
}

export async function createPlanOnce(db, planKey, mutationKey, initialState, effectiveAt,
  delivery = null) {
  if (!MUTATION.test(required(planKey, 'planKey')) || !MUTATION.test(mutationKey)) {
    throw new TypeError('invalid plan creation key');
  }
  required(effectiveAt, 'effectiveAt');
  const state = normalizeState(initialState);
  const changeHash = await hash(statePayload(state));
  const deliveryStatements = mutationDeliveryStatements(db, delivery,
    { kind: 'plan', values: [planKey, mutationKey] });
  const results = await db.batch([
    ...(delivery ? [deliveryStatements[0]] : []),
    db.prepare(`INSERT INTO cloud_plans
      (plan_key, account_fingerprint, source, market, ticker, asset_type,
       instrument_currency, amount_currency, daily_amount, target_weight,
       enabled, timezone_name, started_at, memo, version, last_mutation_key)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
      ON CONFLICT(plan_key) DO NOTHING
      RETURNING plan_key, version, last_mutation_key`).bind(planKey, state.accountFingerprint,
      state.source, state.instrument.market, state.instrument.ticker,
      state.instrument.assetType, state.instrument.currency, state.amountCurrency,
      state.dailyAmount, state.targetWeight, state.enabled ? 1 : 0,
      state.timezoneName, state.startedAt, state.memo, mutationKey),
    db.prepare(`INSERT INTO cloud_plan_revisions
      (plan_key, revision_no, change_hash, source, account_fingerprint,
       market, ticker, asset_type, instrument_currency, enabled, daily_amount,
       amount_currency, target_weight, timezone_name, started_at, effective_from,
       memo, mutation_key)
      SELECT plan_key, 1, ?, source, account_fingerprint, market, ticker,
       asset_type, instrument_currency, enabled, daily_amount, amount_currency,
       target_weight, timezone_name, started_at, ?, memo, ? FROM cloud_plans
      WHERE plan_key = ? AND version = 1 AND last_mutation_key = ?
        AND NOT EXISTS (SELECT 1 FROM cloud_plan_revisions WHERE plan_key = ?)`)
      .bind(changeHash, effectiveAt, mutationKey, planKey, mutationKey, planKey),
    ...(delivery ? [deliveryStatements[1]] : []),
  ]);
  const offset = delivery ? 1 : 0;
  // D1 meta.changes includes AFTER-trigger rows (e.g. immutable portfolio scope).
  // RETURNING proves this top-level INSERT's identity, not unrelated trigger work.
  const inserted = results[offset]?.results;
  if (!Array.isArray(inserted) || inserted.length > 1 ||
      (inserted.length === 1 && (inserted[0].plan_key !== planKey ||
        inserted[0].version !== 1 || inserted[0].last_mutation_key !== mutationKey))) {
    throw new Error('plan creation receipt mismatch');
  }
  const created = inserted.length;
  const revision = resultChanges(results[offset + 1]);
  if (created !== revision || ![0, 1].includes(created)) {
    throw new Error('plan creation transaction mismatch');
  }
  if (delivery && (resultChanges(results[0]) !== created ||
      resultChanges(results.at(-1)) !== created)) {
    throw new Error('plan delivery transaction mismatch');
  }
  if (created) return 'CREATED';
  const existing = await db.prepare(`SELECT * FROM cloud_plans WHERE plan_key = ?`)
    .bind(planKey).first();
  return existing?.last_mutation_key === mutationKey &&
    statePayload(normalizeState(fromRow(existing))) === statePayload(state) ?
    'REPLAY' : 'CONFLICT';
}

export async function applyPlanChange(db, planKey, expectedVersion, mutationKey,
  nextState, effectiveAt, delivery = null) {
  if (!MUTATION.test(required(planKey, 'planKey')) || !MUTATION.test(mutationKey) ||
      !Number.isSafeInteger(expectedVersion) || expectedVersion < 1) {
    throw new TypeError('invalid plan mutation');
  }
  required(effectiveAt, 'effectiveAt');
  const current = await db.prepare(`SELECT * FROM cloud_plans WHERE plan_key = ?`)
    .bind(planKey).first();
  if (!current) return 'MISSING';
  if (current.version !== expectedVersion) return 'CONFLICT';
  const state = normalizeState(nextState);
  if (state.accountFingerprint !== current.account_fingerprint ||
      state.source !== current.source || state.instrument.market !== current.market ||
      state.instrument.ticker !== current.ticker ||
      state.instrument.assetType !== current.asset_type ||
      state.instrument.currency !== current.instrument_currency) {
    throw new TypeError('plan edit cannot change account, source or instrument');
  }
  if (statePayload(state) === statePayload(normalizeState(fromRow(current)))) return 'UNCHANGED';
  const changeHash = await hash(statePayload(state));
  const deliveryStatements = mutationDeliveryStatements(db, delivery,
    { kind: 'plan', values: [planKey, mutationKey] });
  const results = await db.batch([
    ...(delivery ? [deliveryStatements[0]] : []),
    db.prepare(`UPDATE cloud_plans SET account_fingerprint = ?, source = ?, market = ?,
      ticker = ?, asset_type = ?, instrument_currency = ?, amount_currency = ?,
      daily_amount = ?, target_weight = ?, enabled = ?, timezone_name = ?,
      started_at = ?, memo = ?, version = version + 1, last_mutation_key = ?
      WHERE plan_key = ? AND version = ? AND NOT EXISTS (
        SELECT 1 FROM cloud_plan_revisions WHERE plan_key = ? AND mutation_key = ?
      )`).bind(state.accountFingerprint, state.source,
      state.instrument.market, state.instrument.ticker, state.instrument.assetType,
      state.instrument.currency, state.amountCurrency, state.dailyAmount,
      state.targetWeight, state.enabled ? 1 : 0, state.timezoneName,
      state.startedAt, state.memo, mutationKey, planKey, expectedVersion,
      planKey, mutationKey),
    db.prepare(`INSERT INTO cloud_plan_revisions
      (plan_key, revision_no, change_hash, source, account_fingerprint,
       market, ticker, asset_type, instrument_currency, enabled, daily_amount,
       amount_currency, target_weight, timezone_name, started_at, effective_from,
       memo, mutation_key)
      SELECT plan_key, version, ?, source, account_fingerprint, market, ticker,
       asset_type, instrument_currency, enabled, daily_amount, amount_currency,
       target_weight, timezone_name, started_at, ?, memo, ? FROM cloud_plans
      WHERE plan_key = ? AND version = ? AND last_mutation_key = ?
        AND NOT EXISTS (SELECT 1 FROM cloud_plan_revisions
          WHERE plan_key = ? AND mutation_key = ?)`)
      .bind(changeHash, effectiveAt, mutationKey,
      planKey, expectedVersion + 1, mutationKey, planKey, mutationKey),
    ...(delivery ? [deliveryStatements[1]] : []),
  ]);
  const offset = delivery ? 1 : 0;
  const first = resultChanges(results[offset]);
  const second = resultChanges(results[offset + 1]);
  if (first !== second || ![0, 1].includes(first)) throw new Error('plan revision transaction mismatch');
  if (delivery && (resultChanges(results[0]) !== first ||
      resultChanges(results.at(-1)) !== first)) {
    throw new Error('plan delivery transaction mismatch');
  }
  return first ? 'CHANGED' : 'CONFLICT';
}

export async function applyGroupChanges(db, changes, mutationKey, effectiveAt,
  delivery = null) {
  if (!MUTATION.test(mutationKey) || !Array.isArray(changes) ||
      !changes.length || changes.length > 100 ||
      new Set(changes.map(item => item.planKey)).size !== changes.length) {
    throw new TypeError('invalid bounded group mutation');
  }
  required(effectiveAt, 'effectiveAt');
  const prior = await groupMutationPlans(db, mutationKey);
  if (prior.length) return 'REPLAY';
  const work = [];
  for (const item of changes) {
    if (!MUTATION.test(item.planKey) || !Number.isSafeInteger(item.expectedVersion) ||
        item.expectedVersion < 1) throw new TypeError('invalid group member');
    const current = await db.prepare(`SELECT * FROM cloud_plans WHERE plan_key = ?`)
      .bind(item.planKey).first();
    if (!current || current.version !== item.expectedVersion) return 'CONFLICT';
    const next = normalizeState(item.nextState);
    if (next.accountFingerprint !== current.account_fingerprint ||
        next.source !== current.source || next.instrument.market !== current.market ||
        next.instrument.ticker !== current.ticker ||
        next.instrument.assetType !== current.asset_type ||
        next.instrument.currency !== current.instrument_currency) {
      throw new TypeError('group edit crossed account, source or instrument');
    }
    if (statePayload(next) !== statePayload(normalizeState(fromRow(current)))) {
      work.push({ ...item, next, changeHash: await hash(statePayload(next)),
        memberKey: await hash(`${mutationKey}:${item.planKey}`) });
    }
  }
  if (!work.length) return 'UNCHANGED';
  const statements = [];
  const deliveryStatements = mutationDeliveryStatements(db, delivery,
    { kind: 'group', values: [mutationKey] });
  if (delivery) statements.push(deliveryStatements[0]);
  for (const item of work) {
    // A failed version assertion violates CHECK and rolls back every member.
    statements.push(db.prepare(`INSERT INTO cloud_mutation_checks
      (mutation_key, plan_key, valid) VALUES (?, ?,
       (SELECT CASE WHEN version = ? THEN 1 ELSE 0 END
        FROM cloud_plans WHERE plan_key = ?))`)
      .bind(mutationKey, item.planKey, item.expectedVersion, item.planKey));
    const state = item.next;
    statements.push(db.prepare(`UPDATE cloud_plans SET daily_amount = ?, amount_currency = ?,
      target_weight = ?, enabled = ?, timezone_name = ?, started_at = ?, memo = ?,
      version = version + 1, last_mutation_key = ?
      WHERE plan_key = ? AND version = ?`).bind(state.dailyAmount, state.amountCurrency,
      state.targetWeight, state.enabled ? 1 : 0, state.timezoneName, state.startedAt,
      state.memo, item.memberKey, item.planKey, item.expectedVersion));
    statements.push(db.prepare(`INSERT INTO cloud_plan_revisions
      (plan_key, revision_no, change_hash, source, account_fingerprint,
       market, ticker, asset_type, instrument_currency, enabled, daily_amount,
       amount_currency, target_weight, timezone_name, started_at, effective_from,
       memo, mutation_key)
      SELECT plan_key, version, ?, source, account_fingerprint, market, ticker,
       asset_type, instrument_currency, enabled, daily_amount, amount_currency,
       target_weight, timezone_name, started_at, ?, memo, ? FROM cloud_plans
      WHERE plan_key = ? AND version = ? AND last_mutation_key = ?`)
      .bind(item.changeHash, effectiveAt, item.memberKey, item.planKey,
        item.expectedVersion + 1, item.memberKey));
  }
  if (delivery) statements.push(deliveryStatements[1]);
  const results = await db.batch(statements);
  if (results.length !== statements.length || results.some(result => resultChanges(result) !== 1)) {
    throw new Error('group mutation transaction mismatch');
  }
  return 'CHANGED';
}

export async function groupMutationPlans(db, mutationKey) {
  if (!MUTATION.test(mutationKey)) throw new TypeError('invalid group mutation key');
  const { results } = await db.prepare(`SELECT plan_key FROM cloud_mutation_checks
    WHERE mutation_key = ? ORDER BY plan_key`).bind(mutationKey).all();
  return results.map(row => row.plan_key);
}

export async function appendThesis(db, { market, ticker, currency, text,
  revisionKey, mutationKey, expectedRevision, effectiveAt, delivery = null }) {
  if (![market, ticker].every(value => SYMBOL.test(value)) ||
      !/^[A-Z]{3}$/.test(currency) ||
      !MUTATION.test(revisionKey) || !MUTATION.test(mutationKey) ||
      !Number.isSafeInteger(expectedRevision) || expectedRevision < 0 ||
      typeof text !== 'string' || !text.trim() || text.length > 20000) {
    throw new TypeError('invalid thesis revision');
  }
  required(effectiveAt, 'effectiveAt');
  const identity = `${market}:${ticker}`;
  const contentHash = await hash(text.trim());
  const statements = [];
  const deliveryStatements = mutationDeliveryStatements(db, delivery,
    { kind: 'thesis', values: [identity, mutationKey] });
  if (delivery) statements.push(deliveryStatements[0]);
  if (expectedRevision === 0) statements.push(db.prepare(`INSERT INTO cloud_theses
    (instrument_key, market, ticker, currency, current_revision_no)
    VALUES (?, ?, ?, ?, 0) ON CONFLICT(instrument_key) DO NOTHING`)
    .bind(identity, market, ticker, currency));
  statements.push(db.prepare(`UPDATE cloud_theses SET
    current_revision_no = current_revision_no + 1, last_mutation_key = ?
    WHERE instrument_key = ? AND current_revision_no = ?
      AND NOT EXISTS (SELECT 1 FROM cloud_thesis_revisions
        WHERE instrument_key = ? AND (revision_key = ? OR mutation_key = ?))`)
    .bind(mutationKey, identity, expectedRevision, identity, revisionKey, mutationKey));
  statements.push(db.prepare(`INSERT INTO cloud_thesis_revisions
    (instrument_key, revision_no, revision_key, content_hash, thesis_text,
     effective_at, mutation_key)
    SELECT instrument_key, current_revision_no, ?, ?, ?, ?, ?
    FROM cloud_theses WHERE instrument_key = ? AND current_revision_no = ?
      AND last_mutation_key = ? AND NOT EXISTS (
        SELECT 1 FROM cloud_thesis_revisions
        WHERE instrument_key = ? AND mutation_key = ?)`)
    .bind(revisionKey, contentHash, text.trim(), effectiveAt, mutationKey,
      identity, expectedRevision + 1, mutationKey, identity, mutationKey));
  if (delivery) statements.push(deliveryStatements[1]);
  const results = await db.batch(statements);
  const updated = resultChanges(results.at(delivery ? -3 : -2));
  const inserted = resultChanges(results.at(delivery ? -2 : -1));
  if (updated !== inserted || ![0, 1].includes(updated)) {
    throw new Error('thesis revision transaction mismatch');
  }
  if (delivery && (resultChanges(results[0]) !== updated ||
      resultChanges(results.at(-1)) !== updated)) {
    throw new Error('thesis delivery transaction mismatch');
  }
  return updated ? 'CHANGED' : 'CONFLICT';
}

export async function currentThesis(db, market, ticker) {
  if (!SYMBOL.test(market) || !SYMBOL.test(ticker)) throw new TypeError('invalid thesis identity');
  return db.prepare(`SELECT r.thesis_text, r.revision_no, r.revision_key
    FROM cloud_theses AS t JOIN cloud_thesis_revisions AS r
      ON r.instrument_key = t.instrument_key AND r.revision_no = t.current_revision_no
    WHERE t.instrument_key = ?`).bind(`${market}:${ticker}`).first();
}
