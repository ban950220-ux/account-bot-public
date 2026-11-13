import test from 'node:test';
import assert from 'node:assert/strict';
import { FakeD1 } from './cloud_js_fake_d1.mjs';
import { createManualUploadHandler, latestMeritzRevision,
  manualSnapshotFreshness, normalizeMeritzAccountSnapshot, normalizeMeritzRecurringPlans,
  storeMeritzRevision } from '../deploy/cloudflare-js/manual-input.mjs';

const fixture = { as_of: '2026-02-01T09:00:00Z',
  accounts: [{ account_id: 'local-meritz', base_currency: 'KRW' }],
  positions: [{ account_id: 'local-meritz', market: 'NASDAQ', ticker: 'FIXTURE',
    currency: 'USD', quantity: '0.125', average_price: '80.10',
    market_value: '10.1500', as_of: '2026-01-31T21:00:00Z' }],
  cash: [{ account_id: 'local-meritz', currency: 'KRW', amount: '1000.25' }],
  transactions: [{ account_id: 'local-meritz', transaction_id: 'fixture-fill',
    market: 'NASDAQ', ticker: 'FIXTURE', currency: 'USD', side: 'BUY',
    trade_date: '2026-01-31', quantity: '0.125', price: '80.10',
    gross_amount: '10.0125', fee: '0', tax: '0' }] };
const plans = { as_of: '2026-02-01T09:00:00Z', plans: [{ market: 'NASDAQ',
  ticker: 'FIXTURE', daily_amount: '4000', enabled: true }] };

test('manual snapshots keep source, exact amounts and source time separate from upload time', async () => {
  const db = new FakeD1();
  const normalized = normalizeMeritzAccountSnapshot(fixture);
  assert.equal(normalized.dataAsOf, '2026-01-31T21:00:00.000Z');
  assert.equal(normalized.document.positions[0].quantity, '0.125');
  assert.equal(normalized.document.positions[0].current_price, null);
  const first = await storeMeritzRevision(db, normalized, { expectedRevision: 0,
    mutationKey: 'fixture-1', storedAt: '2026-02-10T00:00:00Z' });
  assert.deepEqual(first, { revision: 1, replay: false });
  assert.deepEqual(await storeMeritzRevision(db, normalized, { expectedRevision: 0,
    mutationKey: 'fixture-1', storedAt: '2026-02-10T00:00:00Z' }),
  { revision: 1, replay: true });
  await assert.rejects(() => storeMeritzRevision(db, normalized, { expectedRevision: 0,
    mutationKey: 'fixture-2', storedAt: '2026-02-10T00:00:00Z' }), /stale/);
  const latest = await latestMeritzRevision(db, 'ACCOUNT_SNAPSHOT');
  assert.equal(latest.dataAsOf, '2026-01-31T21:00:00.000Z');
  assert.equal(latest.storedAt, '2026-02-10T00:00:00.000Z');
  assert.equal(manualSnapshotFreshness(latest, '2026-02-10T00:00:00Z'), 'STALE');
  assert.equal(manualSnapshotFreshness(latest, '2026-02-01T00:00:00Z'), 'OK');
  assert.equal(db.rows('cloud_manual_input_revisions').length, 1);
});

test('recurring snapshot keeps independent append-only history', async () => {
  const db = new FakeD1();
  const first = normalizeMeritzRecurringPlans(plans);
  assert.equal(first.document.plans[0].daily_amount, '4000');
  assert.equal(first.document.plans[0].instrument_currency, 'USD');
  await storeMeritzRevision(db, first, { expectedRevision: 0,
    mutationKey: 'plan-1', storedAt: '2026-02-01T09:01:00Z' });
  const changed = normalizeMeritzRecurringPlans({ ...plans,
    plans: [{ ...plans.plans[0], daily_amount: '5000', enabled: false }] });
  await storeMeritzRevision(db, changed, { expectedRevision: 1,
    mutationKey: 'plan-2', storedAt: '2026-02-01T09:02:00Z' });
  assert.equal(db.rows('cloud_manual_input_revisions').length, 2);
  assert.equal((await latestMeritzRevision(db, 'RECURRING_PLANS')).document.plans[0].enabled,
    false);
});

test('invalid and cross-account data are rejected before storage', () => {
  assert.throws(() => normalizeMeritzAccountSnapshot({ ...fixture, cash: [] ,
    positions: [{ ...fixture.positions[0], account_id: 'another' }] }), /row account/);
  assert.throws(() => normalizeMeritzAccountSnapshot({ ...fixture, accounts: [
    { account_id: '12345678' }] }), /local account alias/);
  assert.throws(() => normalizeMeritzAccountSnapshot({ ...fixture,
    positions: [{ ...fixture.positions[0], quantity: null }] }), /required amount/);
  assert.throws(() => normalizeMeritzAccountSnapshot({ ...fixture,
    extra: { api_secret: 'bad' } }), /sensitive/);
  assert.throws(() => normalizeMeritzRecurringPlans({ ...plans,
    plans: [{ ...plans.plans[0], enabled: 'false' }] }), /boolean/);
  assert.throws(() => normalizeMeritzRecurringPlans({ ...plans,
    plans: [plans.plans[0], plans.plans[0]] }), /duplicate/);
});

test('upload boundary denies before D1, then accepts only valid owner data', async () => {
  const db = new FakeD1();
  let allowed = false;
  const handler = createManualUploadHandler({ db, authorizeOwner: async () => allowed,
    now: () => '2026-02-10T00:00:00Z' });
  const body = JSON.stringify({ kind: 'ACCOUNT_SNAPSHOT', document: fixture,
    expectedRevision: 0, mutationKey: 'owner-upload-1' });
  const request = () => new Request('https://fixture.invalid/owner/manual-input', {
    method: 'POST', headers: { 'content-type': 'application/json' }, body });
  assert.equal((await handler(request())).status, 403);
  assert.equal(db.rows('cloud_manual_input_revisions').length, 0);
  allowed = true;
  const response = await handler(request());
  assert.equal(response.status, 200);
  assert.equal((await response.json()).revision, 1);
  assert.equal((await handler(request())).status, 200);
  assert.equal(db.rows('cloud_manual_input_revisions').length, 1);
  assert.equal((await handler(new Request('https://fixture.invalid/owner/manual-input'))).status,
    404);
});


test('omitted manual cash/history remain unknown; explicit empty lists remain observed empty',async()=>{
 const {createOperatingGateway}=await import('../deploy/cloudflare-js/operating-gateway.mjs');
 const {refreshWithProviders}=await import('../deploy/cloudflare-js/provider-contracts.mjs');
 const missing={as_of:'2026-10-30T00:00:00Z',accounts:[{account_id:'manual-meritz',base_currency:'KRW'}],positions:[]};
 const raw=JSON.stringify(missing),normalized=normalizeMeritzAccountSnapshot(missing);
 assert.equal(JSON.stringify(missing),raw);
 assert.equal(Object.hasOwn(normalized.document,'cash'),false);assert.equal(Object.hasOwn(normalized.document,'transactions'),false);
 assert.throws(()=>normalizeMeritzAccountSnapshot({...missing,cash:null}),/cash list required/);
 const db=new FakeD1();
 try{
  await storeMeritzRevision(db,normalized,{expectedRevision:0,mutationKey:'missing-1',storedAt:missing.as_of});
  const gateway=await createOperatingGateway({DB:db,KIWOOM_ACCUM_FINGERPRINT:'fixture_'+'a'.repeat(64),REPORT_TIMEZONE:'Asia/Seoul'},{now:()=>missing.as_of});
  let result=await refreshWithProviders(gateway.providers,['meritz']);
  assert.equal(result.cashResults[0].status,'unavailable');assert.equal(result.cashResults[0].safeErrorType,'MissingManualSection');
  assert.equal(result.snapshot.isComplete,false);assert.equal(result.snapshot.totalMarketValue,null);
  const empty=normalizeMeritzAccountSnapshot({...missing,cash:[],transactions:[]});
  assert.deepEqual(empty.document.cash,[]);assert.deepEqual(empty.document.transactions,[]);
  await storeMeritzRevision(db,empty,{expectedRevision:1,mutationKey:'explicit-empty-2',storedAt:missing.as_of});
  result=await refreshWithProviders(gateway.providers,['meritz']);
  assert.equal(result.cashResults[0].status,'ok');assert.equal(result.snapshot.isComplete,true);assert.equal(result.snapshot.totalMarketValue,'0');
 }finally{db.sqlite.close();}
});
