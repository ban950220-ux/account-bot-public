// Bounded, read-only compatibility view of the inert legacy archive.
// It never writes to an active outbox, ledger, claim, or recurring plan table.
const TABLES = new Set(['broker_accounts', 'transactions', 'reports',
  'report_deliveries', 'recurring_buy_occurrences', 'portfolio_snapshots', 'instruments']);
const KINDS = new Map([['accounts', 'broker_accounts'], ['ledger', 'transactions'],
  ['reports', 'reports'], ['occurrences', 'recurring_buy_occurrences'],
  ['snapshots', 'portfolio_snapshots']]);
const FINGERPRINT = /^[a-z][a-z0-9_]{0,30}_[0-9a-f]{64}$/;
const REQUIRED = new Map([
  ['broker_accounts', ['id', 'broker', 'account_fingerprint', 'masked_label', 'base_currency']],
  ['instruments', ['id', 'market', 'ticker']],
  ['transactions', ['id', 'account_id', 'source_key', 'instrument_id', 'currency',
    'quantity', 'status', 'transaction_type', 'traded_at', 'gross_amount']],
  ['reports', ['id', 'report_type', 'period_key', 'status', 'completed_at']],
  ['report_deliveries', ['report_id', 'status', 'sent_at']],
  ['recurring_buy_occurrences', ['plan_id', 'local_due_date', 'planned_amount',
    'currency', 'actual_amount', 'status']],
  ['portfolio_snapshots', ['data_as_of', 'status', 'total_market_value', 'base_currency']],
]);

async function digest(value) {
  const bytes = new Uint8Array(await crypto.subtle.digest('SHA-256',
    new TextEncoder().encode(value)));
  return [...bytes].map(byte => byte.toString(16).padStart(2, '0')).join('');
}

function decode(encoded, columns) {
  const values = JSON.parse(encoded);
  if (!Array.isArray(values) || values.length !== columns.length) {
    throw new TypeError('legacy row schema mismatch');
  }
  return Object.fromEntries(columns.map((column, index) => {
    const [kind, value] = values[index];
    if (kind === 'null' && value === null) return [column, null];
    if (kind === 'text' && typeof value === 'string') return [column, value];
    if (kind === 'integer' && /^-?\d+$/.test(value)) return [column, value];
    if (kind === 'real' && typeof value === 'string' && Number.isFinite(Number(value))) {
      return [column, value];
    }
    if (kind === 'blob' && typeof value === 'string') return [column, '[BLOB]'];
    throw new TypeError('invalid legacy storage class');
  }));
}

async function activeRun(db) {
  const { results } = await db.prepare(`SELECT run_id, row_count FROM cloud_legacy_archive_runs
    WHERE status = 'VERIFIED' LIMIT 2`).all();
  if (!results.length) return null;
  if (results.length !== 1) throw new TypeError('ambiguous legacy archive');
  const actual = await db.prepare(`SELECT count(*) AS n FROM cloud_legacy_archive_rows
    WHERE run_id = ?`).bind(results[0].run_id).first();
  if (Number(actual.n) !== Number(results[0].row_count)) {
    throw new TypeError('legacy archive row count mismatch');
  }
  return results[0].run_id;
}

async function columns(db, run, table) {
  if (!TABLES.has(table)) throw new TypeError('unsupported legacy table');
  const { results } = await db.prepare(`SELECT column_name FROM cloud_legacy_archive_columns
    WHERE run_id = ? AND table_name = ? ORDER BY column_ordinal`).bind(run, table).all();
  const names = results.map(row => row.column_name);
  if (names.length && !REQUIRED.get(table).every(name => names.includes(name))) {
    throw new TypeError('legacy table columns incomplete');
  }
  return names;
}

async function page(db, run, table, cursor, limit) {
  const names = await columns(db, run, table);
  if (!names.length) return { rows: [], nextCursor: null };
  const { results } = await db.prepare(`SELECT row_ordinal, encoded_values, row_sha256
    FROM cloud_legacy_archive_rows WHERE run_id = ? AND table_name = ?
    AND row_ordinal > ? ORDER BY row_ordinal LIMIT ?`).bind(
    run, table, cursor, limit).all();
  const rows = [];
  for (const record of results) {
    if (await digest(record.encoded_values) !== record.row_sha256) {
      throw new TypeError('legacy row checksum mismatch');
    }
    rows.push({ ordinal: record.row_ordinal, ...decode(record.encoded_values, names) });
  }
  return { rows, nextCursor: results.length === limit ? results.at(-1).row_ordinal : null };
}

async function boundedAll(db, run, table, maximum = 200) {
  const result = await page(db, run, table, -1, maximum + 1);
  if (result.rows.length > maximum) throw new TypeError('legacy reference limit exceeded');
  return result.rows;
}

export async function readLegacyHistory(db, kind, { cursor = -1,
  accountFingerprint = null, limit = 10 } = {}) {
  if (!KINDS.has(kind) || !Number.isSafeInteger(cursor) || cursor < -1 ||
      !Number.isSafeInteger(limit) || limit < 1 || limit > 10 ||
      (accountFingerprint !== null && !FINGERPRINT.test(accountFingerprint))) {
    throw new TypeError('invalid legacy history scope');
  }
  const run = await activeRun(db);
  if (!run) return { rows: [], nextCursor: null, archived: false };
  const table = KINDS.get(kind);
  if (!(await columns(db, run, table)).length) {
    return { rows: [], nextCursor: null, archived: true, supported: false };
  }
  for (const dependency of kind === 'ledger' ? ['broker_accounts', 'instruments'] :
    kind === 'reports' ? ['report_deliveries'] : []) {
    if (!(await columns(db, run, dependency)).length) {
      throw new TypeError('legacy reference category unavailable');
    }
  }
  const accounts = kind === 'ledger' || kind === 'accounts' ?
    await boundedAll(db, run, 'broker_accounts') : [];
  const byId = new Map(accounts.map(row => [row.id, row]));
  const instruments = kind === 'ledger' ? await boundedAll(db, run, 'instruments', 2000) : [];
  const instrumentById = new Map(instruments.map(row => [row.id, row]));
  const deliveries = kind === 'reports' ?
    await boundedAll(db, run, 'report_deliveries', 500) : [];
  const deliveriesByReport = new Map();
  for (const item of deliveries) {
    if (!deliveriesByReport.has(item.report_id)) deliveriesByReport.set(item.report_id, []);
    deliveriesByReport.get(item.report_id).push(item);
  }
  let position = cursor;
  const output = [];
  for (let batch = 0; batch < 5 && output.length < limit; batch += 1) {
    const slice = await page(db, run, table, position, 20);
    if (!slice.rows.length) return { rows: output, nextCursor: null, archived: true };
    for (const row of slice.rows) {
      position = row.ordinal;
      if (kind === 'accounts') {
        if (accountFingerprint && row.account_fingerprint !== accountFingerprint) continue;
        if (await db.prepare(`SELECT 1 FROM cloud_accounts WHERE account_fingerprint = ?`)
          .bind(row.account_fingerprint).first()) continue;
        output.push({ id: row.id, broker: row.broker, accountFingerprint: row.account_fingerprint,
          maskedLabel: row.masked_label, baseCurrency: row.base_currency, source: 'legacy' });
      } else if (kind === 'ledger') {
        const account = byId.get(row.account_id);
        if (!account) throw new TypeError('orphan legacy ledger');
        const instrument = row.instrument_id === null ? null : instrumentById.get(row.instrument_id);
        if (row.instrument_id !== null && !instrument) throw new TypeError('orphan legacy instrument');
        if (accountFingerprint && account.account_fingerprint !== accountFingerprint) continue;
        if (await db.prepare(`SELECT 1 FROM cloud_transactions
          WHERE account_fingerprint = ? AND source_key = ?`).bind(
          account.account_fingerprint, row.source_key).first()) continue;
        output.push({ id: row.id, broker: account.broker,
          accountFingerprint: account.account_fingerprint, accountLabel: account.masked_label,
          sourceKey: row.source_key,
          status: row.status, transactionType: row.transaction_type,
          tradedAt: row.traded_at, currency: row.currency, quantity: row.quantity,
          grossAmount: row.gross_amount, instrumentMarket: instrument?.market || null,
          instrumentTicker: instrument?.ticker || null,
          source: 'legacy' });
      } else if (kind === 'reports') {
        output.push({ ...row, deliveries: (deliveriesByReport.get(row.id) || [])
          .map(item => ({ status: item.status, sentAt: item.sent_at })), source: 'legacy' });
      } else {
        output.push({ ...row, source: 'legacy' });
      }
      if (output.length === limit) break;
    }
    if (output.length === limit || slice.nextCursor === null) {
      return { rows: output, nextCursor: slice.nextCursor === null &&
        output.length < limit ? null : position, archived: true };
    }
  }
  return { rows: output, nextCursor: position, archived: true };
}
