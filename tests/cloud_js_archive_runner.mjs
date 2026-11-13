// Synthetic Python bundle -> local D1 contract probe. No remote binding.
import { readFileSync } from 'node:fs';
import { FakeD1 } from './cloud_js_fake_d1.mjs';
import { beginD1Archive, finalizeD1Archive, stageD1ArchiveChunk } from
  '../deploy/cloudflare-js/d1-archive-import.mjs';

const bundle = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const db = new FakeD1();
await beginD1Archive(db, bundle.manifest);
for (const chunk of bundle.chunks) await stageD1ArchiveChunk(db, bundle.manifest, chunk);
const status = await finalizeD1Archive(db, bundle.manifest);
const count = db.sqlite.prepare('SELECT count(*) AS n FROM cloud_legacy_archive_rows').get().n;
process.stdout.write(JSON.stringify({ status, count }));
