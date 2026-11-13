// Injected D1 and read-only provider gateway for local synthetic command rehearsal.
import { add, compare, decimal, divide28, exact, multiply } from './finance.mjs';
import { refreshWithProviders } from './provider-contracts.mjs';
import { analyzeWithProvider } from './analysis-service.mjs';
import { appendThesis, applyGroupChanges, applyPlanChange, createPlanOnce,
  currentThesis, groupMutationPlans, listCurrentPlans } from './cloud-write.mjs';
import { enqueueUpdate } from './outbox.mjs';
import { currentPolicy, savePolicy } from './policy-store.mjs';
import { recordTransactionOnce } from './storage.mjs';

const KR = new Set(['KOSPI', 'KOSDAQ', 'KRX', 'NXT']);
const US = new Set(['US', 'USA', 'NASDAQ', 'NAS', 'NYSE', 'AMEX', 'ARCA', 'NYSEARCA']);
const ACCOUNT_SCOPE = /^[a-z][a-z0-9_]{0,30}_[0-9a-f]{64}$/;
const stable = plan => `${plan.instrument.market}:${plan.instrument.ticker}:` +
  `${plan.instrument.assetType}:${plan.instrument.currency}`;
const match = (left, market, ticker) => left.ticker === ticker &&
  (left.market === market || (['US', 'USA'].includes(left.market) && US.has(market)) ||
    (['US', 'USA'].includes(market) && US.has(left.market)));

function toDomain(row) {
  const memo = row.memo || '';
  const scheduledWeekdays = memo.includes('[schedule=MON]') ? [0] :
    memo.includes('[schedule=FRI]') ? [4] : [0, 1, 2, 3, 4];
  const broker = memo.includes('[source=USER_CANDIDATE]') ? 'candidate' :
    row.source === 'TELEGRAM' ? 'kiwoom_accum' :
      row.source === 'MERITZ_SNAPSHOT' ? 'meritz' : row.source.toLowerCase();
  return { ...row, currency: row.amountCurrency, broker, scheduledWeekdays,
    targetWeightPct: row.targetWeight === null ? null :
      exact(multiply(decimal(row.targetWeight), decimal('100'))),
    investmentThesis: row.investmentThesis || '' };
}

export class SyntheticCloudGateway {
  constructor({ db, providers, marketProvider = null, externalRecurringPlans,
    scopeAccountFingerprint, scopePortfolioId = null, meritzSnapshotFingerprint = null,
    mainAccountFingerprint = null,
    now = () => new Date().toISOString(),
    timezoneName = 'Asia/Seoul' }) {
    if (!db || !Array.isArray(providers) ||
        typeof externalRecurringPlans !== 'function' ||
        !(ACCOUNT_SCOPE.test(scopeAccountFingerprint) || (scopeAccountFingerprint === null && Number.isSafeInteger(scopePortfolioId) && scopePortfolioId > 0)) ||
        (mainAccountFingerprint !== null && !ACCOUNT_SCOPE.test(mainAccountFingerprint)) ||
        (meritzSnapshotFingerprint !== null &&
          (!ACCOUNT_SCOPE.test(meritzSnapshotFingerprint) ||
            meritzSnapshotFingerprint === scopeAccountFingerprint))) {
      throw new TypeError('explicit D1, source and account scope required');
    }
    this.db = db;
    this.providers = providers;
    this.marketProvider = marketProvider;
    this.analysisProvider = marketProvider;
    this.externalRecurringPlans = externalRecurringPlans;
    this.scopeAccountFingerprint = scopeAccountFingerprint;
    this.scopePortfolioId = scopePortfolioId;
    this.meritzSnapshotFingerprint = meritzSnapshotFingerprint;
    this.mainAccountFingerprint = mainAccountFingerprint;
    this.now = now;
    this.timezoneName = timezoneName;
    this.lastSnapshot = null;
  }

  async refresh({ allBrokers }) {
    const names = allBrokers ? this.providers.map(provider => provider.name) : ['kiwoom_main'];
    let fxQuotes = [];
    if (typeof this.marketProvider?.getFx === 'function') {
      try { fxQuotes = [await this.marketProvider.getFx('USD', 'KRW')]; }
      catch { /* Missing FX remains missing; it is never synthesized as 0 or 1. */ }
    }
    const refresh = await refreshWithProviders(this.providers, names, fxQuotes);
    this.lastSnapshot = refresh.snapshot;
    return { ...refresh, collectedAt: this.now() };
  }

  async listPlans(enabledOnly) {
    const local = (await listCurrentPlans(this.db, enabledOnly, { withThesis: true }))
      .map(toDomain);
    const external = await this.externalRecurringPlans(enabledOnly);
    if (!Array.isArray(external)) throw new TypeError('external source scope unavailable');
    const merged = new Map();
    for (const plan of external) {
      if (!plan.broker || !plan.instrument || !plan.currency ||
          typeof plan.enabled !== 'boolean' ||
          compare(decimal(plan.dailyAmount), decimal('0')) <= 0) {
        throw new TypeError('invalid external recurring source');
      }
      if (!enabledOnly || plan.enabled) merged.set(`${stable(plan)}:${plan.broker}`, plan);
    }
    for (const plan of local) merged.set(`${stable(plan)}:${plan.broker}`, plan);
    return [...merged.values()].sort((a, b) =>
      a.broker.localeCompare(b.broker) || stable(a).localeCompare(stable(b)));
  }

  async ownsPlan(plan) {
    if (plan.accountFingerprint !== null) return plan.accountFingerprint === this.scopeAccountFingerprint;
    if (this.scopePortfolioId === null) return false;
    const scope = await this.db.prepare('SELECT portfolio_id FROM cloud_plan_scope WHERE plan_key=?').bind(plan.planKey).first();
    return scope?.portfolio_id === this.scopePortfolioId;
  }

  async createPlan({ market, ticker, amount, currency, instrumentCurrency = null, targetWeightPct, memo,
    mutationKey, delivery = null }) {
    let planKey = `manual:${market}:${ticker}`;
    const plans = await listCurrentPlans(this.db);
    let existing = plans.find(row => row.planKey === planKey);
    if (!existing) {
      const aliases = [];
      for (const plan of plans) {
        if (match(plan.instrument, market, ticker) && await this.ownsPlan(plan)) aliases.push(plan);
      }
      if (aliases.length > 1 || (aliases.length === 1 && !aliases[0].planKey.startsWith('manual:'))) {
        throw new TypeError('existing listing requires explicit recurring_set plan key');
      }
      if (aliases.length === 1) { existing = aliases[0]; planKey = existing.planKey; }
    }
    if (existing && !await this.ownsPlan(existing)) {
      throw new TypeError('plan belongs to a different account scope');
    }
    const quoteCurrency = existing?.instrument.currency || (KR.has(market) ? 'KRW' : US.has(market) ? 'USD' : null);
    if (!quoteCurrency || (instrumentCurrency !== null && instrumentCurrency !== quoteCurrency) ||
        !['KRW','USD'].includes(currency) || (KR.has(market) && currency !== 'KRW') ||
        (existing && existing.amountCurrency !== currency)) {
      throw new TypeError('explicit supported instrument/budget currency pair required');
    }
    // Manual planning identity only, never a broker/listing verification or FX conversion.
    const unverified = '[instrument=OWNER_UNVERIFIED]';
    const planMemo = (!existing || existing.memo?.includes(unverified)) && !(memo || '').includes(unverified)
      ? [memo, unverified].filter(Boolean).join(' ') : memo || null;
    const start = existing?.startedAt || this.now();
    const state = { instrument: existing?.instrument || {
      market, ticker, assetType: 'EQUITY', currency: quoteCurrency },
    accountFingerprint: existing ? existing.accountFingerprint : this.scopeAccountFingerprint,
    source: 'TELEGRAM', amountCurrency: currency,
    dailyAmount: amount, targetWeight: exact(divide28(decimal(targetWeightPct), decimal('100'))),
    enabled: true, timezoneName: this.timezoneName, startedAt: start,
    memo: planMemo };
    const staged = delivery && { ...delivery, reply: delivery.render(toDomain(state)) };
    const outcome = existing ? await applyPlanChange(this.db, planKey, existing.version,
      mutationKey, state, this.now(), staged) :
      await createPlanOnce(this.db, planKey, mutationKey, state, this.now(), staged);
    if (!['CHANGED', 'UNCHANGED', 'CREATED', 'REPLAY'].includes(outcome)) {
      throw new Error('plan changed concurrently');
    }
    if (outcome === 'UNCHANGED' && staged) await enqueueUpdate(this.db,
      staged.updateId, staged.destination, JSON.stringify({ text: staged.reply }), staged.now);
    const current = (await listCurrentPlans(this.db)).find(row => row.planKey === planKey);
    return toDomain(current);
  }

  async editPlan({ planKey, expectedVersion, amount, currency, enabled, mutationKey, delivery = null }) {
    const row = (await listCurrentPlans(this.db)).find(plan => plan.planKey === planKey);
    if (!row || !await this.ownsPlan(row) ||
        row.version !== expectedVersion || row.amountCurrency !== currency || typeof enabled !== 'boolean') {
      throw new TypeError('plan/account/currency/version conflict');
    }
    const value = decimal(amount);
    // This is an owner's planning amount, not a brokerage order. Do not impose
    // a guessed broker minimum/step (existing 1,000/1,500 KRW plans are valid).
    if (compare(value, decimal('0.00000001')) < 0) {
      throw new TypeError('explicit positive planning amount required');
    }
    const next = { ...row, dailyAmount: exact(value), enabled };
    const staged = delivery && { ...delivery, reply: delivery.render(toDomain({ ...next, version: expectedVersion+1 })) };
    const outcome = await applyPlanChange(this.db, planKey, expectedVersion, mutationKey, next, this.now(), staged);
    if (!['CHANGED','UNCHANGED'].includes(outcome)) throw new TypeError('stale plan edit');
    if (outcome === 'UNCHANGED' && staged) await enqueueUpdate(this.db, staged.updateId,
      staged.destination, JSON.stringify({ text: delivery.render(toDomain(row)) }), staged.now);
    return toDomain((await listCurrentPlans(this.db)).find(plan => plan.planKey === planKey));
  }

  async editableFilter() {
    const portfolioKeys = new Set(this.scopePortfolioId === null ? [] :
      (await this.db.prepare('SELECT plan_key FROM cloud_plan_scope WHERE portfolio_id=?').bind(this.scopePortfolioId).all()).results.map(row => row.plan_key));
    return plan => (plan.accountFingerprint === null ? portfolioKeys.has(plan.planKey) : plan.accountFingerprint === this.scopeAccountFingerprint) ||
      (this.meritzSnapshotFingerprint !== null &&
        plan.accountFingerprint === this.meritzSnapshotFingerprint &&
        plan.source === 'MERITZ_SNAPSHOT');
  }

  async editablePlanKeys() {
    const editable = await this.editableFilter();
    return new Set((await listCurrentPlans(this.db, true)).filter(editable)
      .map(plan => plan.planKey));
  }

  currentPolicy() { return currentPolicy(this.db); }

  async savePolicy({ policy, expectedVersion, mutationKey, delivery = null }) {
    const current = await currentPolicy(this.db);
    if (!current.installed) throw new Error('policy tables unavailable');
    if (current.version !== expectedVersion) throw new TypeError('stale policy edit');
    const staged = delivery && { ...delivery,
      reply: delivery.render({ policy, version: expectedVersion + 1 }) };
    if (JSON.stringify(current.policy) === JSON.stringify(policy)) {
      if (delivery) await enqueueUpdate(this.db, delivery.updateId, delivery.destination,
        JSON.stringify({ text: delivery.render(current) }), delivery.now);
      return current;
    }
    const outcome = await savePolicy(this.db, { policy, expectedVersion, mutationKey,
      effectiveAt: this.now(), delivery: staged });
    if (outcome !== 'CHANGED') throw new Error('policy changed concurrently');
    return currentPolicy(this.db);
  }

  // Returns the plans a group mutation already changed, or null if it never ran.
  async replayedGroup(mutationKey) {
    const keys = new Set(await groupMutationPlans(this.db, mutationKey));
    if (!keys.size) return null;
    const editable = await this.editableFilter();
    return (await listCurrentPlans(this.db)).filter(plan => keys.has(plan.planKey) &&
      editable(plan)).map(toDomain);
  }

  // Applies an approved proposal: every listed plan at its expected version, or none.
  async applyAmounts({ changes, mutationKey, delivery = null }) {
    const editable = await this.editableFilter();
    const current = await listCurrentPlans(this.db, true);
    const work = changes.map(change => {
      const plan = current.find(row => row.planKey === change.planKey);
      if (!plan || !editable(plan) || plan.version !== change.expectedVersion ||
          compare(decimal(change.dailyAmount), decimal('0.00000001')) < 0) {
        throw new TypeError('stale or foreign proposal member');
      }
      return { planKey: plan.planKey, expectedVersion: plan.version,
        nextState: { ...plan, dailyAmount: exact(decimal(change.dailyAmount)) } };
    });
    const staged = delivery && { ...delivery,
      reply: delivery.render(work.map(item => toDomain(item.nextState))) };
    const result = await applyGroupChanges(this.db, work, mutationKey, this.now(), staged);
    if (result !== 'CHANGED') throw new Error('proposal changed concurrently');
    const keys = new Set(work.map(item => item.planKey));
    return (await listCurrentPlans(this.db)).filter(plan => keys.has(plan.planKey))
      .map(toDomain);
  }

  async changeGroup({ assetType, delta, stop, excluded, mutationKey, delivery = null }) {
    const editable = await this.editableFilter();
    const replayKeys = await groupMutationPlans(this.db, mutationKey);
    if (replayKeys.length) {
      const keys = new Set(replayKeys);
      return (await listCurrentPlans(this.db)).filter(plan => keys.has(plan.planKey) &&
        editable(plan)).map(toDomain);
    }
    const current = (await listCurrentPlans(this.db, true)).filter(plan =>
      editable(plan) &&
      plan.instrument.assetType === assetType &&
      !excluded.includes(plan.instrument.ticker));
    const changes = current.map(plan => {
      let dailyAmount = plan.dailyAmount;
      if (!stop) {
        const candidate = add(decimal(plan.dailyAmount), decimal(delta));
        if (compare(candidate, decimal('0.00000001')) < 0) throw new TypeError('group planning amount must remain positive');
        dailyAmount = exact(candidate);
      }
      return { planKey: plan.planKey, expectedVersion: plan.version,
        nextState: { ...plan, dailyAmount, enabled: stop ? false : plan.enabled } };
    }).filter(item => stop || item.nextState.dailyAmount !== current.find(plan =>
      plan.planKey === item.planKey).dailyAmount);
    if (!changes.length) {
      if (delivery) await enqueueUpdate(this.db, delivery.updateId, delivery.destination,
        JSON.stringify({ text: delivery.render([]) }), delivery.now);
      return [];
    }
    const staged = delivery && { ...delivery,
      reply: delivery.render(changes.map(item => toDomain(item.nextState))) };
    const result = await applyGroupChanges(this.db, changes, mutationKey, this.now(), staged);
    if (result !== 'CHANGED') throw new Error('group changed concurrently');
    const keys = new Set(changes.map(item => item.planKey));
    return (await listCurrentPlans(this.db)).filter(plan => keys.has(plan.planKey))
      .map(toDomain);
  }

  async setThesis({ market, ticker, thesis, mutationKey, delivery = null }) {
    const plans = await this.listPlans(false);
    const matches = plans.filter(plan => match(plan.instrument, market, ticker));
    if (matches.length === 1) {
      market = matches[0].instrument.market;
      ticker = matches[0].instrument.ticker;
    }
    const previous = await currentThesis(this.db, market, ticker);
    const contentHash = [...new Uint8Array(await crypto.subtle.digest('SHA-256',
      new TextEncoder().encode(thesis)))].map(byte => byte.toString(16).padStart(2, '0')).join('');
    const revisionKey = `telegram:${market}:${ticker}:${contentHash.slice(0, 32)}`;
    const staged = delivery && { ...delivery, reply: delivery.render({ market, ticker }) };
    if (previous?.revision_key === revisionKey) {
      if (staged) await enqueueUpdate(this.db, staged.updateId, staged.destination,
        JSON.stringify({ text: staged.reply }), staged.now);
      return { market, ticker };
    }
    const status = await appendThesis(this.db, { market, ticker,
      currency: KR.has(market) ? 'KRW' : 'USD', text: thesis,
      revisionKey,
      mutationKey, expectedRevision: previous?.revision_no || 0,
      effectiveAt: this.now(), delivery: staged });
    if (status !== 'CHANGED') {
      const current = await currentThesis(this.db, market, ticker);
      if (current?.revision_key !== revisionKey) throw new Error('thesis changed concurrently');
      if (staged) await enqueueUpdate(this.db, staged.updateId, staged.destination,
        JSON.stringify({ text: staged.reply }), staged.now);
    }
    return { market, ticker };
  }

  async analyzePlans(plans, snapshot) {
    if (!this.marketProvider) return {};
    const unique = new Map();
    for (const plan of plans) if (!unique.has(stable(plan)) ||
        (unique.get(stable(plan)).targetWeightPct == null && plan.targetWeightPct != null)) {
      unique.set(stable(plan), plan);
    }
    const pairs = await Promise.all([...unique].map(async ([key, plan]) => {
      try {
        return [key, await analyzeWithProvider(this.marketProvider,
          { market: plan.instrument.market, ticker: plan.instrument.ticker,
            snapshot, targetWeightPct: plan.targetWeightPct })];
      } catch { return [key, null]; }
    }));
    return Object.fromEntries(pairs.filter(([, value]) => value !== null));
  }

  async refreshValuations() {
    if (!this.marketProvider || typeof this.marketProvider.getValuation !== 'function') {
      throw new Error('valuation provider unavailable');
    }
    const plans = await this.listPlans(true);
    this.marketProvider.cache?.clear?.('valuation:');
    const unique = new Map(plans.map(plan => [stable(plan), plan.instrument]));
    let successes = 0;
    let failed = 0;
    const items = [...unique.values()];
    for (let offset = 0; offset < items.length; offset += 4) {
      const results = await Promise.all(items.slice(offset, offset + 4).map(async item => {
        try { await this.marketProvider.getValuation(item.market, item.ticker); return true; }
        catch { return false; }
      }));
      successes += results.filter(Boolean).length;
      failed += results.filter(value => !value).length;
    }
    return { successes, failed };
  }

  async refreshTransactions({ startDate, endDate }) {
    if (!this.mainAccountFingerprint) throw new Error('main ledger account binding unavailable');
    const provider = this.providers.find(item => item.name === 'kiwoom_main');
    if (typeof provider?.getTransactions !== 'function') {
      throw new Error('read-only execution provider unavailable');
    }
    const result = await provider.getTransactions({ startDate, endDate });
    if (!result || result.broker !== 'kiwoom_main' || !Array.isArray(result.items) ||
        !['ok', 'partial', 'auth_failed', 'rate_limited', 'unavailable'].includes(result.status) ||
        result.historyScope !== 'SAME_DAY_ONLY') {
      throw new TypeError('invalid same-day execution result');
    }
    let imported = 0;
    for (const item of result.items) {
      if (item.broker !== provider.name || item.accountId !== provider.accountId) {
        throw new TypeError('execution account mismatch');
      }
      imported += Number(await recordTransactionOnce(this.db,
        { ...item, accountFingerprint: this.mainAccountFingerprint }));
    }
    return { status: result.status, imported, observed: result.items.length,
      historyScope: result.historyScope };
  }
}
