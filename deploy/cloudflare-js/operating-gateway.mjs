// Explicit composition for the operating candidate. No broker calls occur at construction.
import { SyntheticCloudGateway } from './cloud-gateway.mjs';
import { createKiwoomReadOnlyProvider } from './kiwoom-readonly.mjs';
import { latestMeritzRevision, manualSnapshotFreshness } from './manual-input.mjs';
import { readLegacyHistory } from './legacy-read.mjs';
import { createYahooChartReadOnly } from './yahoo-chart-readonly.mjs';

const FINGERPRINT = /^[a-z][a-z0-9_]{0,30}_[0-9a-f]{64}$/;
function requiredFingerprint(value) {
  if (typeof value !== 'string' || !FINGERPRINT.test(value)) {
    throw new TypeError('approved account fingerprint binding required');
  }
  return value;
}

function meritzProvider(db, now) {
  async function read(kind) { return latestMeritzRevision(db, kind); }
  async function result(select, section = null) {
    const revision = await read('ACCOUNT_SNAPSHOT');
    if (!revision) return { broker: 'meritz', status: 'not_configured', items: [] };
    if (section && !Object.hasOwn(revision.document, section)) {
      return { broker: 'meritz', status: 'unavailable', items: [], safeErrorType: 'MissingManualSection' };
    }
    const status = manualSnapshotFreshness(revision, now()) === 'STALE' ? 'stale' : 'ok';
    return { broker: 'meritz', status, items: select(revision.document) };
  }
  return { name: 'meritz',
    getAccounts: () => result(doc => doc.accounts.map(row => ({ broker: 'meritz',
      accountId: row.account_id, displayName: row.display_name || '등록 계좌',
      baseCurrency: row.base_currency }))),
    getPositions: () => result(doc => doc.positions.map(row => ({ broker: 'meritz',
      accountId: row.account_id, instrument: { market: row.market, ticker: row.ticker,
        assetType: row.asset_type, currency: row.currency }, quantity: row.quantity,
      averagePrice: row.average_price, currentPrice: row.current_price,
      costBasis: row.cost_basis, marketValue: row.market_value,
      companyName: row.company_name, asOf: row.as_of || doc.as_of }))),
    getCash: () => result(doc => doc.cash.map(row => ({ broker: 'meritz',
      accountId: row.account_id, currency: row.currency, amount: row.amount,
      asOf: row.as_of || doc.as_of })), 'cash'),
  };
}

export async function createOperatingGateway(env, { now = () => new Date().toISOString(),
  fetcher = fetch, marketProvider = null } = {}) {
  if (!env?.DB) throw new TypeError('D1 binding unavailable');
  const brokerConfigured = [env.KIWOOM_MAIN_APP_KEY, env.KIWOOM_MAIN_APP_SECRET, env.KIWOOM_MAIN_ACCOUNT_ALIAS];
  if (brokerConfigured.some(Boolean) && !brokerConfigured.every(Boolean)) {
    throw new TypeError('partial read-only Kiwoom binding');
  }
  const cutover = await env.DB.prepare('SELECT * FROM cloud_cutover WHERE singleton=1').first();
  const scope = env.KIWOOM_ACCUM_FINGERPRINT == null && cutover?.scope_fingerprint === null
    ? null : requiredFingerprint(env.KIWOOM_ACCUM_FINGERPRINT);
  if (cutover && (cutover.scope_fingerprint !== scope || cutover.portfolio_id !== Number(env.CLOUD_PORTFOLIO_ID))) {
    throw new TypeError('approved portfolio/account binding differs from import');
  }
  const mainScope = env.KIWOOM_MAIN_FINGERPRINT == null ? null :
    requiredFingerprint(env.KIWOOM_MAIN_FINGERPRINT);
  const meritzScope = env.MERITZ_SNAPSHOT_FINGERPRINT == null ? null :
    requiredFingerprint(env.MERITZ_SNAPSHOT_FINGERPRINT);
  const providers = [brokerConfigured.every(Boolean) ? createKiwoomReadOnlyProvider({ name: 'kiwoom_main',
    accountId: env.KIWOOM_MAIN_ACCOUNT_ALIAS, appKey: env.KIWOOM_MAIN_APP_KEY,
    appSecret: env.KIWOOM_MAIN_APP_SECRET, fetcher, now }) : { name: 'kiwoom_main' }, meritzProvider(env.DB, now)];
  const selectedMarket = marketProvider || (env.PUBLIC_YAHOO_CHART_ENABLED === 'true' ?
    createYahooChartReadOnly({ db: env.DB, fetcher, now }) : null);
  const externalRecurringPlans = async () => {
    const revision = await latestMeritzRevision(env.DB, 'RECURRING_PLANS');
    return (revision?.document.plans || []).map(row => ({ broker: 'meritz',
      instrument: { market: row.market, ticker: row.ticker,
        assetType: row.asset_type, currency: row.instrument_currency },
      currency: row.amount_currency, dailyAmount: row.daily_amount,
      enabled: row.enabled, scheduledWeekdays: [0, 1, 2, 3, 4],
      startedAt: row.started_at, memo: row.memo, targetWeightPct: null,
      sourceDataAsOf: revision.dataAsOf }));
  };
  const gateway = new SyntheticCloudGateway({ db: env.DB, providers,
    marketProvider: selectedMarket,
    externalRecurringPlans, scopeAccountFingerprint: scope, scopePortfolioId: cutover?.portfolio_id ?? null,
    mainAccountFingerprint: mainScope,
    meritzSnapshotFingerprint: meritzScope, now,
    timezoneName: env.REPORT_TIMEZONE || 'Asia/Seoul' });
  gateway.legacyHistory = (kind, options) => readLegacyHistory(env.DB, kind, options);
  return gateway;
}
