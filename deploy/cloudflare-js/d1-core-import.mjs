// Internal operator boundary only. No HTTP route, credential, or fetch call.
import { normalizeMeritzAccountSnapshot, normalizeMeritzRecurringPlans } from './manual-input.mjs';
export const CORE_COLUMNS = Object.freeze({
  cloud_accounts: 'account_fingerprint broker masked_label base_currency'.split(' '),
  cloud_plans: 'plan_key account_fingerprint source market ticker asset_type instrument_currency amount_currency daily_amount target_weight enabled timezone_name started_at memo version last_mutation_key'.split(' '),
  cloud_plan_revisions: 'plan_key revision_no change_hash source account_fingerprint market ticker asset_type instrument_currency enabled daily_amount amount_currency target_weight timezone_name started_at effective_from memo mutation_key'.split(' '),
  cloud_transactions: 'account_fingerprint source_key source market ticker asset_type currency transaction_type status traded_at settled_at quantity unit_price gross_amount fee tax net_amount'.split(' '),
  cloud_theses: 'instrument_key market ticker currency current_revision_no last_mutation_key'.split(' '),
  cloud_thesis_revisions: 'instrument_key revision_no revision_key content_hash thesis_text effective_at mutation_key'.split(' '),
  cloud_manual_input_revisions: 'source kind revision mutation_key content_hash data_as_of stored_at document_json'.split(' '),
  cloud_legacy_report_holds: 'period_key report_type local_date legacy_states'.split(' '),
  cloud_plan_scope: 'plan_key portfolio_id'.split(' '),
  cloud_plan_lifecycle: 'plan_key ended_at'.split(' '),
});
export async function sha(value) {
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256',
    new TextEncoder().encode(JSON.stringify(value))))].map(x => x.toString(16).padStart(2, '0')).join('');
}
const HASH = /^[a-f0-9]{64}$/;
const FINGERPRINT = /^[a-z][a-z0-9_]{0,30}_[a-f0-9]{64}$/;
const JSON_LIMIT = value => new TextEncoder().encode(JSON.stringify(value)).length;
async function validate(manifest, metadata) {
  const { runId, ...identity } = manifest || {};
  const scope = metadata?.scope;
  if (manifest.kind !== 'CORE_PROJECTION_V1' || runId !== `core:${await sha(identity)}` ||
      !['sourceLogicalSha','backupSha','scopeSha','rowsSha','chunkRootSha'].every(k => HASH.test(manifest[k])) ||
      manifest.archiveRunId !== `legacy:${manifest.sourceLogicalSha}` ||
      !Number.isSafeInteger(manifest.expectedChunks) || manifest.expectedChunks < 1 || manifest.expectedChunks > 5000 ||
      JSON.stringify(Object.keys(manifest.counts)) !== JSON.stringify(Object.keys(CORE_COLUMNS)) ||
      Object.values(manifest.counts).some(n => !Number.isSafeInteger(n) || n < 0) ||
      Object.values(manifest.counts).reduce((a,b) => a+b,0) > 5000 ||
      await sha(metadata) !== manifest.scopeSha || JSON_LIMIT(metadata) > 262144 ||
      !scope || !/^[1-9][0-9]*$/.test(scope.ownerUserId) ||
      !Number.isSafeInteger(Number(scope.ownerUserId)) || typeof scope.targetId !== 'string' || !scope.targetId ||
      !Number.isSafeInteger(scope.portfolioId) || scope.portfolioId < 1 ||
      !Array.isArray(scope.accountFingerprints) || !scope.accountFingerprints.length ||
      scope.accountFingerprints.some(x => !FINGERPRINT.test(x)) ||
      (scope.recurringAccountFingerprint !== null && (!FINGERPRINT.test(scope.recurringAccountFingerprint) ||
        !scope.accountFingerprints.includes(scope.recurringAccountFingerprint)))) {
    throw new TypeError('invalid core manifest or explicit scope');
  }
}
async function pinned(db, manifest) {
  const row = await db.prepare('SELECT * FROM cloud_core_import_runs WHERE run_id=?').bind(manifest.runId).first();
  if (!row || row.manifest_json !== JSON.stringify(manifest)) throw new TypeError('core manifest mismatch');
  return row;
}
export async function beginCoreImport(db, manifest, metadata) {
  await validate(manifest, metadata);
  const archive = await db.prepare('SELECT * FROM cloud_legacy_archive_runs WHERE run_id=?').bind(manifest.archiveRunId).first();
  if (archive?.status !== 'VERIFIED' || archive.source_backup_sha256 !== manifest.backupSha ||
      archive.source_logical_sha256 !== manifest.sourceLogicalSha) throw new TypeError('matching verified archive required');
  const existing = await db.prepare('SELECT * FROM cloud_core_import_runs LIMIT 1').first();
  if (existing) {
    const row = await pinned(db, manifest);
    if (row.metadata_json !== JSON.stringify(metadata)) throw new TypeError('core metadata mismatch');
    return row.status;
  }
  for (const table of [...Object.keys(CORE_COLUMNS), 'cloud_outbox','cloud_update_claims','cloud_report_claims',
    'cloud_snapshots','cloud_snapshot_sources','cloud_position_snapshots','cloud_cash_snapshots',
    'cloud_manual_input_revisions','cloud_mutation_checks','cloud_cutover']) {
    if (await db.prepare(`SELECT 1 FROM ${table} LIMIT 1`).first()) throw new TypeError('core import requires inert empty target');
  }
  const s = metadata.scope;
  await db.batch([
    db.prepare('INSERT INTO cloud_core_import_runs VALUES (?,?,?,?)').bind(manifest.runId, JSON.stringify(manifest), JSON.stringify(metadata), 'STAGING'),
    db.prepare(`INSERT INTO cloud_cutover(singleton,run_id,epoch,state,owner_user_id,target_id,scope_fingerprint,source_sha256,report_timezone,portfolio_id)
      VALUES (1,?,1,'STAGING',?,?,?,?,?,?)`).bind(manifest.runId,s.ownerUserId,s.targetId,s.recurringAccountFingerprint,manifest.sourceLogicalSha,s.reportTimezone,s.portfolioId),
  ]);
  return 'STAGING';
}
export async function stageCoreChunk(db, manifest, chunk) {
  const run = await pinned(db, manifest);
  if (run.status !== 'STAGING') throw new TypeError('core import sealed');
  if (!Number.isSafeInteger(chunk?.index) || chunk.index < 0 || chunk.index >= manifest.expectedChunks ||
      !Array.isArray(chunk.items) || !chunk.items.length || chunk.items.length > 20 ||
      JSON_LIMIT(chunk.items) > 327680 || await sha(chunk.items) !== chunk.sha) throw new TypeError('invalid core chunk');
  const prior = await db.prepare('SELECT chunk_sha256 FROM cloud_core_import_chunks WHERE run_id=? AND chunk_index=?').bind(manifest.runId,chunk.index).first();
  if (prior) {
    if (prior.chunk_sha256 !== chunk.sha) throw new TypeError('core chunk replay mismatch');
    return 'REPLAY';
  }
  const count = await db.prepare('SELECT count(*) AS n FROM cloud_core_import_chunks WHERE run_id=?').bind(manifest.runId).first();
  if (count.n !== chunk.index) throw new TypeError('core chunks must preserve dependency order');
  for (const item of chunk.items) if (item.table === 'cloud_manual_input_revisions') {
    const row = Object.fromEntries(CORE_COLUMNS[item.table].map((key,i) => [key,item.values[i]]));
    const normalized = row.kind === 'ACCOUNT_SNAPSHOT' ? normalizeMeritzAccountSnapshot(JSON.parse(row.document_json)) :
      row.kind === 'RECURRING_PLANS' ? normalizeMeritzRecurringPlans(JSON.parse(row.document_json)) : null;
    if (!normalized || row.source !== 'meritz' || row.revision !== 1 || Date.parse(normalized.dataAsOf) !== Date.parse(row.data_as_of) ||
        JSON.stringify(normalized.document) !== row.document_json || await sha(normalized.document) !== row.content_hash) {
      throw new TypeError('manual projection not normalized');
    }
  }
  const statements = chunk.items.map(item => {
    const columns = CORE_COLUMNS[item.table];
    if (!columns || !Array.isArray(item.values) || item.values.length !== columns.length ||
        JSON_LIMIT(item) > 16384 || item.values.some(v => v !== null && typeof v !== 'string' && !Number.isSafeInteger(v))) {
      throw new TypeError('invalid core row');
    }
    return db.prepare(`INSERT INTO ${item.table} (${columns.join(',')}) VALUES (${columns.map(() => '?').join(',')})`).bind(...item.values);
  });
  statements.push(db.prepare('INSERT INTO cloud_core_import_chunks VALUES (?,?,?,?)').bind(manifest.runId,chunk.index,chunk.sha,JSON.stringify(chunk.items)));
  await db.batch(statements);
  return 'STAGED';
}
export async function reconcileCore(db, manifest) {
  await pinned(db, manifest);
  const { results: chunks } = await db.prepare('SELECT * FROM cloud_core_import_chunks WHERE run_id=? ORDER BY chunk_index').bind(manifest.runId).all();
  if (chunks.length !== manifest.expectedChunks || chunks.some((c,i) => c.chunk_index !== i) ||
      await sha(chunks.map(c => c.chunk_sha256)) !== manifest.chunkRootSha) throw new TypeError('core chunks incomplete');
  const items = [];
  for (const c of chunks) {
    const values = JSON.parse(c.items_json);
    if (await sha(values) !== c.chunk_sha256) throw new TypeError('core stored chunk changed');
    items.push(...values);
  }
  if (await sha(items) !== manifest.rowsSha) throw new TypeError('core row root mismatch');
  for (const [table, columns] of Object.entries(CORE_COLUMNS)) {
    const expected = items.filter(item => item.table === table).map(item => JSON.stringify(item.values)).sort();
    const { results } = await db.prepare(`SELECT ${columns.join(',')} FROM ${table} LIMIT 5001`).all();
    const actual = results.map(row => JSON.stringify(columns.map(c => row[c]))).sort();
    if (actual.length !== manifest.counts[table] || JSON.stringify(actual) !== JSON.stringify(expected)) throw new TypeError('active core reconciliation failed');
  }
  return true;
}
export async function finalizeCoreImport(db, manifest) {
  const run = await pinned(db, manifest);
  if (run.status === 'VERIFIED') return 'VERIFIED';
  await reconcileCore(db, manifest);
  await db.batch([
    db.prepare("UPDATE cloud_core_import_runs SET status='VERIFIED' WHERE run_id=? AND status='STAGING'").bind(manifest.runId),
    db.prepare("UPDATE cloud_cutover SET state='VERIFIED' WHERE run_id=? AND state='STAGING'").bind(manifest.runId),
  ]);
  return 'VERIFIED';
}
