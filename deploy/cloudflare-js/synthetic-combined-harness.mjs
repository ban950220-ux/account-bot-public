// Fixed, scheduled-only Worker rehearsal. No live provider, Telegram or public writes.
import core, { initSyntheticTables } from './synthetic-harness.mjs';
import { SyntheticCloudGateway } from './cloud-gateway.mjs';
import { routeUpdate } from './command-router.mjs';
import { importAccount } from './storage.mjs';
import { enqueueUpdate } from './outbox.mjs';

const CASES = { small: 1, representative: 5, large: 40 };
const FIXED_AT = '2026-02-02T01:00:00Z';
const SECRET = 'synthetic-test-only';
const accountFor = name => `fixture_v2_${name}_${'d'.repeat(64)}`;
const mutationTicker = name => name === 'small' ? '999991' : '999995';
const HEADER = [['X-Telegram-Bot-Api-Secret-Token', SECRET]];
const keyFor = name => `commands-v2:${name}`;
const diagnosticsDDL = `CREATE TABLE IF NOT EXISTS cloud_test_diagnostics (
  run_key TEXT PRIMARY KEY, stage TEXT NOT NULL, code TEXT NOT NULL)`;
const symbol = index => `9${String(index).padStart(5, '0')}`;
const instrument = index => ({ market: 'KOSPI', ticker: symbol(index),
  assetType: 'EQUITY', currency: 'KRW' });

function fixtures(size) {
  const source = { name: 'kiwoom_main',
    getPositions: async () => ({ broker: 'kiwoom_main', status: 'ok',
      items: Array.from({ length: size }, (_, index) => ({ broker: 'kiwoom_main',
        accountId: 'fixture-account', instrument: instrument(index), quantity: '1',
        costBasis: '1000', marketValue: '1000' })) }),
    getCash: async () => ({ broker: 'kiwoom_main', status: 'ok', items: [] }),
    getAccounts: async () => ({ broker: 'kiwoom_main', status: 'ok', items: [] }) };
  const market = {
    getPrice: async (marketName, ticker) => ({ market: marketName, ticker,
      currency: 'KRW', price: '1000', asOf: FIXED_AT, provider: 'fixture', stale: false }),
    getFundamentals: async () => ({ asOf: FIXED_AT, provider: 'fixture',
      revenueGrowth: '0.12', earningsGrowth: '0.20', grossMargin: '0.4',
      operatingMargin: '0.2', freeCashFlow: '100', returnOnEquity: '0.18',
      totalCash: '200', totalDebt: '300', debtToEquity: '75' }),
    getValuation: async () => ({ asOf: FIXED_AT, forwardPe: '24', peg: '1.6',
      enterpriseToEbitda: '18', freeCashFlowYield: '0.04',
      historicalForwardPeMedian: '22', dilutionPct: '2' }),
    getEarnings: async () => ({ period: 'Q1', revenue: '1000', revenueYoy: '0.1' }),
    getNews: async () => [],
    getTrend: async () => ({ return1y: '0.15', cagr3y: '0.1',
      maxDrawdown3y: '-0.3', annualizedVolatility1y: '0.25',
      above200dAverage: true }),
  };
  const externalRecurringPlans = async () => Array.from({ length: size }, (_, index) => ({
    instrument: instrument(index), broker: 'meritz', currency: 'KRW',
    dailyAmount: '1000', targetWeightPct: '1', enabled: true,
    scheduledWeekdays: [0, 1, 2, 3, 4], memo: '', investmentThesis: '',
  }));
  return { source, market, externalRecurringPlans };
}

function updateBody(id, command) {
  return new TextEncoder().encode(JSON.stringify({ update_id: id,
    message: { from: { id: 42 }, chat: { id: 42, type: 'private' }, text: command } }));
}

async function commandCase(db, name) {
  await initSyntheticTables(db);
  await db.prepare(diagnosticsDDL).run();
  const key = keyFor(name);
  const claim = await db.prepare(`INSERT INTO cloud_test_runs (run_key, status)
    VALUES (?, 'RUNNING') ON CONFLICT(run_key) DO NOTHING`).bind(key).run();
  if (claim.meta.changes !== 1) return;
  let stage = 'account_import';
  try {
    const size = CASES[name];
    const account = accountFor(name);
    await importAccount(db, { fingerprint: account, broker: 'synthetic',
      baseCurrency: 'KRW' });
    const { source, market, externalRecurringPlans } = fixtures(size);
    const gateway = new SyntheticCloudGateway({ db, providers: [source],
      marketProvider: market, externalRecurringPlans,
      scopeAccountFingerprint: account, now: () => FIXED_AT });
    const edited = mutationTicker(name);
    const commands = name === 'large' ? ['/recurring', '/analyze KOSPI:900000', '/weekly'] :
      ['/start', '/help', '/assets', '/portfolio', `/recurring_add KOSPI:${edited} 2000KRW 8%`,
        '/recurring_update 개별주 전부 +1000', '/stock KOSPI:900000',
        `/analyze KOSPI:${edited}`, `/thesis KOSPI:${edited} synthetic thesis`,
        '/recurring', '/rebalance 1000', '/weekly'];
    const base = name === 'small' ? 910000 : name === 'representative' ? 920000 : 930000;
    let passed = true;
    let failure = null;
    for (const [index, command] of commands.entries()) {
      stage = `command_${index}`;
      const result = await routeUpdate({ headerEntries: HEADER,
        body: updateBody(base + index, command), secret: SECRET,
        allowedUserIds: [42], gateway });
      if (result.status !== 'READY' || !result.reply) {
        passed = false;
        failure ??= { stage, code: 'COMMAND_NOT_READY' };
      }
      if (index === 4 && name !== 'large' && result.status === 'READY') {
        stage = 'outbox_4';
        const first = await enqueueUpdate(db, base + index, 'synthetic-only',
          JSON.stringify({ reply: result.reply }), FIXED_AT);
        const replay = await enqueueUpdate(db, base + index, 'synthetic-only',
          JSON.stringify({ reply: result.reply }), FIXED_AT);
        if (!first || replay) {
          passed = false;
          failure ??= { stage, code: 'OUTBOX_ASSERTION' };
        }
      }
    }
    stage = 'finalize';
    if (failure) await db.prepare(`INSERT INTO cloud_test_diagnostics
      (run_key, stage, code) VALUES (?, ?, ?)`).bind(key, failure.stage, failure.code).run();
    await db.prepare(`UPDATE cloud_test_runs SET status = ?, completed_at = ?
      WHERE run_key = ? AND status = 'RUNNING'`).bind(passed ? 'PASS' : 'FAIL',
      new Date().toISOString(), key).run();
  } catch (error) {
    const code = error?.code === 'ERR_SQLITE_ERROR' ? 'SQL_ERROR' : 'EXCEPTION';
    await db.prepare(`INSERT INTO cloud_test_diagnostics (run_key, stage, code)
      VALUES (?, ?, ?) ON CONFLICT(run_key) DO NOTHING`).bind(key, stage, code).run();
    await db.prepare(`UPDATE cloud_test_runs SET status = 'FAIL', completed_at = ?
      WHERE run_key = ? AND status = 'RUNNING'`).bind(new Date().toISOString(), key).run();
  }
}

export default {
  async fetch(request, env) {
    if (!Object.hasOwn(CASES, env.TEST_CASE)) return core.fetch(request, env);
    if (request.method !== 'GET' || new URL(request.url).pathname !== '/health') {
      return new Response('not found', { status: 404 });
    }
    if (!env.DB) return Response.json({ status: 'UNBOUND' }, { status: 503 });
    try {
      const row = await env.DB.prepare(`SELECT status FROM cloud_test_runs
        WHERE run_key = ?`).bind(keyFor(env.TEST_CASE)).first();
      const diagnostic = row?.status === 'FAIL' ? await env.DB.prepare(
        `SELECT stage, code FROM cloud_test_diagnostics WHERE run_key = ?`
      ).bind(keyFor(env.TEST_CASE)).first() : null;
      return Response.json({ status: row?.status || 'NOT_RUN',
        ...(diagnostic ? { diagnostic } : {}) },
        { headers: { 'Cache-Control': 'no-store' } });
    } catch {
      return Response.json({ status: 'NOT_RUN' },
        { headers: { 'Cache-Control': 'no-store' } });
    }
  },
  async scheduled(event, env) {
    if (env.TEST_WRITES_ENABLED !== '1' || !env.DB) return;
    if (Object.hasOwn(CASES, env.TEST_CASE)) await commandCase(env.DB, env.TEST_CASE);
    else if (env.TEST_CASE === undefined || env.TEST_CASE === 'core') {
      await core.scheduled(event, env);
    }
  },
};
