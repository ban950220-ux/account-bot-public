// Point-in-time view of recurring plans, built only from existing revisions.
// Pure functions: the gateway supplies plans and revisions, nothing is written here.
import { add, compare, decimal, exact, multiply } from './finance.mjs';

const KST_OFFSET_MS = 9 * 60 * 60 * 1000;
const pad = value => String(value).padStart(2, '0');

// `2026-11-20`, `11/20`, `11-20`, optionally followed by `08:00` or `08:00:30`, in KST.
// A date alone means the end of that day. Returns { at: Date, label } or null.
export function parseKstMoment(dateText, timeText = null, now = new Date()) {
  const full = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(dateText);
  const short = /^(\d{1,2})[/-](\d{1,2})$/.exec(dateText);
  if (!full && !short) return null;
  const kstNow = new Date(now.getTime() + KST_OFFSET_MS);
  let [year, month, day] = full ? full.slice(1).map(Number) :
    [kstNow.getUTCFullYear(), Number(short[1]), Number(short[2])];
  let hour = 23; let minute = 59; let second = 59;
  if (timeText !== null) {
    const time = /^(\d{1,2}):(\d{2})(?::(\d{2}))?$/.exec(timeText);
    if (!time) return null;
    [hour, minute, second] = [Number(time[1]), Number(time[2]), Number(time[3] ?? 0)];
  }
  const build = y => new Date(Date.UTC(y, month - 1, day, hour, minute, second) - KST_OFFSET_MS);
  let at = build(year);
  // A short date that lands in the future means last year's date.
  if (!full && at > now) { year -= 1; at = build(year); }
  const check = new Date(at.getTime() + KST_OFFSET_MS);
  if (check.getUTCMonth() !== month - 1 || check.getUTCDate() !== day ||
      hour > 23 || minute > 59 || second > 59) return null;
  return { at, label: formatKst(at, true) };
}

export function formatKst(date, withYear = false) {
  const k = new Date(date.getTime() + KST_OFFSET_MS);
  const day = `${pad(k.getUTCMonth() + 1)}/${pad(k.getUTCDate())}`;
  const time = `${pad(k.getUTCHours())}:${pad(k.getUTCMinutes())}`;
  return withYear ? `${k.getUTCFullYear()}-${day.replace('/', '-')} ${time} KST` : `${day} ${time}`;
}

// Argument form of a moment for `/모으기시점`, to the second, in KST.
export function momentArgs(date) {
  const k = new Date(date.getTime() + KST_OFFSET_MS);
  return `${k.getUTCFullYear()}-${pad(k.getUTCMonth() + 1)}-${pad(k.getUTCDate())} ` +
    `${pad(k.getUTCHours())}:${pad(k.getUTCMinutes())}:${pad(k.getUTCSeconds())}`;
}

function when(revision) {
  const time = Date.parse(revision.effectiveFrom);
  if (!Number.isFinite(time)) throw new TypeError('invalid revision time');
  return time;
}

// Latest revision per plan at or before `at`; plans with none did not exist yet.
export function statesAt(revisions, at) {
  const states = new Map();
  for (const revision of revisions) {
    if (when(revision) > at.getTime()) continue;
    const seen = states.get(revision.planKey);
    if (!seen || revision.revisionNo > seen.revisionNo) states.set(revision.planKey, revision);
  }
  return states;
}

// Plans whose amount or ON/OFF differ from `at`. A plan created later goes OFF.
export function diffToMoment(plans, revisions, at) {
  const states = statesAt(revisions, at);
  const changes = [];
  for (const plan of plans) {
    const past = states.get(plan.planKey);
    const target = past ? { dailyAmount: past.dailyAmount, enabled: past.enabled } :
      { dailyAmount: plan.dailyAmount, enabled: false };
    if (compare(decimal(target.dailyAmount), decimal(plan.dailyAmount)) === 0 &&
        target.enabled === plan.enabled) continue;
    changes.push({ plan, target, existed: Boolean(past) });
  }
  return changes.sort((a, b) => a.plan.instrument.ticker.localeCompare(b.plan.instrument.ticker) ||
    a.plan.planKey.localeCompare(b.plan.planKey));
}

// Weekly planned amount in KRW over enabled KRW plans; other currencies are counted apart.
export function weeklyKrw(entries) {
  let total = decimal('0');
  let other = 0;
  for (const { amount, enabled, plan } of entries) {
    if (!enabled) continue;
    if (plan.currency !== 'KRW') { other += 1; continue; }
    total = add(total, multiply(decimal(amount), decimal(String(plan.scheduledWeekdays.length))));
  }
  return { total: exact(total), other };
}

// Binds a confirmation to the moment and the exact plan versions and targets shown.
export async function momentToken(at, changes) {
  const payload = JSON.stringify([at.toISOString(), changes.map(change =>
    [change.plan.planKey, change.plan.version, change.target.dailyAmount, change.target.enabled])]);
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(payload));
  return [...new Uint8Array(digest)].slice(0, 4).map(byte => byte.toString(16).padStart(2, '0'))
    .join('');
}

// Revisions written by one command share their effective time; newest first.
export function changeEvents(revisions, limit = 15) {
  const groups = new Map();
  for (const revision of revisions) {
    const key = revision.effectiveFrom;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(revision);
  }
  return [...groups.entries()].map(([effectiveFrom, members]) => ({
    at: new Date(Date.parse(effectiveFrom)), members,
  })).sort((a, b) => b.at - a.at).slice(0, limit);
}
