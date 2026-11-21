import assert from 'node:assert/strict';
import { test } from 'node:test';
import { FakeD1 } from './cloud_js_fake_d1.mjs';
import { importAccount } from '../deploy/cloudflare-js/storage.mjs';
import { SyntheticCloudGateway } from '../deploy/cloudflare-js/cloud-gateway.mjs';
import { createPlanOnce, listCurrentPlans } from '../deploy/cloudflare-js/cloud-write.mjs';
import { routeUpdate } from '../deploy/cloudflare-js/command-router.mjs';

const account = `fixture_${'c'.repeat(64)}`;
const secret = 'synthetic-secret';
const headers = [['X-Telegram-Bot-Api-Secret-Token', secret]];
const body = (id, text) => new TextEncoder().encode(JSON.stringify({ update_id: id,
  message: { from: { id: 42 }, chat: { id: 42, type: 'private' }, text } }));

// Two ROK plans reproduce the production duplicate; NVDA is a single plan.
async function operating({ dropArchive = false } = {}) {
  const db = new FakeD1();
  if (dropArchive) db.sqlite.exec('DROP TABLE cloud_plan_archive_events');
  await importAccount(db, { fingerprint: account, broker: 'synthetic', baseCurrency: 'KRW' });
  const state = (ticker, assetType, dailyAmount, memo) => ({ instrument: { market: 'US', ticker,
    assetType, currency: 'USD' }, accountFingerprint: account, source: 'TELEGRAM',
  amountCurrency: 'KRW', dailyAmount, targetWeight: null, enabled: true,
  timezoneName: 'Asia/Seoul', startedAt: '2026-02-01T00:00:00Z', memo });
  // The candidate list marks its plans in the memo, as in production.
  for (const [key, ticker, amount, memo] of [
    ['user-candidate:US:ROK', 'ROK', '2000', '[source=USER_CANDIDATE]'],
    ['kiwoom-photo:US:ROK', 'ROK', '2000', null], ['fixture:NVDA', 'NVDA', '4000', null]]) {
    await createPlanOnce(db, key, `seed:${key}`, state(ticker, 'EQUITY', amount, memo),
      '2026-02-01T00:00:00Z');
  }
  const gateway = new SyntheticCloudGateway({ db, providers: [],
    scopeAccountFingerprint: account, externalRecurringPlans: async () => [],
    now: () => '2026-02-02T01:00:00Z' });
  const route = (id, command, overrides = {}) => routeUpdate({ headerEntries: headers,
    body: body(id, command), secret, allowedUserIds: [42], gateway, ...overrides });
  const plan = key => db.rows('cloud_plans').find(row => row.plan_key === key);
  const events = () => db.sqlite.prepare(
    'SELECT * FROM cloud_plan_archive_events ORDER BY event_no').all();
  return { db, route, plan, events };
}

test('archiving a duplicate hides it everywhere and turns it OFF', async () => {
  const { db, route, plan, events } = await operating();
  // Before: the ticker is ambiguous.
  assert.match((await route(1, '/모으기설정 ROK OFF')).reply, /같은 종목의 계획이 여러 개/);
  assert.match((await route(2, '/모으기삭제 ROK')).reply,
    /변경된 것은 없습니다[\s\S]*\/모으기삭제 kiwoom-photo:US:ROK[\s\S]*\/모으기삭제 user-candidate:US:ROK/);
  assert.equal(events().length, 0);

  const archived = await route(3, '/모으기삭제 user-candidate:US:ROK');
  assert.equal(archived.status, 'READY');
  assert.match(archived.reply, /US:ROK<\/b> 계획을 삭제\(보관\)했습니다/);
  assert.match(archived.reply, /2,000원 · OFF/);
  assert.match(archived.reply, /\/모으기복원 user-candidate:US:ROK/);
  assert.equal(plan('user-candidate:US:ROK').enabled, 0);
  assert.equal(plan('user-candidate:US:ROK').version, 2);
  assert.deepEqual(events().map(row => [row.plan_key, row.action]),
    [['user-candidate:US:ROK', 'ARCHIVE']]);
  // The row and its history remain.
  assert.equal(db.rows('cloud_plan_revisions')
    .filter(row => row.plan_key === 'user-candidate:US:ROK').length, 2);

  // Hidden from listings, the recurring report and ticker lookup.
  assert.deepEqual((await listCurrentPlans(db)).map(row => row.planKey),
    ['fixture:NVDA', 'kiwoom-photo:US:ROK']);
  assert.ok(!(await route(4, '/모으기')).reply.includes('user-candidate'));
  assert.match((await route(5, '/모으기설정 ROK OFF')).reply, /US:ROK<\/b> 2,000원 · OFF/);
  assert.equal(plan('kiwoom-photo:US:ROK').enabled, 0);
  assert.match((await route(6, '/모으기설정\nROK 3000\nNVDA 2000')).reply, /모으기 2종목 변경/);
  assert.equal(plan('user-candidate:US:ROK').daily_amount, '2000');

  // Replaying the same update re-renders without a second event; archiving again is a no-op.
  assert.match((await route(3, '/모으기삭제 user-candidate:US:ROK')).reply, /삭제\(보관\)했습니다/);
  assert.match((await route(7, '/모으기삭제 user-candidate:US:ROK')).reply, /찾지 못했습니다/);
  assert.equal(events().length, 1);
  assert.equal(plan('user-candidate:US:ROK').version, 2);
});

test('restoring brings the plan back OFF and can be archived again', async () => {
  const { db, route, plan, events } = await operating();
  await route(10, '/모으기삭제 user-candidate:US:ROK');
  assert.match((await route(11, '/모으기복원 NVDA')).reply, /보관된 NVDA 계획이 없습니다/);
  const restored = await route(12, '/모으기복원 ROK');
  assert.match(restored.reply, /US:ROK<\/b> 계획을 복원했습니다/);
  assert.match(restored.reply,
    /다시 모으려면: <code>\/모으기설정 user-candidate:US:ROK 2000 KRW ON 2<\/code>/);
  assert.equal(plan('user-candidate:US:ROK').enabled, 0);
  assert.equal(plan('user-candidate:US:ROK').version, 2);
  assert.equal((await listCurrentPlans(db)).length, 3);
  assert.match((await route(12, '/모으기복원 ROK')).reply, /복원했습니다/);
  assert.equal(events().length, 2);
  // Already OFF: archiving again only records the event, no plan revision.
  assert.match((await route(13, '/모으기삭제 user-candidate:US:ROK')).reply, /삭제\(보관\)했습니다/);
  assert.equal(plan('user-candidate:US:ROK').version, 2);
  assert.deepEqual(events().map(row => row.action),
    ['ARCHIVE', 'RESTORE', 'ARCHIVE']);
  // A plain /모으기추가 cannot silently revive or shadow an archived plan.
  assert.equal((await route(14, '/모으기추가 US:ROK 2000KRW 5%')).status, 'ERROR');
});

test('archive input is validated and an archived plan cannot be edited by key', async () => {
  const { route } = await operating();
  for (const text of ['/모으기삭제', '/모으기삭제 A B', '/모으기삭제 <b>']) {
    assert.match((await route(20, text)).reply, /사용법/);
  }
  assert.match((await route(21, '/모으기삭제 ZZZZ')).reply, /찾지 못했습니다/);
  await route(22, '/모으기삭제 user-candidate:US:ROK');
  assert.equal((await route(23, '/recurring_set user-candidate:US:ROK 3000 KRW ON 2')).status,
    'ERROR');
});

test('without the archive table everything works as before', async () => {
  const { db, route } = await operating({ dropArchive: true });
  assert.equal((await listCurrentPlans(db)).length, 3);
  assert.match((await route(30, '/모으기삭제 user-candidate:US:ROK')).reply,
    /아직 운영 DB에 설치되지 않았습니다/);
  assert.match((await route(31, '/모으기설정 NVDA 3000')).reply, /US:NVDA<\/b> 3,000원/);
  assert.match((await route(32, '/모으기')).reply, /US:ROK/);
});

test('archive events obey the edit fence and are immutable', async () => {
  const { db, route, plan, events } = await operating();
  await route(40, '/모으기삭제 user-candidate:US:ROK');
  assert.throws(() => db.sqlite.exec(`UPDATE cloud_plan_archive_events SET action='RESTORE'`),
    /immutable/);
  assert.throws(() => db.sqlite.exec('DELETE FROM cloud_plan_archive_events'), /immutable/);
  db.sqlite.exec('PRAGMA foreign_keys = OFF');
  db.sqlite.exec(`INSERT INTO cloud_cutover (singleton, run_id, epoch, state, owner_user_id,
    target_id, source_sha256, report_timezone, portfolio_id, edits_enabled)
    VALUES (1, 'run', 1, 'ACTIVE', '42', 't', 'x', 'Asia/Seoul', 1, 0)`);
  db.sqlite.exec('PRAGMA foreign_keys = ON');
  const before = db.rows('cloud_plan_revisions').length;
  assert.equal((await route(41, '/모으기삭제 kiwoom-photo:US:ROK')).status, 'ERROR');
  assert.equal(plan('kiwoom-photo:US:ROK').enabled, 1);
  assert.equal(db.rows('cloud_plan_revisions').length, before);
  assert.equal(events().length, 1);
});

test('atomic delivery stages the archive reply with its event', async () => {
  const { db, route } = await operating();
  const result = await route(50, '/모으기삭제 user-candidate:US:ROK',
    { atomicDelivery: true, now: () => '2026-02-02T01:00:00Z' });
  assert.equal(result.status, 'READY');
  assert.equal(result.mutationAttempted, true);
  assert.equal(db.rows('cloud_outbox').length, 1);
  assert.match(JSON.parse(db.rows('cloud_outbox')[0].payload_json).text,
    /삭제\(보관\)했습니다[\s\S]*2,000원 · OFF/);
});
