// Calendar fields of an instant in a time zone. Asia/Seoul is UTC+9 with no
// daylight saving, so it is computed arithmetically: the first Intl.DateTimeFormat
// with a timeZone costs several ms of CPU per Worker instance, which matters
// under the Workers Free 10ms CPU limit. Other zones keep the Intl path.
const SEOUL = 'Asia/Seoul';
const SEOUL_OFFSET_MS = 9 * 60 * 60 * 1000;
const WEEKDAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const pad = value => String(value).padStart(2, '0');

// { year, month, day, hour, minute, second } as zero-padded strings and weekday as
// 'Mon'..'Sun', the same values Intl en-US/en-CA formatToParts gives with h23.
export function zonedParts(date, timeZone) {
  if (Number.isNaN(date.getTime())) throw new RangeError('invalid date');
  if (timeZone === SEOUL) {
    const k = new Date(date.getTime() + SEOUL_OFFSET_MS);
    return { year: String(k.getUTCFullYear()), month: pad(k.getUTCMonth() + 1),
      day: pad(k.getUTCDate()), hour: pad(k.getUTCHours()), minute: pad(k.getUTCMinutes()),
      second: pad(k.getUTCSeconds()), weekday: WEEKDAY_NAMES[k.getUTCDay()] };
  }
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-US', {
    timeZone, weekday: 'short', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  }).formatToParts(date).map(({ type, value }) => [type, value]));
  return { year: parts.year, month: parts.month, day: parts.day, hour: parts.hour,
    minute: parts.minute, second: parts.second, weekday: parts.weekday };
}

export function validTimeZone(timeZone) {
  if (timeZone === SEOUL) return true;
  try { new Intl.DateTimeFormat('en-US', { timeZone }); return true; } catch { return false; }
}
