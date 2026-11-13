import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtemp, rm, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { FakeD1 } from './cloud_js_fake_d1.mjs';
import { beginD1Archive,stageD1ArchiveChunk,finalizeD1Archive } from '../deploy/cloudflare-js/d1-archive-import.mjs';
import { beginCoreImport,stageCoreChunk,finalizeCoreImport,reconcileCore,sha } from '../deploy/cloudflare-js/d1-core-import.mjs';
import { activateCutover,pauseCutover,requireActiveCutover } from '../deploy/cloudflare-js/cutover-state.mjs';
import { createOperatingGateway } from '../deploy/cloudflare-js/operating-gateway.mjs';
import { routeUpdate } from '../deploy/cloudflare-js/command-router.mjs';
import defaultWorker,{ createOperatingWorker } from '../deploy/cloudflare-js/operating-worker.mjs';

const root=fileURLToPath(new URL('../',import.meta.url));
const now='2026-10-29T00:00:00Z';
function py(args) {
  const result=spawnSync('python',['-B',...args],{cwd:root,env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'},encoding:'utf8'});
  assert.equal(result.status,0,result.stderr||result.stdout);return result.stdout;
}
async function fixture(t,manual=false,ended=false,portfolioNull=false,missingManual=false,reportMatrix=false) {
  const path=await mkdtemp(join(tmpdir(),'core-cutover-fixture-'));t.after(()=>rm(path,{recursive:true,force:true}));
  const {archive,core}=JSON.parse(py(['tests/cloud_bundle_fixture.py',path,...(manual?['--manual']:[]),...(ended?['--ended']:[]),...(portfolioNull?['--portfolio-null']:[]),...(missingManual?['--manual-missing-sections']:[]),...(reportMatrix?['--report-case-matrix']:[])]));
  const db=new FakeD1();t.after(()=>db.sqlite.close());
  await beginD1Archive(db,archive.manifest);
  for(const c of archive.chunks)await stageD1ArchiveChunk(db,archive.manifest,c);
  await finalizeD1Archive(db,archive.manifest);
  return {db,core,archive,path};
}
async function imported(t) {
  const f=await fixture(t);await beginCoreImport(f.db,f.core.manifest,f.core.metadata);
  for(const chunk of f.core.chunks)await stageCoreChunk(f.db,f.core.manifest,chunk);
  await finalizeCoreImport(f.db,f.core.manifest);return f;
}
function evidence(core) {return {runId:core.manifest.runId,sourceSha:core.manifest.sourceLogicalSha,
  targetId:core.metadata.scope.targetId,ownerUserId:'42',observedAt:now,reportsAfter:'2026-10-29T00:01:00Z',
  pcStopped:true,pcStopEvidenceSha:'1'.repeat(64),backupReadbackSha:core.manifest.backupSha,rollbackRehearsalSha:'3'.repeat(64),deliveryStatesReconciled:true};}
const envFor=({db,core})=>({DB:db,CLOUD_TARGET_ID:core.metadata.scope.targetId,CLOUD_PORTFOLIO_ID:String(core.metadata.scope.portfolioId),KIWOOM_ACCUM_FINGERPRINT:core.metadata.scope.recurringAccountFingerprint,
  TELEGRAM_BOT_TOKEN:'123456:'+ 'x'.repeat(30),TELEGRAM_WEBHOOK_SECRET:'fixture-secret',TELEGRAM_ALLOWED_USER_IDS:'42',TELEGRAM_CHAT_ID:'42',REPORT_TIMEZONE:'Asia/Seoul',DAILY_REPORT_TIME:'10:00',WEEKLY_REPORT_TIME:'18:00',WEEKLY_REPORT_WEEKDAY:'5'});
const request=(id,command,user=42)=>new Request('https://fixture.invalid/telegram/webhook',{method:'POST',headers:{'x-telegram-bot-api-secret-token':'fixture-secret'},body:JSON.stringify({update_id:id,message:{from:{id:user},chat:{id:user,type:'private'},text:command}})});

test('full 29-table archive plus active projection verifies without activating old UNKNOWN',async t=>{
 const f=await imported(t);assert.equal(f.archive.manifest.tableCount,29);
 assert.equal(await reconcileCore(f.db,f.core.manifest),true);
 assert.equal(f.db.rows('cloud_plans').length,1);assert.equal(f.db.rows('cloud_outbox').length,0);
 assert.equal(f.db.sqlite.prepare('SELECT state FROM cloud_cutover').get().state,'VERIFIED');
 assert.throws(()=>f.db.sqlite.prepare("UPDATE cloud_plans SET daily_amount='9999'").run(),/fence/);
 assert.equal((await defaultWorker.fetch(request(1,'/recurring'),envFor(f))).status,503);
});
test('manifest, metadata, chunk order and changed replay all fail closed',async t=>{
 const f=await fixture(t);await beginCoreImport(f.db,f.core.manifest,f.core.metadata);
 await assert.rejects(()=>beginCoreImport(f.db,{...f.core.manifest,backupSha:'0'.repeat(64)},f.core.metadata),/manifest/);
 await assert.rejects(()=>stageCoreChunk(f.db,{...f.core.manifest,rowsSha:'0'.repeat(64)},f.core.chunks[0]),/manifest/);
 await assert.rejects(()=>beginCoreImport(f.db,f.core.manifest,{...f.core.metadata,scope:{...f.core.metadata.scope,ownerUserId:'99'}}),/manifest|scope/);
 await stageCoreChunk(f.db,f.core.manifest,f.core.chunks[0]);
 assert.equal(await stageCoreChunk(f.db,f.core.manifest,f.core.chunks[0]),'REPLAY');
 const changed=structuredClone(f.core.chunks[0]);changed.items[0].values[1]='tampered';changed.sha=await sha(changed.items);
 await assert.rejects(()=>stageCoreChunk(f.db,f.core.manifest,changed),/replay/);
});
test('late chunk failure is atomic; final reconciliation catches unauthorized row changes',async t=>{
 const f=await fixture(t);await beginCoreImport(f.db,f.core.manifest,f.core.metadata);
 f.db.sqlite.exec("CREATE TRIGGER fixture_fail BEFORE INSERT ON cloud_core_import_chunks BEGIN SELECT RAISE(ABORT,'fixture failure'); END");
 await assert.rejects(()=>stageCoreChunk(f.db,f.core.manifest,f.core.chunks[0]),/fixture failure/);
 assert.equal(f.db.rows('cloud_accounts').length,0);
 f.db.sqlite.exec('DROP TRIGGER fixture_fail');for(const c of f.core.chunks)await stageCoreChunk(f.db,f.core.manifest,c);
 f.db.sqlite.prepare("UPDATE cloud_plans SET daily_amount='3000'").run();
 await assert.rejects(()=>finalizeCoreImport(f.db,f.core.manifest),/reconciliation/);
});
test('activation requires all proof fields; actual target and owner cannot be substituted',async t=>{
 const f=await imported(t);
 for(const field of ['pcStopped','pcStopEvidenceSha','backupReadbackSha','rollbackRehearsalSha','deliveryStatesReconciled']){
  const proof=evidence(f.core);delete proof[field];await assert.rejects(()=>activateCutover(f.db,{expectedEpoch:1,proof},now),/proof/);
 }
 await activateCutover(f.db,{expectedEpoch:1,proof:evidence(f.core),edits:true,sends:true},now);
 const env=envFor(f),config={chat:'42',users:[42]};await requireActiveCutover(env,config);
 await assert.rejects(()=>requireActiveCutover({...env,CLOUD_TARGET_ID:'other'},config),/fence/);
 await assert.rejects(()=>requireActiveCutover(env,{chat:'43',users:[43]}),/fence/);
 await assert.rejects(()=>requireActiveCutover(env,config,{scheduledAt:now}),/fence/);
 assert.deepEqual(await pauseCutover(f.db,2,'fixture rollback'),{epoch:3,state:'PAUSED',drained:false});
 assert.throws(()=>f.db.sqlite.prepare("UPDATE cloud_plans SET daily_amount='4000'").run(),/fence/);
});
test('owner manual recurring edit needs no broker credential, is versioned, scoped and replay-safe',async t=>{
 const f=await imported(t);await activateCutover(f.db,{expectedEpoch:1,proof:evidence(f.core),edits:true,sends:true},now);
 const env=envFor(f);let sends=0;
 const worker=createOperatingWorker({requireCutover:true,gatewayFactory:e=>createOperatingGateway(e,{now:()=>now,fetcher:()=>assert.fail('no broker API')}),fetcher:async()=>{sends++;return {ok:true,json:async()=>({ok:true,result:{message_id:sends}})};},now:()=>now});
 assert.equal((await worker.fetch(request(50,'/recurring_set fixture:US:TEST 3000 KRW ON 1',43),env)).status,403);
 assert.equal((await worker.fetch(request(51,'/recurring_set fixture:US:TEST 3000 KRW ON 1'),env)).status,200);
 assert.equal(f.db.rows('cloud_plans')[0].daily_amount,'3000');assert.equal(f.db.rows('cloud_plan_revisions').length,2);
 assert.equal((await worker.fetch(request(51,'/recurring_set fixture:US:TEST 3000 KRW ON 1'),env)).status,200);assert.equal(sends,1);
 assert.equal((await worker.fetch(request(52,'/recurring_set fixture:US:TEST 4000 KRW ON 1'),env)).status,503);
 assert.equal((await worker.fetch(request(53,'/recurring_set fixture:US:TEST 10 USD ON 2'),env)).status,503);
 assert.equal(f.db.rows('cloud_plans')[0].daily_amount,'3000');
 await pauseCutover(f.db,2,'fixture freeze');
 assert.equal((await worker.fetch(request(54,'/recurring_set fixture:US:TEST 4000 KRW ON 2'),env)).status,503);
});
test('reverse reconciliation preserves new plan revision and old archive-only data; polling remains held',async t=>{
 const f=await imported(t);await activateCutover(f.db,{expectedEpoch:1,proof:evidence(f.core),edits:true,sends:true},now);
 const gateway=await createOperatingGateway(envFor(f),{now:()=>now});
 await gateway.editPlan({planKey:'fixture:US:TEST',expectedVersion:1,amount:'3000',currency:'KRW',enabled:false,mutationKey:'fixture-edit'});
 await pauseCutover(f.db,2,'fixture rollback');
 f.db.sqlite.prepare('VACUUM INTO ?').run(join(f.path,'cloud.db'));
 const output=py(['-c',`import runpy,json,sys,sqlite3;from pathlib import Path;sys.path.insert(0,'src');m=runpy.run_path('scripts/cloud/rollback_reconcile.py');r=Path(sys.argv[1]);result=m['reconcile_rollback'](r/'backup.db',json.loads((r/'core.json').read_text()),r/'cloud.db',r/'restored');db=sqlite3.connect(r/'restored/restored.sqlite3');assert db.execute('SELECT daily_amount,enabled,version FROM recurring_buy_plans').fetchone()==(3000,0,2);assert db.execute('SELECT status FROM report_deliveries').fetchone()==('UNKNOWN',);assert db.execute('SELECT configured_amount_krw FROM saved_scenario_plans').fetchone()==('2000.000000000000000001',);from account_bot.polling_guard import require_rollback_clear
try:require_rollback_clear(r/'restored/restored.sqlite3')
except RuntimeError:pass
else:raise AssertionError('startup not held')
print(json.dumps(result))`,f.path]);
 const result=JSON.parse(output);assert.equal(result.newPlanRevisions,1);assert.equal(result.startup,'HELD');
 assert.ok((await readFile(join(f.path,'restored/cloud-state.json'),'utf8')).includes('fixture-edit'));
});

test('initial Meritz documents join the verified import with exact revision hashes',async t=>{
 const f=await fixture(t,true);await beginCoreImport(f.db,f.core.manifest,f.core.metadata);
 for(const c of f.core.chunks)await stageCoreChunk(f.db,f.core.manifest,c);
 await finalizeCoreImport(f.db,f.core.manifest);
 const docs=f.db.rows('cloud_manual_input_revisions');assert.equal(docs.length,2);
 const gateway=await createOperatingGateway(envFor(f),{now:()=>now});
 assert.ok((await gateway.listPlans(true)).some(p=>p.broker==='meritz'&&p.dailyAmount==='1000'));
 await activateCutover(f.db,{expectedEpoch:1,proof:evidence(f.core),edits:true,sends:true},now);
 await pauseCutover(f.db,2,'fixture rollback');f.db.sqlite.prepare('VACUUM INTO ?').run(join(f.path,'cloud.db'));
 py(['-c',`import runpy,json,sys;from pathlib import Path;m=runpy.run_path('scripts/cloud/rollback_reconcile.py');r=Path(sys.argv[1]);m['reconcile_rollback'](r/'backup.db',json.loads((r/'core.json').read_text()),r/'cloud.db',r/'restored')`,f.path]);
 assert.equal(JSON.parse(await readFile(join(f.path,'restored/meritz-recurring.json'),'utf8')).plans[0].daily_amount,'1000');
});
test('reverse refuses in-flight sends and never leaves a misleading partial restore',async t=>{
 const f=await imported(t);await activateCutover(f.db,{expectedEpoch:1,proof:evidence(f.core),edits:true,sends:true},now);
 f.db.sqlite.prepare("INSERT INTO cloud_outbox VALUES ('fixture','42','{}','SENDING',?,NULL)").run(now);
 await pauseCutover(f.db,2,'fixture rollback');f.db.sqlite.prepare('VACUUM INTO ?').run(join(f.path,'cloud.db'));
 py(['-c',`import runpy,json,sys;from pathlib import Path;m=runpy.run_path('scripts/cloud/rollback_reconcile.py');r=Path(sys.argv[1])
try:m['reconcile_rollback'](r/'backup.db',json.loads((r/'core.json').read_text()),r/'cloud.db',r/'restored')
except m['Blocked'] as e:assert 'in-flight' in str(e)
else:raise AssertionError('ambiguous send accepted')
assert not (r/'restored').exists();assert not (r/'restored.partial').exists()`,f.path]);
});
test('paused fence wins after an owner operation has read an old version',async t=>{
 const f=await imported(t);await activateCutover(f.db,{expectedEpoch:1,proof:evidence(f.core),edits:true,sends:true},now);
 const gateway=await createOperatingGateway(envFor(f),{now:()=>now});
 let paused=false;const batch=f.db.batch.bind(f.db);
 f.db.batch=async statements=>{if(!paused){paused=true;await pauseCutover({...f.db,batch,prepare:f.db.prepare.bind(f.db)},2,'fixture race');}return batch(statements);};
 await assert.rejects(()=>gateway.editPlan({planKey:'fixture:US:TEST',expectedVersion:1,amount:'3000',currency:'KRW',enabled:true,mutationKey:'racing'}),/fence/);
 assert.equal(f.db.rows('cloud_plans')[0].daily_amount,'2000');assert.equal(f.db.rows('cloud_plan_revisions').length,1);
});

test('explicit 1,500/1,000 KRW amounts and OFF preserve the owner plan, not broker limits',async t=>{
 const f=await imported(t);await activateCutover(f.db,{expectedEpoch:1,proof:evidence(f.core),edits:true,sends:true},now);
 const gateway=await createOperatingGateway(envFor(f),{now:()=>now});
 await gateway.editPlan({planKey:'fixture:US:TEST',expectedVersion:1,amount:'1500',currency:'KRW',enabled:true,mutationKey:'budget1500'});
 await gateway.editPlan({planKey:'fixture:US:TEST',expectedVersion:2,amount:'1000',currency:'KRW',enabled:false,mutationKey:'budget1000'});
 assert.equal(f.db.rows('cloud_plans')[0].daily_amount,'1000');assert.equal(f.db.rows('cloud_plans')[0].enabled,0);
});
test('legacy UNKNOWN period cannot be sent again later on the same day or claimed directly',async t=>{
 const f=await imported(t);await activateCutover(f.db,{expectedEpoch:1,proof:evidence(f.core),edits:true,sends:true},now);
 let sends=0;const worker=createOperatingWorker({requireCutover:true,gatewayFactory:e=>createOperatingGateway(e),fetcher:async()=>{sends++;throw Error('should not send');},now:()=>now});
 await worker.scheduled({scheduledTime:Date.parse('2026-10-29T01:00:00Z')},envFor(f));
 assert.equal(sends,0);assert.equal(f.db.rows('cloud_report_claims').length,0);
 assert.throws(()=>f.db.sqlite.prepare("INSERT INTO cloud_report_claims VALUES ('daily:20261029',?,'fixture')").run(now),/suppressed/);
});
test('ended disabled plan remains read only and cannot be resurrected by owner ON',async t=>{
 const f=await fixture(t,false,true);await beginCoreImport(f.db,f.core.manifest,f.core.metadata);
 for(const c of f.core.chunks)await stageCoreChunk(f.db,f.core.manifest,c);
 await finalizeCoreImport(f.db,f.core.manifest);await activateCutover(f.db,{expectedEpoch:1,proof:evidence(f.core),edits:true,sends:true},now);
 const gateway=await createOperatingGateway(envFor(f),{now:()=>now});
 await assert.rejects(()=>gateway.editPlan({planKey:'fixture:US:TEST',expectedVersion:1,amount:'2000',currency:'KRW',enabled:true,mutationKey:'ended-on'}),/read only/);
 assert.equal(f.db.rows('cloud_plans')[0].enabled,0);
 assert.equal(f.db.sqlite.prepare('SELECT ended_at FROM cloud_plan_lifecycle').get().ended_at,'2026-10-28T00:00:00Z');
});
test('reverse rejects precision loss in an intermediate new revision even if final head is safe',async t=>{
 const f=await imported(t);await activateCutover(f.db,{expectedEpoch:1,proof:evidence(f.core),edits:true,sends:true},now);
 const {applyPlanChange,listCurrentPlans}=await import('../deploy/cloudflare-js/cloud-write.mjs');
 let row=(await listCurrentPlans(f.db))[0];await applyPlanChange(f.db,row.planKey,1,'fractional-history',{...row,targetWeight:'0.1234567890123456789'},now);
 row=(await listCurrentPlans(f.db))[0];await applyPlanChange(f.db,row.planKey,2,'safe-head',{...row,targetWeight:null},now);
 await pauseCutover(f.db,2,'fixture rollback');f.db.sqlite.prepare('VACUUM INTO ?').run(join(f.path,'cloud.db'));
 py(['-c',`import runpy,json,sys;from pathlib import Path;m=runpy.run_path('scripts/cloud/rollback_reconcile.py');r=Path(sys.argv[1])
try:m['reconcile_rollback'](r/'backup.db',json.loads((r/'core.json').read_text()),r/'cloud.db',r/'restored')
except m['Blocked'] as e:assert 'cannot preserve' in str(e),str(e)
else:raise AssertionError('rounded history accepted')
assert not (r/'restored').exists();assert not (r/'restored.partial').exists()`,f.path]);
});

test('reverse maps complete and partial cloud snapshots without inventing price or FX',async t=>{
 const f=await imported(t);await activateCutover(f.db,{expectedEpoch:1,proof:evidence(f.core),edits:true,sends:true},now);
 const {storeSnapshot}=await import('../deploy/cloudflare-js/storage.mjs');
 const fingerprint=f.core.metadata.scope.recurringAccountFingerprint;
 const source={key:'kiwoom_accum',accountFingerprint:fingerprint,status:'SUCCESS',observedAt:now,safeErrorType:null};
 const common={capturedAt:now,dataAsOf:now,baseCurrency:'KRW',finalizedAt:now};
 await storeSnapshot(f.db,{...common,key:'cloud-complete',status:'COMPLETE',totalMarketValue:'15000',totalCostBasis:'9000',sources:[source],
   positions:[{accountFingerprint:fingerprint,instrument:{market:'US',ticker:'TEST',assetType:'ETF',currency:'USD'},quantity:'1',costBasis:'9',marketValue:'10',valueInBase:'14000'}],
   cash:[{accountFingerprint:fingerprint,currency:'KRW',amount:'1000',valueInBase:'1000'}]});
 await storeSnapshot(f.db,{...common,key:'cloud-partial',status:'PARTIAL',totalMarketValue:null,totalCostBasis:null,sources:[{...source,status:'FAILED',safeErrorType:'FixtureUnavailable'}],positions:[],cash:[]});
 await pauseCutover(f.db,2,'fixture snapshot rollback');
 await assert.rejects(()=>storeSnapshot(f.db,{...common,key:'too-late',status:'PARTIAL',totalMarketValue:null,totalCostBasis:null,sources:[source],positions:[],cash:[]}),/fence/);
 f.db.sqlite.prepare('VACUUM INTO ?').run(join(f.path,'cloud.db'));
 const result=JSON.parse(py(['-c',`import runpy,json,sys,sqlite3;from pathlib import Path;m=runpy.run_path('scripts/cloud/rollback_reconcile.py');r=Path(sys.argv[1]);result=m['reconcile_rollback'](r/'backup.db',json.loads((r/'core.json').read_text()),r/'cloud.db',r/'restored');db=sqlite3.connect(r/'restored/restored.sqlite3');assert db.execute("SELECT status,total_market_value FROM portfolio_snapshots WHERE idempotency_key='cloud-partial'").fetchone()==('PARTIAL',None);assert db.execute('SELECT quantity,value_in_base,current_price,fx_rate_id FROM position_snapshots').fetchone()==(1,14000,None,None);assert db.execute('SELECT amount FROM cash_snapshots').fetchone()==(1000,);assert db.execute('PRAGMA foreign_key_check').fetchone() is None;print(json.dumps(result))`,f.path]));
 assert.equal(result.restoredSnapshots,2);
});
test('reverse snapshot precision loss is rejected atomically with no restored output',async t=>{
 const f=await imported(t);await activateCutover(f.db,{expectedEpoch:1,proof:evidence(f.core),edits:true,sends:true},now);
 const {storeSnapshot}=await import('../deploy/cloudflare-js/storage.mjs');const fingerprint=f.core.metadata.scope.recurringAccountFingerprint;
 await storeSnapshot(f.db,{key:'fractional',capturedAt:now,dataAsOf:now,baseCurrency:'KRW',finalizedAt:now,status:'PARTIAL',totalMarketValue:null,totalCostBasis:null,
  sources:[{key:'fixture',accountFingerprint:fingerprint,status:'PARTIAL',observedAt:now,safeErrorType:null}],positions:[],
  cash:[{accountFingerprint:fingerprint,currency:'KRW',amount:'1000.1234567890123456789',valueInBase:null}]});
 await pauseCutover(f.db,2,'fixture');f.db.sqlite.prepare('VACUUM INTO ?').run(join(f.path,'cloud.db'));
 py(['-c',`import runpy,json,sys;from pathlib import Path;m=runpy.run_path('scripts/cloud/rollback_reconcile.py');r=Path(sys.argv[1])
try:m['reconcile_rollback'](r/'backup.db',json.loads((r/'core.json').read_text()),r/'cloud.db',r/'restored')
except m['Blocked'] as e:assert 'cannot preserve' in str(e),str(e)
else:raise AssertionError('rounded snapshot accepted')
assert not (r/'restored').exists();assert not (r/'restored.partial').exists()`,f.path]);
});

test('native portfolio-scoped NULL plans preserve identity through owner edits, new plans and reverse',async t=>{
 const f=await fixture(t,false,false,true);
 await beginCoreImport(f.db,f.core.manifest,f.core.metadata);
 for(const c of f.core.chunks)await stageCoreChunk(f.db,f.core.manifest,c);
 await finalizeCoreImport(f.db,f.core.manifest);
 assert.equal(f.db.rows('cloud_plans')[0].account_fingerprint,null);
 assert.equal(JSON.stringify(f.db.sqlite.prepare('SELECT * FROM cloud_plan_scope').all()),JSON.stringify([{plan_key:'fixture:US:TEST',portfolio_id:1}]));
 await activateCutover(f.db,{expectedEpoch:1,proof:evidence(f.core),edits:true,sends:true},now);
 const env=envFor(f),gateway=await createOperatingGateway(env,{now:()=>now});
 await assert.rejects(()=>createOperatingGateway({...env,CLOUD_PORTFOLIO_ID:'2'}),/binding/);
 await assert.rejects(()=>requireActiveCutover({...env,CLOUD_PORTFOLIO_ID:'2'},{chat:'42',users:[42]}),/fence/);
 assert.throws(()=>f.db.sqlite.prepare('UPDATE cloud_plan_scope SET portfolio_id=2').run(),/immutable/);
 await gateway.editPlan({planKey:'fixture:US:TEST',expectedVersion:1,amount:'1500',currency:'KRW',enabled:true,mutationKey:'portfolio-edit'});
 await gateway.createPlan({market:'US',ticker:'NEW',amount:'1000',currency:'KRW',instrumentCurrency:'USD',targetWeightPct:'0',memo:'synthetic',mutationKey:'portfolio-new'});
 assert.equal(f.db.sqlite.prepare('SELECT * FROM cloud_plan_scope').all().length,2);
 const added=f.db.rows('cloud_plans').find(p=>p.ticker==='NEW');
 assert.equal(added.instrument_currency,'USD');assert.equal(added.amount_currency,'KRW');
 assert.ok(added.memo.includes('[instrument=OWNER_UNVERIFIED]'));
 await assert.rejects(()=>gateway.createPlan({market:'US',ticker:'BAD',amount:'1000',currency:'KRW',instrumentCurrency:'KRW',targetWeightPct:'0',mutationKey:'bad-currency'}),/currency/);
 assert.ok(f.db.rows('cloud_plans').every(p=>p.account_fingerprint===null));
 await gateway.changeGroup({assetType:'EQUITY',delta:'0',stop:true,excluded:[],mutationKey:'portfolio-stop'});
 await gateway.changeGroup({assetType:'ETF',delta:'0',stop:true,excluded:[],mutationKey:'portfolio-stop-etf'});
 assert.ok(f.db.rows('cloud_plans').every(p=>p.enabled===0));
 assert.equal(await gateway.ownsPlan({planKey:'unknown-null',accountFingerprint:null}),false);
 await pauseCutover(f.db,2,'portfolio reverse');f.db.sqlite.prepare('VACUUM INTO ?').run(join(f.path,'cloud.db'));
 py(['-c',`import runpy,json,sys,sqlite3;from pathlib import Path;m=runpy.run_path('scripts/cloud/rollback_reconcile.py');r=Path(sys.argv[1]);m['reconcile_rollback'](r/'backup.db',json.loads((r/'core.json').read_text()),r/'cloud.db',r/'restored');db=sqlite3.connect(r/'restored/restored.sqlite3');assert db.execute('SELECT count(*) FROM recurring_buy_plans WHERE account_id IS NOT NULL').fetchone()[0]==0;assert db.execute('SELECT count(*) FROM recurring_buy_revisions WHERE account_id IS NOT NULL').fetchone()[0]==0;assert db.execute('SELECT count(*) FROM recurring_buy_plans WHERE portfolio_id=1').fetchone()[0]==2`,f.path]);
});


test('owner add accepts separate US quote and KRW budget without FX, cancellation or replay mutation',async t=>{
 const f=await fixture(t,false,false,true);await beginCoreImport(f.db,f.core.manifest,f.core.metadata);
 for(const c of f.core.chunks)await stageCoreChunk(f.db,f.core.manifest,c);
 await finalizeCoreImport(f.db,f.core.manifest);
 await activateCutover(f.db,{expectedEpoch:1,proof:evidence(f.core),edits:true,sends:true},now);
 const gateway=await createOperatingGateway(envFor(f),{now:()=>now,fetcher:()=>assert.fail('no finance API')});
 const route=(id,text,signal)=>routeUpdate({headerEntries:[['x-telegram-bot-api-secret-token','fixture-secret']],secret:'fixture-secret',allowedUserIds:[42],gateway,signal,
   body:new TextEncoder().encode(JSON.stringify({update_id:id,message:{from:{id:42},chat:{id:42,type:'private'},text}}))});
 assert.equal((await route(901,'/recurring_add US:PBR.A 1000KRW 0%',AbortSignal.abort())).status,'CANCELLED');
 assert.equal(f.db.rows('cloud_plans').length,1);
 assert.equal((await route(902,'/recurring_add US:PBR.A 1000KRW 0%')).status,'READY');
 assert.equal((await route(902,'/recurring_add US:PBR.A 1000KRW 0%')).status,'READY');
 const plan=f.db.rows('cloud_plans').find(p=>p.ticker==='PBR.A');
 assert.equal(plan.daily_amount,'1000');assert.equal(plan.instrument_currency,'USD');assert.equal(plan.amount_currency,'KRW');
 assert.equal(plan.account_fingerprint,null);assert.equal(plan.version,1);assert.match(plan.memo,/OWNER_UNVERIFIED/);
 await gateway.editPlan({planKey:plan.plan_key,expectedVersion:1,amount:'1500',currency:'KRW',enabled:true,mutationKey:'amount-1500'});
 await assert.rejects(()=>gateway.editPlan({planKey:plan.plan_key,expectedVersion:1,amount:'2000',currency:'KRW',enabled:true,mutationKey:'stale'}),/version/);
 assert.equal(f.db.rows('cloud_plan_revisions').filter(p=>p.plan_key===plan.plan_key).length,2);
 await gateway.changeGroup({assetType:'EQUITY',delta:'-1000',stop:false,excluded:[],mutationKey:'portfolio-minus'});
 assert.equal(f.db.rows('cloud_plans').find(p=>p.ticker==='PBR.A').daily_amount,'500');
 await assert.rejects(()=>gateway.changeGroup({assetType:'EQUITY',delta:'-1000',stop:false,excluded:[],mutationKey:'portfolio-negative'}),/positive/);
 assert.equal((await route(903,'/recurring_add UNKNOWN 1500KRW 0%')).status,'READY');
 assert.equal(f.db.rows('cloud_plans').find(p=>p.ticker==='UNKNOWN').market,'US');
 assert.equal(f.db.rows('cloud_transactions').length,1); // Original fixture ledger only.
});

test('D1 trigger-inclusive metadata uses direct RETURNING receipt and recovers postcommit response failure once',async t=>{
 const f=await fixture(t,false,false,true);await beginCoreImport(f.db,f.core.manifest,f.core.metadata);
 for(const c of f.core.chunks)await stageCoreChunk(f.db,f.core.manifest,c);
 await finalizeCoreImport(f.db,f.core.manifest);
 await activateCutover(f.db,{expectedEpoch:1,proof:evidence(f.core),edits:true,sends:true},now);
 let sends=0,failOnce=true,sawTriggerCount=false;
 const batch=f.db.batch.bind(f.db);
 f.db.batch=async statements=>{const result=await batch(statements);
  if(result.some(r=>r.results?.some(row=>row.plan_key==='manual:US:RECOVER'))){
   const receipt=result.find(r=>r.results?.some(row=>row.plan_key==='manual:US:RECOVER'));
   sawTriggerCount=receipt.meta.changes===2;
   if(failOnce){failOnce=false;throw new Error('fixture postcommit response lost');}
  }return result;};
 const worker=createOperatingWorker({requireCutover:true,gatewayFactory:e=>createOperatingGateway(e,{now:()=>now}),fetcher:async()=>{sends++;return {ok:true,json:async()=>({ok:true,result:{message_id:sends}})};},now:()=>now});
 const env=envFor(f),command='/recurring_add US:RECOVER 1500KRW 0%';
 assert.equal((await worker.fetch(request(950,command),env)).status,503);
 assert.equal(sends,0);assert.equal(sawTriggerCount,true);
 assert.equal((await worker.fetch(request(950,command),env)).status,200);
 assert.equal((await worker.fetch(request(950,command),env)).status,200);
 assert.equal(sends,1);
 assert.equal(f.db.rows('cloud_plans').filter(r=>r.plan_key==='manual:US:RECOVER').length,1);
 assert.equal(f.db.rows('cloud_plan_revisions').filter(r=>r.plan_key==='manual:US:RECOVER').length,1);
 assert.equal(f.db.rows('cloud_outbox').filter(r=>r.event_key==='update:950:0').length,1);
});

test('reverse closes every SQLite handle before Windows-style directory rename and keeps pre-existing partial',async t=>{
 const f=await fixture(t,false,false,true);await beginCoreImport(f.db,f.core.manifest,f.core.metadata);
 for(const c of f.core.chunks)await stageCoreChunk(f.db,f.core.manifest,c);
 await finalizeCoreImport(f.db,f.core.manifest);
 await activateCutover(f.db,{expectedEpoch:1,proof:evidence(f.core),edits:true,sends:true},now);
 await pauseCutover(f.db,2,'Windows locking rehearsal');f.db.sqlite.prepare('VACUUM INTO ?').run(join(f.path,'cloud.db'));
 py(['-c',`import runpy,json,sys,sqlite3;from pathlib import Path;from unittest.mock import patch
m=runpy.run_path('scripts/cloud/rollback_reconcile.py');r=Path(sys.argv[1]);bundle=json.loads((r/'core.json').read_text());opened=[];connect=sqlite3.connect;rename=Path.rename
class Tracked(sqlite3.Connection):
 def __init__(self,*args,**kwargs):
  super().__init__(*args,**kwargs);self.closed=False;opened.append(self)
 def close(self):
  super().close();self.closed=True
def tracked(*args,**kwargs):return connect(*args,**kwargs,factory=Tracked)
def windows_rename(source,target):
 if any(not db.closed for db in opened):raise PermissionError('Windows-style open SQLite handle blocks rename')
 return rename(source,target)
with patch.object(sqlite3,'connect',tracked),patch.object(Path,'rename',windows_rename):
 result=m['reconcile_rollback'](r/'backup.db',bundle,r/'cloud.db',r/'restored')
assert result['startup']=='HELD' and len(opened)==3 and all(db.closed for db in opened)
assert not (r/'restored.partial').exists()
keep=r/'untouched.partial';keep.mkdir();(keep/'operator-evidence.txt').write_text('preserve')
try:m['reconcile_rollback'](r/'backup.db',bundle,r/'cloud.db',r/'untouched')
except m['Blocked'] as e:assert 'partial rollback already exists' in str(e)
else:raise AssertionError('pre-existing partial overwritten')
assert (keep/'operator-evidence.txt').read_text()=='preserve'
print('PASS all connections closed before rename; pre-existing partial preserved')`,f.path]);
});


test('legacy Meritz missing cash/history sections survive import and reverse without becoming empty',async t=>{
 const f=await fixture(t,false,false,true,true);await beginCoreImport(f.db,f.core.manifest,f.core.metadata);
 for(const c of f.core.chunks)await stageCoreChunk(f.db,f.core.manifest,c);
 await finalizeCoreImport(f.db,f.core.manifest);
 const gateway=await createOperatingGateway(envFor(f),{now:()=>now});
 const cash=await gateway.providers.find(p=>p.name==='meritz').getCash();
 assert.equal(cash.status,'unavailable');assert.equal(cash.safeErrorType,'MissingManualSection');
 await activateCutover(f.db,{expectedEpoch:1,proof:evidence(f.core),edits:true,sends:true},now);
 await pauseCutover(f.db,2,'missing manual section preservation');f.db.sqlite.prepare('VACUUM INTO ?').run(join(f.path,'cloud.db'));
 py(['-c',`import runpy,json,sys;from pathlib import Path;m=runpy.run_path('scripts/cloud/rollback_reconcile.py');r=Path(sys.argv[1]);m['reconcile_rollback'](r/'backup.db',json.loads((r/'core.json').read_text()),r/'cloud.db',r/'restored');doc=json.loads((r/'restored/meritz-account.json').read_text());assert 'cash' not in doc and 'transactions' not in doc;assert doc['positions']==[]`,f.path]);
});

test('explicit native and legacy report identities suppress both periods and reverse preserves exact source rows',async t=>{
 const f=await fixture(t,false,false,true,false,true);
 const original=structuredClone(f.core.metadata.reportWatermarks);
 assert.equal(original.length,16);
 assert.deepEqual([...new Set(original.map(r=>r.report_type))],['DAILY','WEEKLY','daily','weekly']);
 await beginCoreImport(f.db,f.core.manifest,f.core.metadata);
 for(const c of f.core.chunks)await stageCoreChunk(f.db,f.core.manifest,c);
 await finalizeCoreImport(f.db,f.core.manifest);
 const holds=f.db.sqlite.prepare('SELECT * FROM cloud_legacy_report_holds').all().sort((a,b)=>a.period_key.localeCompare(b.period_key));
 assert.deepEqual(holds.map(r=>[r.period_key,r.report_type,r.local_date,JSON.parse(r.legacy_states)]),
  ['daily','weekly'].map(kind=>[`${kind}:20261029`,kind,'2026-10-29',['FAILED','PENDING','SENT','UNKNOWN']]));
 assert.deepEqual(f.core.metadata.reportWatermarks,original);
 await activateCutover(f.db,{expectedEpoch:1,proof:evidence(f.core),edits:true,sends:true},now);
 for(const kind of ['daily','weekly'])assert.throws(()=>f.db.sqlite.prepare("INSERT INTO cloud_report_claims VALUES (?,?,'fixture')").run(`${kind}:20261029`,now),/suppressed/);
 await pauseCutover(f.db,2,'report identity fixture rollback');
 f.db.sqlite.exec(`VACUUM INTO '${join(f.path,'cloud.db').replaceAll("'","''")}'`);
 const result=JSON.parse(py(['-c',`import runpy,json,sys,sqlite3;from pathlib import Path;from contextlib import closing
r=Path(sys.argv[1]);m=runpy.run_path('scripts/cloud/rollback_reconcile.py');bundle=json.loads((r/'core.json').read_text())
result=m['reconcile_rollback'](r/'backup.db',bundle,r/'cloud.db',r/'restored')
with closing(sqlite3.connect(r/'backup.db')) as source, closing(sqlite3.connect(r/'restored/restored.sqlite3')) as restored:
 for table in ('reports','report_deliveries'):
  assert source.execute('SELECT * FROM '+table+' ORDER BY id').fetchall()==restored.execute('SELECT * FROM '+table+' ORDER BY id').fetchall()
 assert restored.execute('SELECT state FROM cloud_rollback_gate').fetchone()==('HELD',)
 assert restored.execute('PRAGMA foreign_key_check').fetchall()==[]
print(json.dumps({'startup':result['startup'],'exactReportRows':16,'exactDeliveryRows':16}))`,f.path]));
 assert.deepEqual(result,{startup:'HELD',exactReportRows:16,exactDeliveryRows:16});
});

test('unreviewed report type, timezone and date remain blocked',()=>{
 const result=JSON.parse(py(['-c',`import json,sys,tempfile,sqlite3,importlib.util;from pathlib import Path
sys.path.insert(0,'tests');from cloud_full_schema_fixture import seed,FINGERPRINT
spec=importlib.util.spec_from_file_location('report_projection','scripts/cloud/migration_projection.py');m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)
cases=[('reports','report_type',v) for v in ('Daily','Weekly','MONTHLY',' DAILY','DAILY ','','daily:20261029')]+[('reports','timezone_name','UTC'),('reports','period_key','2026-10-29T00:00:00'),('reports','period_key','2026-W44')]
with tempfile.TemporaryDirectory(prefix='report-reject-') as tmp:
 for n,(table,column,value) in enumerate(cases):
  r=Path(tmp)/str(n);r.mkdir();seed(r/'source.db')
  db=sqlite3.connect(r/'source.db');db.execute('UPDATE '+table+' SET '+column+'=?',(value,));db.commit();db.close()
  audit=m.p.backup_and_audit(r/'source.db',r/'backup.db')
  try:m.build_core_bundle(r/'backup.db',audit,scope={'portfolioId':1,'ownerUserId':'42','accountFingerprints':[FINGERPRINT],'recurringAccountFingerprint':FINGERPRINT,'targetId':'synthetic-operating-target','manualKinds':[]})
  except m.Blocked as error:assert str(error).startswith('legacy report'),str(error)
  else:raise AssertionError('unreviewed mapping accepted: '+str((table,column,value)))
print(json.dumps({'rejected':len(cases)}))`]));
 assert.equal(result.rejected,10);
});
