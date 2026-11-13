// Internal state transitions only. Proofs are operator attestations, not a process killer.
import { reconcileCore } from './d1-core-import.mjs';
const HASH = /^[a-f0-9]{64}$/;
const validTime = value => typeof value === 'string' && /Z$/.test(value) && Number.isFinite(Date.parse(value));
export async function activateCutover(db, { expectedEpoch, proof, edits = false, sends = false }, now = new Date().toISOString()) {
  const row = await db.prepare('SELECT * FROM cloud_cutover WHERE singleton=1').first();
  if (!row || row.state !== 'VERIFIED' || row.epoch !== expectedEpoch ||
      !validTime(now) || !validTime(proof?.observedAt) ||
      Math.abs(Date.parse(now)-Date.parse(proof.observedAt)) > 300000 ||
      proof.runId !== row.run_id || proof.sourceSha !== row.source_sha256 ||
      proof.targetId !== row.target_id || proof.ownerUserId !== row.owner_user_id ||
      proof.pcStopped !== true || !HASH.test(proof.pcStopEvidenceSha) ||
      !HASH.test(proof.backupReadbackSha) || !HASH.test(proof.rollbackRehearsalSha) ||
      proof.deliveryStatesReconciled !== true || !validTime(proof.reportsAfter) ||
      Date.parse(proof.reportsAfter) < Date.parse(now) || typeof edits !== 'boolean' || typeof sends !== 'boolean') {
    throw new TypeError('fresh matching backup, stopped-PC and reconciled rollback proof required');
  }
  const run = await db.prepare('SELECT manifest_json FROM cloud_core_import_runs WHERE run_id=?').bind(row.run_id).first();
  const manifest = JSON.parse(run.manifest_json);
  if (proof.backupReadbackSha !== manifest.backupSha) throw new TypeError('backup readback differs from frozen source');
  await reconcileCore(db, manifest);
  const encoded = JSON.stringify(proof);
  await db.batch([
    db.prepare(`INSERT INTO cloud_cutover_audit SELECT epoch+1,'ACTIVE',? FROM cloud_cutover
      WHERE singleton=1 AND epoch=? AND state='VERIFIED'`).bind(encoded,expectedEpoch),
    db.prepare(`UPDATE cloud_cutover SET epoch=epoch+1,state='ACTIVE',activated_at=?,reports_after=?,
      edits_enabled=?,sends_enabled=?,proof_json=? WHERE singleton=1 AND epoch=? AND state='VERIFIED'`)
      .bind(now,proof.reportsAfter,Number(edits),Number(sends),encoded,expectedEpoch),
  ]);
  const current = await db.prepare('SELECT * FROM cloud_cutover WHERE singleton=1').first();
  if (current.epoch !== expectedEpoch+1 || current.proof_json !== encoded) throw new TypeError('cutover transition conflict');
  return current;
}
export async function pauseCutover(db, expectedEpoch, reason) {
  if (typeof reason !== 'string' || !reason || reason.length>200) throw new TypeError('safe pause reason required');
  const proof = JSON.stringify({ reason, externalSendsMayStillBeInFlight: true });
  await db.batch([
    db.prepare(`INSERT INTO cloud_cutover_audit SELECT epoch+1,'PAUSED',? FROM cloud_cutover
      WHERE singleton=1 AND epoch=? AND state='ACTIVE'`).bind(proof,expectedEpoch),
    db.prepare(`UPDATE cloud_cutover SET epoch=epoch+1,state='PAUSED',edits_enabled=0,sends_enabled=0,
      proof_json=? WHERE singleton=1 AND epoch=? AND state='ACTIVE'`).bind(proof,expectedEpoch),
  ]);
  const row = await db.prepare('SELECT * FROM cloud_cutover WHERE singleton=1').first();
  if (row?.epoch !== expectedEpoch+1 || row.state !== 'PAUSED') throw new TypeError('pause conflict');
  return { epoch: row.epoch, state: row.state, drained: false };
}
export async function requireActiveCutover(env, config, { scheduledAt = null } = {}) {
  const row = await env.DB.prepare('SELECT * FROM cloud_cutover WHERE singleton=1').first();
  if (!row || row.state !== 'ACTIVE' || row.target_id !== env.CLOUD_TARGET_ID ||
      row.owner_user_id !== config.chat || config.users.length !== 1 || String(config.users[0]) !== row.owner_user_id ||
      row.scope_fingerprint !== (env.KIWOOM_ACCUM_FINGERPRINT ?? null) || row.portfolio_id !== Number(env.CLOUD_PORTFOLIO_ID) || row.report_timezone !== env.REPORT_TIMEZONE || row.sends_enabled !== 1 ||
      (scheduledAt !== null && (!validTime(scheduledAt) || Date.parse(scheduledAt) <= Date.parse(row.reports_after)))) {
    throw new TypeError('operating cutover fence closed');
  }
  return row;
}

export async function legacyReportSuppressed(db, periodKey) {
  return Boolean(await db.prepare('SELECT 1 FROM cloud_legacy_report_holds WHERE period_key=?').bind(periodKey).first());
}
