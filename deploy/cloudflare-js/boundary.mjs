// Pure Worker boundaries. No live endpoint, credentials, or network calls.
const HEADER = 'x-telegram-bot-api-secret-token';
const encoder = new TextEncoder();
const decoder = new TextDecoder('utf-8', { fatal: true });
const WEEKDAYS = new Map([['Mon', 0], ['Tue', 1], ['Wed', 2], ['Thu', 3],
  ['Fri', 4], ['Sat', 5], ['Sun', 6]]);

function sameSecret(a, b) {
  const left = encoder.encode(a);
  const right = encoder.encode(b);
  let difference = left.length ^ right.length;
  for (let index = 0; index < Math.max(left.length, right.length); index++) {
    difference |= (left[index] || 0) ^ (right[index] || 0);
  }
  return difference === 0;
}

function positiveId(value) {
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}

export function admittedUpdateId(headerEntries, body, expectedSecret, allowedUserIds) {
  if (!expectedSecret || !allowedUserIds.length || body.byteLength > 64 * 1024) return null;
  const secrets = headerEntries.filter(([name]) => name.toLowerCase() === HEADER);
  if (secrets.length !== 1 || !sameSecret(secrets[0][1], expectedSecret)) return null;
  let update;
  try { update = JSON.parse(decoder.decode(body)); } catch { return null; }
  if (update === null || typeof update !== 'object' || Array.isArray(update)) return null;
  const updateId = positiveId(update.update_id);
  if (updateId === null) return null;
  const message = update.message || update.callback_query?.message;
  const user = update.message?.from || update.callback_query?.from;
  const userId = positiveId(user?.id);
  if (userId === null || !allowedUserIds.includes(userId) || message?.chat?.type !== 'private' ||
      positiveId(message.chat.id) !== userId) return null;
  return updateId;
}

export function dueJobs(utcIso, timezoneName, dailyTime, weeklyTime, weeklyWeekday) {
  if (!Number.isInteger(weeklyWeekday) || weeklyWeekday < 0 || weeklyWeekday > 6 ||
      !/Z$|[+-]\d\d:\d\d$/.test(utcIso)) throw new TypeError('aware timestamp required');
  const at = new Date(utcIso);
  if (Number.isNaN(at.getTime())) throw new TypeError('invalid timestamp');
  // Cron has minute precision. Round only the controller's timestamp down;
  // never substitute invocation/arrival time or round into the next minute.
  at.setUTCSeconds(0, 0);
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-US', {
    timeZone: timezoneName, weekday: 'short', hour: '2-digit', minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(at).map(({ type, value }) => [type, value]));
  const weekday = WEEKDAYS.get(parts.weekday);
  const clock = `${parts.hour}:${parts.minute}:${parts.second}`;
  const normalizeTime = value => {
    if (!/^\d{2}:\d{2}(?::\d{2})?$/.test(value)) throw new TypeError('invalid local time');
    return value.length === 5 ? `${value}:00` : value;
  };
  const jobs = [];
  if (weekday < 5 && clock === normalizeTime(dailyTime)) jobs.push('daily');
  if (weekday === 5 && clock === '08:30:00') jobs.push('weekly_valuation');
  if (weekday === weeklyWeekday && clock === normalizeTime(weeklyTime)) jobs.push('weekly');
  return jobs;
}
