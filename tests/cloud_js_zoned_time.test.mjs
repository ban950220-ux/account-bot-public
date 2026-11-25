import assert from 'node:assert/strict';
import { test } from 'node:test';
import { validTimeZone, zonedParts } from '../deploy/cloudflare-js/zoned-time.mjs';
import { dueJobs } from '../deploy/cloudflare-js/boundary.mjs';
import { localTime } from '../deploy/cloudflare-js/assets-report.mjs';

const viaIntl = (date, timeZone) => {
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-US', {
    timeZone, weekday: 'short', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  }).formatToParts(date).map(({ type, value }) => [type, value]));
  return { year: parts.year, month: parts.month, day: parts.day, hour: parts.hour,
    minute: parts.minute, second: parts.second, weekday: parts.weekday };
};

test('Seoul arithmetic matches Intl at day, month, year and leap boundaries', () => {
  const instants = ['2026-11-24T14:59:59Z', '2026-11-24T15:00:00Z', '2026-12-31T14:59:59Z',
    '2026-12-31T15:00:00Z', '2028-02-28T15:00:00Z', '2028-02-29T15:00:00Z',
    '2026-03-08T07:00:00Z', '2026-11-01T06:00:00Z', '1999-12-31T23:59:59.999Z',
    '2026-11-20T23:00:00Z'];
  // Plus one instant every 7h13m over two years to cover every weekday and hour.
  for (let t = Date.parse('2026-01-01T00:00:00Z'); t < Date.parse('2028-01-01T00:00:00Z');
    t += (7 * 60 + 13) * 60 * 1000) instants.push(new Date(t).toISOString());
  for (const iso of instants) {
    const date = new Date(iso);
    assert.deepEqual(zonedParts(date, 'Asia/Seoul'), viaIntl(date, 'Asia/Seoul'), iso);
  }
});

test('other zones keep the Intl path, including daylight saving', () => {
  for (const iso of ['2026-03-08T06:59:00Z', '2026-03-08T07:00:00Z', '2026-11-01T05:59:00Z']) {
    const date = new Date(iso);
    assert.deepEqual(zonedParts(date, 'America/New_York'), viaIntl(date, 'America/New_York'));
  }
  assert.equal(validTimeZone('Asia/Seoul'), true);
  assert.equal(validTimeZone('America/New_York'), true);
  assert.equal(validTimeZone('Not/AZone'), false);
  assert.throws(() => zonedParts(new Date('x'), 'Asia/Seoul'), RangeError);
});

test('scheduled jobs and report time read the same as before', () => {
  assert.deepEqual(dueJobs('2026-11-23T23:00:00Z', 'Asia/Seoul', '08:00', '09:00', 5), ['daily']);
  assert.deepEqual(dueJobs('2026-11-27T23:30:00Z', 'Asia/Seoul', '08:00', '09:00', 5),
    ['weekly_valuation']);
  assert.deepEqual(dueJobs('2026-11-28T00:00:00Z', 'Asia/Seoul', '08:00', '09:00', 5), ['weekly']);
  assert.deepEqual(dueJobs('2026-11-28T23:00:00Z', 'Asia/Seoul', '08:00', '09:00', 5), []);
  assert.deepEqual(dueJobs('2026-11-24T13:00:00Z', 'America/New_York', '08:00', '09:00', 5),
    ['daily']);
  assert.equal(localTime('2026-11-24T23:00:00Z'), '2026-11-25 08:00');
});
