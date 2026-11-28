// Heavy read-only replies, computed outside the Worker (GitHub Actions has no
// 10 ms CPU limit) from the same D1 data and the same code. The Worker sends a
// stored reply only while the inputs it was computed from are unchanged.
import { renderCommand, PRECOMPUTED_COMMANDS } from './command-router.mjs';
import { buildReportText } from './operating-worker.mjs';
import { createOperatingGateway } from './operating-gateway.mjs';
import { zonedParts } from './zoned-time.mjs';
import { COMMAND_KINDS, inputFingerprint } from './precomputed-store.mjs';

export { COMMAND_KINDS, inputFingerprint, storePrecomputed } from './precomputed-store.mjs';



export function localDay(iso, timezone) {
  const fields = zonedParts(new Date(iso), timezone);
  return `${fields.year}${fields.month}${fields.day}`;
}

function weekdayOf(day) {
  const date = new Date(Date.UTC(Number(day.slice(0, 4)), Number(day.slice(4, 6)) - 1,
    Number(day.slice(6, 8))));
  return (date.getUTCDay() + 6) % 7; // Monday = 0, as WEEKLY_REPORT_WEEKDAY
}

// Computes every precomputed reply. The fingerprint is read before and after;
// a change in between means the replies may mix old and new data, so none is kept.
export async function computeAll(env, { now = () => new Date().toISOString(), fetcher = fetch,
  gatewayFactory = (e, options) => createOperatingGateway(e, options) } = {}) {
  const before = await inputFingerprint(env.DB);
  const computedAt = now();
  const gateway = await gatewayFactory(env, { now, fetcher });
  const items = [];
  for (const command of PRECOMPUTED_COMMANDS) {
    items.push({ kind: COMMAND_KINDS[command], periodKey: '', body: await renderCommand(command, gateway) });
  }
  const day = localDay(computedAt, env.REPORT_TIMEZONE);
  // Same days as dueJobs: the daily report on weekdays, the weekly one on its weekday.
  if (weekdayOf(day) < 5) {
    items.push({ kind: 'daily_report', periodKey: `daily:${day}`,
      body: await buildReportText('daily', gateway) });
  }
  if (weekdayOf(day) === Number(env.WEEKLY_REPORT_WEEKDAY)) {
    items.push({ kind: 'weekly_report', periodKey: `weekly:${day}`,
      body: await buildReportText('weekly', gateway) });
  }
  const after = await inputFingerprint(env.DB);
  if (before !== after) return { stable: false, items: [] };
  return { stable: true, items: items.map(item => ({ ...item, inputFingerprint: before, computedAt })) };
}
