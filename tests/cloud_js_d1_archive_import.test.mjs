import assert from 'node:assert/strict';
import { test } from 'node:test';
import { FakeD1 } from './cloud_js_fake_d1.mjs';
import { beginD1Archive, finalizeD1Archive, stageD1ArchiveChunk } from
  '../deploy/cloudflare-js/d1-archive-import.mjs';
import { readLegacyHistory } from '../deploy/cloudflare-js/legacy-read.mjs';

async function sha(text) {
  const bytes = new Uint8Array(await crypto.subtle.digest('SHA-256',
    new TextEncoder().encode(text)));
  return [...bytes].map(x => x.toString(16).padStart(2, '0')).join('');
}
async function fixture() {
  const fingerprint = `fixture_${'a'.repeat(64)}`;
  const encoded = JSON.stringify([['text', '1'], ['text', 'kiwoom'],
    ['text', fingerprint], ['text', '****1234'], ['text', 'KRW']]);
  const schema = { type: 'schema', objectType: 'table', objectName: 'broker_accounts',
    table: 'broker_accounts', sql: 'CREATE TABLE broker_accounts (id INTEGER)' };
  const columns = ['id', 'broker', 'account_fingerprint', 'masked_label', 'base_currency']
    .map((name, ordinal) => ({ type: 'column', table: 'broker_accounts', ordinal, name }));
  const row = { type: 'row', table: 'broker_accounts', ordinal: 0,
    encoded, sha: await sha(encoded) };
  const chunks = [{ index: 0, items: [schema, ...columns] },
    { index: 1, items: [row] }];
  for (const chunk of chunks) chunk.sha = await sha(JSON.stringify(chunk.items));
  const manifest = { runId: `legacy:${'b'.repeat(64)}`,
    sourceLogicalSha: 'b'.repeat(64), backupSha: 'c'.repeat(64),
    schemaSha: await sha(JSON.stringify([['table', 'broker_accounts',
      'broker_accounts', schema.sql]])), tableCount: 1, rowCount: 1,
    expectedChunks: 2, chunkRootSha: await sha(JSON.stringify(chunks.map(x => x.sha))) };
  return { manifest, chunks, fingerprint };
}

test('bounded D1 staging stays invisible until complete and is replay safe', async () => {
  const db = new FakeD1();
  const { manifest, chunks, fingerprint } = await fixture();
  assert.equal(await beginD1Archive(db, manifest), 'STAGING');
  assert.equal(await stageD1ArchiveChunk(db, manifest, chunks[0]), 'STAGED');
  assert.equal((await readLegacyHistory(db, 'accounts')).archived, false);
  await assert.rejects(() => finalizeD1Archive(db, manifest), /incomplete/);
  assert.equal(await stageD1ArchiveChunk(db, manifest, chunks[0]), 'REPLAY');
  assert.equal(await stageD1ArchiveChunk(db, manifest, chunks[1]), 'STAGED');
  assert.equal(await finalizeD1Archive(db, manifest), 'VERIFIED');
  assert.equal(await beginD1Archive(db, manifest), 'VERIFIED');
  assert.equal((await readLegacyHistory(db, 'accounts')).rows[0].accountFingerprint,
    fingerprint);
  assert.equal(db.sqlite.prepare('SELECT count(*) AS n FROM cloud_outbox').get().n, 0);
  assert.throws(() => db.sqlite.prepare(`INSERT INTO cloud_legacy_archive_rows
    VALUES (?,?,?,?,?)`).run(manifest.runId, 'broker_accounts', 2, '[]', '0'.repeat(64)),
  /archive sealed/);
});

test('late D1 chunk failure rolls back whole chunk and can resume safely', async () => {
  const db = new FakeD1();
  const { manifest, chunks } = await fixture();
  await beginD1Archive(db, manifest);
  await stageD1ArchiveChunk(db, manifest, chunks[0]);
  db.sqlite.exec(`CREATE TRIGGER synthetic_fail BEFORE INSERT ON cloud_legacy_archive_chunks
    WHEN NEW.chunk_index = 1 BEGIN SELECT RAISE(ABORT, 'fixture failure'); END`);
  await assert.rejects(() => stageD1ArchiveChunk(db, manifest, chunks[1]), /fixture failure/);
  assert.equal(db.sqlite.prepare('SELECT count(*) AS n FROM cloud_legacy_archive_rows').get().n,
    0);
  assert.equal(db.sqlite.prepare('SELECT count(*) AS n FROM cloud_legacy_archive_chunks').get().n,
    1);
  db.sqlite.exec('DROP TRIGGER synthetic_fail');
  await stageD1ArchiveChunk(db, manifest, chunks[1]);
  await finalizeD1Archive(db, manifest);
});

test('changed chunk and active target cannot be silently promoted', async () => {
  const db = new FakeD1();
  const { manifest, chunks } = await fixture();
  await beginD1Archive(db, manifest);
  await stageD1ArchiveChunk(db, manifest, chunks[0]);
  await assert.rejects(() => stageD1ArchiveChunk(db, manifest,
    { ...chunks[0], sha: '0'.repeat(64) }), /invalid bounded/);
  db.sqlite.prepare(`INSERT INTO cloud_outbox
    (event_key,destination_key,payload_json,status,created_at)
    VALUES ('fixture','fixture','{}','UNKNOWN','2026-02-02T00:00:00Z')`).run();
  await assert.rejects(() => beginD1Archive(db, manifest), /active target/);
});

test('every stage and finalize pins the original manifest, including verified replay', async () => {
  const db = new FakeD1();
  const { manifest, chunks } = await fixture();
  await beginD1Archive(db, manifest);
  for (const key of ['backupSha', 'sourceLogicalSha', 'schemaSha', 'chunkRootSha',
    'tableCount', 'rowCount', 'expectedChunks']) {
    const changed = { ...manifest, [key]: typeof manifest[key] === 'string' ?
      '0'.repeat(64) : manifest[key] + 1 };
    await assert.rejects(() => stageD1ArchiveChunk(db, changed, chunks[0]), /manifest/);
    await assert.rejects(() => finalizeD1Archive(db, changed), /manifest/);
  }
  for (const chunk of chunks) await stageD1ArchiveChunk(db, manifest, chunk);
  await finalizeD1Archive(db, manifest);
  await assert.rejects(() => finalizeD1Archive(db,
    { ...manifest, backupSha: '0'.repeat(64) }), /manifest/);
  assert.equal(await finalizeD1Archive(db, manifest), 'VERIFIED');
});
