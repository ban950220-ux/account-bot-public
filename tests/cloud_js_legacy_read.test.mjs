import assert from 'node:assert/strict';
import { test } from 'node:test';
import { FakeD1 } from './cloud_js_fake_d1.mjs';
import { readLegacyHistory } from '../deploy/cloudflare-js/legacy-read.mjs';
import { createOperatingGateway } from '../deploy/cloudflare-js/operating-gateway.mjs';
import { createOperatingWorker } from '../deploy/cloudflare-js/operating-worker.mjs';
import { formatLegacyHistory } from '../deploy/cloudflare-js/legacy-history-report.mjs';

const accountA = `fixture_${'a'.repeat(64)}`;
const accountB = `fixture_${'b'.repeat(64)}`;
const run = `legacy:${'c'.repeat(64)}`;
async function sha(value) {
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256',
    new TextEncoder().encode(value)));
  return [...digest].map(x => x.toString(16).padStart(2, '0')).join('');
}
async function seed(db, table, names, rows) {
  for (let index = 0; index < names.length; index += 1) db.sqlite.prepare(`
    INSERT INTO cloud_legacy_archive_columns VALUES (?,?,?,?)`).run(run, table, index, names[index]);
  for (let ordinal = 0; ordinal < rows.length; ordinal += 1) {
    const encoded = JSON.stringify(rows[ordinal].map(value => value === null ?
      ['null', null] : ['text', String(value)]));
    db.sqlite.prepare(`INSERT INTO cloud_legacy_archive_rows VALUES (?,?,?,?,?)`)
      .run(run, table, ordinal, encoded, await sha(encoded));
  }
}
async function fixture() {
  const db = new FakeD1();
  db.sqlite.prepare(`INSERT INTO cloud_legacy_archive_runs VALUES (?,?,?,?,?,?,?)`)
    .run(run, 'd'.repeat(64), 'e'.repeat(64), 'f'.repeat(64), 7, 19, 'STAGING');
  await seed(db, 'broker_accounts', ['id', 'broker', 'account_fingerprint',
    'masked_label', 'base_currency'], [
    ['1', 'kiwoom', accountA, '****1234', 'KRW'],
    ['2', 'meritz', accountB, '****5678', 'KRW'],
  ]);
  await seed(db, 'instruments', ['id', 'market', 'ticker', 'asset_type', 'currency'], [
    ['1', 'NASDAQ', 'FIXTURE', 'EQUITY', 'USD'],
  ]);
  await seed(db, 'transactions', ['id', 'account_id', 'source_key', 'instrument_id',
    'currency', 'quantity', 'status', 'transaction_type', 'traded_at', 'gross_amount'],
  Array.from({ length: 12 }, (_, index) => [String(index + 1), index === 0 ? '1' : '2',
    `fill-${index}`, '1', 'USD', '0.125', index === 0 ? 'CORRECTED' : 'FILLED',
    'BUY', index === 0 ? '2026-02-02T00:30:00+09:00' : '2026-02-01T15:30:00Z',
    index === 2 ? null : '10.0125']));
  await seed(db, 'reports', ['id', 'report_type', 'period_key', 'status', 'completed_at'], [
    ['1', 'WEEKLY', '2026-W05', 'READY', '2026-02-02T00:00:00Z'],
  ]);
  await seed(db, 'report_deliveries', ['id', 'report_id', 'status', 'sent_at'], [
    ['1', '1', 'UNKNOWN', null],
  ]);
  await seed(db, 'recurring_buy_occurrences', ['id', 'plan_id', 'local_due_date',
    'planned_amount', 'currency', 'actual_amount', 'status'], [
    ['1', '4', '2026-02-02', '5000', 'KRW', null, 'UNKNOWN'],
  ]);
  await seed(db, 'portfolio_snapshots', ['id', 'data_as_of', 'status',
    'total_market_value', 'base_currency'], [
    ['1', '2026-02-02T00:00:00Z', 'PARTIAL', null, 'KRW'],
  ]);
  db.sqlite.prepare(`UPDATE cloud_legacy_archive_runs SET status='VERIFIED'
    WHERE run_id=?`).run(run);
  return db;
}

test('legacy ledger pages preserve source, time, correction and missing money', async () => {
  const db = await fixture();
  const changesBefore = db.sqlite.prepare('SELECT total_changes() AS n').get().n;
  const first = await readLegacyHistory(db, 'ledger');
  assert.equal(first.rows.length, 10);
  assert.equal(first.rows[0].status, 'CORRECTED');
  assert.equal(first.rows[0].tradedAt, '2026-02-02T00:30:00+09:00');
  assert.equal(first.rows[2].grossAmount, null);
  assert.equal(first.rows[0].accountFingerprint, accountA);
  assert.equal(first.rows[1].accountFingerprint, accountB);
  const second = await readLegacyHistory(db, 'ledger', { cursor: first.nextCursor });
  assert.equal(second.rows.length, 2);
  assert.equal(second.nextCursor, null);
  const scoped = await readLegacyHistory(db, 'ledger', { accountFingerprint: accountA });
  assert.equal(scoped.rows.length, 1);
  assert.ok(scoped.rows.every(row => row.accountFingerprint === accountA));
  assert.equal(db.sqlite.prepare('SELECT count(*) AS n FROM cloud_outbox').get().n, 0);
  assert.equal(db.sqlite.prepare('SELECT total_changes() AS n').get().n, changesBefore);
});

test('current ledger identity wins and old UNKNOWN stays read-only', async () => {
  const db = await fixture();
  db.sqlite.prepare(`INSERT INTO cloud_accounts VALUES (?,?,?,?)`)
    .run(accountA, 'kiwoom', '****1234', 'KRW');
  db.sqlite.prepare(`INSERT INTO cloud_transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`)
    .run(accountA, 'fill-0', 'kiwoom', 'NASDAQ', 'FIXTURE', 'EQUITY', 'USD',
      'BUY', 'FILLED', '2026-02-02T00:30:00Z', null, '0.125', null,
      '10.0125', null, null, null);
  const ledger = await readLegacyHistory(db, 'ledger');
  assert.equal(ledger.rows.length, 10);
  assert.ok(ledger.rows.every(row => row.sourceKey !== 'fill-0'));
  const accounts = await readLegacyHistory(db, 'accounts');
  assert.deepEqual(accounts.rows.map(row => row.accountFingerprint), [accountB]);
  const reports = await readLegacyHistory(db, 'reports');
  assert.equal(reports.rows[0].deliveries[0].status, 'UNKNOWN');
  const occurrences = await readLegacyHistory(db, 'occurrences');
  assert.equal(occurrences.rows[0].actual_amount, null);
  const snapshots = await readLegacyHistory(db, 'snapshots');
  assert.equal(snapshots.rows[0].total_market_value, null);
  assert.match(formatLegacyHistory('snapshots', snapshots), /평가액 미확정/);
  assert.match(formatLegacyHistory('reports', reports), /UNKNOWN/);
  assert.match(formatLegacyHistory('ledger', ledger), /거래금액 미확정/);
  assert.equal(db.sqlite.prepare('SELECT count(*) AS n FROM cloud_outbox').get().n, 0);
});

test('owner-only history command creates a reply but never activates a legacy event', async () => {
  const db = await fixture();
  const env = { DB: db, TELEGRAM_BOT_TOKEN: `123456:${'a'.repeat(36)}`,
    TELEGRAM_WEBHOOK_SECRET: 'synthetic-secret', TELEGRAM_ALLOWED_USER_IDS: '42',
    TELEGRAM_CHAT_ID: '42', REPORT_TIMEZONE: 'Asia/Seoul', DAILY_REPORT_TIME: '10:00',
    WEEKLY_REPORT_TIME: '18:00', WEEKLY_REPORT_WEEKDAY: '5',
    KIWOOM_MAIN_APP_KEY: 'fixture-key', KIWOOM_MAIN_APP_SECRET: 'fixture-secret',
    KIWOOM_MAIN_ACCOUNT_ALIAS: 'local-alias', KIWOOM_ACCUM_FINGERPRINT: accountA };
  let sends = 0;
  const worker = createOperatingWorker({ gatewayFactory: input => createOperatingGateway(input,
    { fetcher: async () => { throw new Error('broker must not be called'); } }),
  fetcher: async () => { sends += 1; return { ok: true,
    json: async () => ({ ok: true, result: { message_id: sends } }) }; },
  now: () => '2026-02-02T00:00:00Z' });
  const request = (id, owner) => new Request('https://fixture.invalid/telegram/webhook', {
    method: 'POST', headers: { 'X-Telegram-Bot-Api-Secret-Token': 'synthetic-secret' },
    body: JSON.stringify({ update_id: id, message: { from: { id: owner },
      chat: { id: owner, type: 'private' }, text: '/history reports' } }) });
  const changesBeforeDenied = db.sqlite.prepare('SELECT total_changes() AS n').get().n;
  assert.equal((await worker.fetch(request(300, 43), env)).status, 403);
  assert.equal(sends, 0);
  assert.equal(db.sqlite.prepare('SELECT total_changes() AS n').get().n,
    changesBeforeDenied);
  assert.equal((await worker.fetch(request(301, 42), env)).status, 200);
  assert.equal((await worker.fetch(request(301, 42), env)).status, 200);
  assert.equal(sends, 1);
  assert.equal(db.sqlite.prepare('SELECT count(*) AS n FROM cloud_transactions').get().n, 0);
  assert.equal(db.sqlite.prepare('SELECT count(*) AS n FROM cloud_report_claims').get().n, 0);
  assert.equal(db.sqlite.prepare('SELECT count(*) AS n FROM cloud_outbox').get().n, 1);
});

test('missing legacy schema is explicit and malformed columns never masquerade as empty',
  async () => {
    const db = await fixture();
    db.sqlite.prepare(`DELETE FROM cloud_legacy_archive_columns WHERE table_name = 'reports'`)
      .run();
    const missing = await readLegacyHistory(db, 'reports');
    assert.equal(missing.supported, false);
    db.sqlite.prepare(`DELETE FROM cloud_legacy_archive_columns
      WHERE table_name = 'transactions' AND column_name = 'gross_amount'`).run();
    await assert.rejects(() => readLegacyHistory(db, 'ledger'), /columns incomplete/);
  });

test('truncated or tampered archive fails closed before display', async () => {
  const db = await fixture();
  db.sqlite.prepare(`UPDATE cloud_legacy_archive_runs SET row_count = 20`).run();
  await assert.rejects(() => readLegacyHistory(db, 'ledger'), /row count mismatch/);
  db.sqlite.prepare(`UPDATE cloud_legacy_archive_runs SET row_count = 19`).run();
  db.sqlite.prepare(`UPDATE cloud_legacy_archive_rows SET encoded_values = '[]'
    WHERE table_name = 'transactions' AND row_ordinal = 0`).run();
  await assert.rejects(() => readLegacyHistory(db, 'ledger'), /checksum mismatch/);
});
