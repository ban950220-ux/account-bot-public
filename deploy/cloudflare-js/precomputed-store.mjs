// Stored precomputed replies: the input fingerprint, the Worker's read with its
// usability rules, and the Actions job's write. Small on purpose: the Worker
// imports it on every precomputed command.
import { zonedParts } from './zoned-time.mjs';

export const MAX_AGE_MS = 2 * 60 * 60 * 1000;
export const COMMAND_KINDS = Object.freeze({ recurring: 'recurring', weekly: 'weekly_cmd',
  recommend: 'recommend' });
export const KINDS = Object.freeze(['recurring', 'weekly_cmd', 'recommend', 'daily_report', 'weekly_report']);

// Everything a precomputed reply depends on besides public prices and signals,
// which age by time instead. Every plan, archive, policy, thesis and manual-input
// change appends a row, so counts and the last row id move on every change.
const FINGERPRINT_SQL = `SELECT
  (SELECT count(*) || '.' || coalesce(max(rowid), 0) || '.' || coalesce(sum(version), 0) FROM cloud_plans) AS plans,
  (SELECT count(*) || '.' || coalesce(max(rowid), 0) FROM cloud_plan_revisions) AS revisions,
  (SELECT count(*) || '.' || coalesce(max(rowid), 0) FROM cloud_plan_scope) AS scope,
  (SELECT count(*) || '.' || coalesce(max(rowid), 0) FROM cloud_plan_archive_events) AS archive,
  (SELECT coalesce(max(version), 0) FROM cloud_policy) || '.' ||
    (SELECT count(*) FROM cloud_policy_revisions) AS policy,
  (SELECT count(*) || '.' || coalesce(max(rowid), 0) FROM cloud_thesis_revisions) AS thesis,
  (SELECT count(*) || '.' || coalesce(max(rowid), 0) FROM cloud_manual_input_revisions) AS manual,
  (SELECT coalesce(max(epoch || ':' || state || ':' || edits_enabled || ':' || sends_enabled), '-')
    FROM cloud_cutover) AS cutover`;

export async function inputFingerprint(db) {
  const row = await db.prepare(FINGERPRINT_SQL).first();
  if (!row) throw new Error('input fingerprint unavailable');
  return ['plans', 'revisions', 'scope', 'archive', 'policy', 'thesis', 'manual', 'cutover']
    .map(key => `${key}=${row[key]}`).join(';');
}

// A stored reply, or null when the Worker must compute it itself: no row (or no
// table yet), inputs changed since it was computed, older than MAX_AGE_MS, from
// the future, or for another report period.
export async function usablePrecomputed(db, kind, { now, periodKey = '' }) {
  if (!KINDS.includes(kind)) throw new TypeError('unknown precomputed kind');
  let row;
  try {
    row = await db.prepare(`SELECT period_key, input_fingerprint, computed_at, body
      FROM cloud_precomputed WHERE kind = ?`).bind(kind).first();
  } catch { return null; }
  if (!row || row.period_key !== periodKey || typeof row.body !== 'string' || !row.body) return null;
  const age = Date.parse(now) - Date.parse(row.computed_at);
  if (!Number.isFinite(age) || age < 0 || age > MAX_AGE_MS) return null;
  if (row.input_fingerprint !== await inputFingerprint(db)) return null;
  return { body: row.body, computedAt: row.computed_at };
}

export function asOfLine(computedAt, timezone) {
  const p = zonedParts(new Date(computedAt), timezone);
  const zone = timezone === 'Asia/Seoul' ? 'KST' : timezone;
  return `🕘 ${Number(p.month)}/${Number(p.day)} ${p.hour}:${p.minute} ${zone} 기준`;
}

export async function storePrecomputed(db, items) {
  for (const item of items) {
    if (!KINDS.includes(item.kind)) throw new TypeError('unknown precomputed kind');
    await db.prepare(`INSERT INTO cloud_precomputed
      (kind, period_key, input_fingerprint, computed_at, body) VALUES (?, ?, ?, ?, ?)
      ON CONFLICT (kind) DO UPDATE SET period_key = excluded.period_key,
        input_fingerprint = excluded.input_fingerprint, computed_at = excluded.computed_at,
        body = excluded.body`).bind(item.kind, item.periodKey, item.inputFingerprint,
      item.computedAt, item.body).run();
  }
}
