// Internal D1 staging API. No HTTP route or operating-data caller is attached.
// Each chunk is atomic; readers see only a finalized VERIFIED run.
const HASH = /^[0-9a-f]{64}$/;
const RUN = /^legacy:[0-9a-f]{64}$/;
const NAME = /^[a-z][a-z0-9_]*$/;

async function hash(value) {
  const bytes = new Uint8Array(await crypto.subtle.digest('SHA-256',
    new TextEncoder().encode(value)));
  return [...bytes].map(x => x.toString(16).padStart(2, '0')).join('');
}

function manifestValid(manifest) {
  if (!RUN.test(manifest?.runId) ||
      !['sourceLogicalSha', 'backupSha', 'schemaSha', 'chunkRootSha']
        .every(key => HASH.test(manifest[key])) ||
      !['tableCount', 'rowCount', 'expectedChunks'].every(key =>
        Number.isSafeInteger(manifest[key]) && manifest[key] >= 0) ||
      manifest.expectedChunks < 1) throw new TypeError('invalid archive manifest');
  if (manifest.runId !== `legacy:${manifest.sourceLogicalSha}`) {
    throw new TypeError('archive manifest identity mismatch');
  }
}

async function pinnedRun(db, manifest) {
  const run = await db.prepare(`SELECT r.*, i.chunk_root_sha256, i.expected_chunks
    FROM cloud_legacy_archive_runs AS r
    JOIN cloud_legacy_archive_imports AS i ON i.run_id = r.run_id
    WHERE r.run_id = ?`).bind(manifest.runId).first();
  if (!run || run.source_logical_sha256 !== manifest.sourceLogicalSha ||
      run.source_backup_sha256 !== manifest.backupSha ||
      run.schema_sha256 !== manifest.schemaSha || run.table_count !== manifest.tableCount ||
      run.row_count !== manifest.rowCount || run.chunk_root_sha256 !== manifest.chunkRootSha ||
      run.expected_chunks !== manifest.expectedChunks) {
    throw new TypeError('archive pinned manifest mismatch');
  }
  return run;
}

export async function beginD1Archive(db, manifest) {
  manifestValid(manifest);
  for (const table of ['cloud_outbox', 'cloud_transactions', 'cloud_plans',
    'cloud_report_claims', 'cloud_update_claims']) {
    // Fixed table names only. A live service target is not a migration scratch DB.
    if (await db.prepare(`SELECT 1 FROM ${table} LIMIT 1`).first()) {
      throw new TypeError('active target is not an archive staging database');
    }
  }
  const existing = await db.prepare(`SELECT * FROM cloud_legacy_archive_runs LIMIT 2`).all();
  if (existing.results.length) {
    if (existing.results.length !== 1 || existing.results[0].run_id !== manifest.runId ||
        existing.results[0].source_logical_sha256 !== manifest.sourceLogicalSha ||
        existing.results[0].source_backup_sha256 !== manifest.backupSha ||
        existing.results[0].schema_sha256 !== manifest.schemaSha ||
        existing.results[0].table_count !== manifest.tableCount ||
        existing.results[0].row_count !== manifest.rowCount) {
      throw new TypeError('archive resume manifest mismatch');
    }
    const receipt = await db.prepare(`SELECT chunk_root_sha256, expected_chunks
      FROM cloud_legacy_archive_imports WHERE run_id = ?`).bind(manifest.runId).first();
    if (!receipt || receipt.chunk_root_sha256 !== manifest.chunkRootSha ||
        receipt.expected_chunks !== manifest.expectedChunks) {
      throw new TypeError('archive resume receipt mismatch');
    }
    return existing.results[0].status;
  }
  await db.batch([
    db.prepare(`INSERT INTO cloud_legacy_archive_runs VALUES (?,?,?,?,?,?,?)`).bind(
      manifest.runId, manifest.sourceLogicalSha, manifest.backupSha,
      manifest.schemaSha, manifest.tableCount, manifest.rowCount, 'STAGING'),
    db.prepare(`INSERT INTO cloud_legacy_archive_imports VALUES (?,?,?)`).bind(
      manifest.runId, manifest.chunkRootSha, manifest.expectedChunks),
  ]);
  return 'STAGING';
}

function itemStatement(db, runId, item) {
  if (item.type === 'row') {
    if (!NAME.test(item.table) || !Number.isSafeInteger(item.ordinal) ||
        item.ordinal < 0 || typeof item.encoded !== 'string' || !HASH.test(item.sha)) {
      throw new TypeError('invalid archive row');
    }
    return db.prepare(`INSERT INTO cloud_legacy_archive_rows VALUES (?,?,?,?,?)`).bind(
      runId, item.table, item.ordinal, item.encoded, item.sha);
  }
  if (item.type === 'column') {
    if (!NAME.test(item.table) || !NAME.test(item.name) ||
        !Number.isSafeInteger(item.ordinal) || item.ordinal < 0) {
      throw new TypeError('invalid archive column');
    }
    return db.prepare(`INSERT INTO cloud_legacy_archive_columns VALUES (?,?,?,?)`).bind(
      runId, item.table, item.ordinal, item.name);
  }
  if (item.type === 'schema') {
    if (!['table', 'index', 'view', 'trigger'].includes(item.objectType) ||
        !NAME.test(item.objectName) || !NAME.test(item.table) ||
        typeof item.sql !== 'string' || !item.sql.startsWith('CREATE ')) {
      throw new TypeError('invalid archive schema object');
    }
    return db.prepare(`INSERT INTO cloud_legacy_archive_schema VALUES (?,?,?,?,?)`).bind(
      runId, item.objectType, item.objectName, item.table, item.sql);
  }
  throw new TypeError('unsupported archive item');
}

export async function stageD1ArchiveChunk(db, manifest, chunk) {
  manifestValid(manifest);
  if (!Number.isSafeInteger(chunk?.index) || chunk.index < 0 ||
      chunk.index >= manifest.expectedChunks || !Array.isArray(chunk.items) ||
      !chunk.items.length || chunk.items.length > 50 || !HASH.test(chunk.sha) ||
      await hash(JSON.stringify(chunk.items)) !== chunk.sha) {
    throw new TypeError('invalid bounded archive chunk');
  }
  const run = await pinnedRun(db, manifest);
  if (run?.status !== 'STAGING') throw new TypeError('archive staging unavailable');
  const prior = await db.prepare(`SELECT chunk_sha256 FROM cloud_legacy_archive_chunks
    WHERE run_id = ? AND chunk_index = ?`).bind(manifest.runId, chunk.index).first();
  if (prior) {
    if (prior.chunk_sha256 !== chunk.sha) throw new TypeError('archive chunk replay mismatch');
    return 'REPLAY';
  }
  for (const item of chunk.items) if (item.type === 'row' &&
      await hash(item.encoded) !== item.sha) throw new TypeError('archive row hash mismatch');
  const statements = chunk.items.map(item => itemStatement(db, manifest.runId, item));
  statements.push(db.prepare(`INSERT INTO cloud_legacy_archive_chunks VALUES (?,?,?,?)`).bind(
    manifest.runId, chunk.index, chunk.sha,
    chunk.items.filter(item => item.type === 'row').length));
  await db.batch(statements);
  return 'STAGED';
}

export async function finalizeD1Archive(db, manifest) {
  manifestValid(manifest);
  const run = await pinnedRun(db, manifest);
  if (run?.status === 'VERIFIED') return 'VERIFIED';
  if (run?.status !== 'STAGING') throw new TypeError('archive staging unavailable');
  const { results: chunks } = await db.prepare(`SELECT chunk_index, chunk_sha256, row_count
    FROM cloud_legacy_archive_chunks WHERE run_id = ? ORDER BY chunk_index`).bind(
    manifest.runId).all();
  if (chunks.length !== manifest.expectedChunks || chunks.some((row, index) =>
    row.chunk_index !== index) ||
    await hash(JSON.stringify(chunks.map(row => row.chunk_sha256))) !== manifest.chunkRootSha ||
    chunks.reduce((sum, row) => sum + row.row_count, 0) !== manifest.rowCount) {
    throw new TypeError('archive chunks incomplete');
  }
  const actualRows = await db.prepare(`SELECT count(*) AS n FROM cloud_legacy_archive_rows
    WHERE run_id = ?`).bind(manifest.runId).first();
  const actualTables = await db.prepare(`SELECT count(DISTINCT table_name) AS n
    FROM cloud_legacy_archive_columns WHERE run_id = ?`).bind(manifest.runId).first();
  const { results: schema } = await db.prepare(`SELECT object_type, object_name,
    table_name, create_sql FROM cloud_legacy_archive_schema WHERE run_id = ?
    ORDER BY object_type, object_name`).bind(manifest.runId).all();
  const schemaRows = schema.map(row => [row.object_type, row.object_name,
    row.table_name, row.create_sql]);
  if (actualRows.n !== manifest.rowCount || actualTables.n !== manifest.tableCount ||
      await hash(JSON.stringify(schemaRows)) !== manifest.schemaSha) {
    throw new TypeError('archive D1 reconciliation failed');
  }
  const result = await db.prepare(`UPDATE cloud_legacy_archive_runs SET status = 'VERIFIED'
    WHERE run_id = ? AND status = 'STAGING'`).bind(manifest.runId).run();
  if (result.meta.changes !== 1) throw new TypeError('archive visibility conflict');
  return 'VERIFIED';
}
