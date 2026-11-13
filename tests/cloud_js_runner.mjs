// Synthetic parity bridge only; never reads operating files.
import { readFileSync } from 'node:fs';
import { admittedUpdateId, dueJobs } from '../deploy/cloudflare-js/boundary.mjs';
import { aggregateCoverage } from '../deploy/cloudflare-js/aggregation.mjs';
import { formatAssets } from '../deploy/cloudflare-js/assets-report.mjs';
import { formatAccounts, formatPortfolio } from '../deploy/cloudflare-js/portfolio-report.mjs';
import { buildPeriodicReport } from '../deploy/cloudflare-js/periodic-report.mjs';
import { stockReply } from '../deploy/cloudflare-js/dynamic-commands.mjs';
import { suggestNewMoneyRebalance, formatRebalance } from '../deploy/cloudflare-js/rebalance.mjs';
import { formatRecurring } from '../deploy/cloudflare-js/recurring-report.mjs';
import { fundamentalComponent, growthComponent, valuationComponent, safetyComponent,
  companyScore, portfolioScore, valuationDecision, buyCapacityScore,
  recurringSuitability } from
  '../deploy/cloudflare-js/scoring.mjs';
import { formatAnalysis } from '../deploy/cloudflare-js/analysis-report.mjs';
import { analyzeNormalized } from '../deploy/cloudflare-js/analysis-service.mjs';
import { START, HELP } from '../deploy/cloudflare-js/static-commands.mjs';
import { decimal, display, divide28, evaluateRecurringBuy, exact, weeklyBudget } from '../deploy/cloudflare-js/finance.mjs';

const fixture = JSON.parse(readFileSync(0, 'utf8'));
const results = {
  money: fixture.money.map(([value, places]) => display(decimal(value), places)),
  divisions: fixture.divisions.map(([left, right]) => exact(divide28(decimal(left), decimal(right)))),
  budgets: fixture.budgets.map(([plans, fx]) => {
    const result = weeklyBudget(plans, fx);
    if (result === null) return null;
    return exact(result);
  }),
  schedules: fixture.schedules.map(args => dueJobs(...args)),
  admissions: fixture.admissions.map(({ headers, update, secret, allowed }) =>
    admittedUpdateId(headers, new TextEncoder().encode(JSON.stringify(update)), secret, allowed)),
  ledgers: fixture.ledgers.map(({ plan, transactions, options }) => {
    const result = evaluateRecurringBuy(plan, transactions, options);
    return { ...result, actual: display(result.actual, 4),
      remaining: display(result.remaining, 4) };
  }),
  aggregations: fixture.aggregations.map(aggregateCoverage),
  assetReports: fixture.aggregations.map(input => formatAssets(aggregateCoverage(input), {
    positionResults: input.positionResults, cashResults: input.cashResults || [],
    collectedAt: '2026-02-02T01:00:00Z', fxQuote: input.fxQuotes?.[0] || null,
  })),
  portfolioReports: fixture.aggregations.map(input => formatPortfolio(aggregateCoverage(input))),
  accountReports: fixture.accountReports.map(input => formatAccounts(input.results,
    '2026-02-02T01:00:00Z', input.isComplete)),
  periodicReports: fixture.aggregations.map(input => {
    const snapshot = aggregateCoverage(input);
    const refresh = { positionResults: input.positionResults,
      cashResults: input.cashResults || [], collectedAt: '2026-02-02T01:00:00Z',
      fxQuote: input.fxQuotes?.[0] || null };
    return { daily: buildPeriodicReport('daily', snapshot, refresh),
      weekly: buildPeriodicReport('weekly', snapshot, refresh) };
  }),
  staticCommands: { start: START, help: HELP },
  stockReplies: await Promise.all(fixture.stockCases.map(caseData => stockReply(
    caseData.raw,
    caseData.providerConfigured ? { getPrice: async () => {
      if (caseData.quote === null) throw new Error('synthetic unavailable');
      return caseData.quote;
    } } : null,
  ))),
  rebalanceCases: (fixture.rebalanceCases || []).map(caseData => {
    const plan = suggestNewMoneyRebalance(aggregateCoverage(caseData.aggregation),
      caseData.targets, caseData.newMoney);
    return { plan, report: formatRebalance(plan) };
  }),
  recurringReports: (fixture.recurringCases || []).map(caseData =>
    formatRecurring(caseData.plans, caseData.analyses || {})),
  periodicPlanReports: (fixture.periodicPlanCases || []).map(caseData => {
    const snapshot = aggregateCoverage(caseData.aggregation);
    const refresh = { positionResults: caseData.aggregation.positionResults,
      cashResults: caseData.aggregation.cashResults || [],
      collectedAt: '2026-02-02T01:00:00Z', fxQuote: caseData.aggregation.fxQuotes?.[0] || null };
    return { daily: buildPeriodicReport('daily', snapshot, refresh, caseData.plans,
      caseData.analyses || {}),
    weekly: buildPeriodicReport('weekly', snapshot, refresh, caseData.plans,
      caseData.analyses || {}) };
  }),
  scoringCases: (fixture.scoringCases || []).map(caseData => ({
    fundamental: fundamentalComponent(caseData.fundamentals),
    growth: growthComponent(caseData.fundamentals),
    valuation: valuationComponent(caseData.valuation),
    safety: safetyComponent(caseData.fundamentals),
    company: companyScore(caseData.company),
    portfolio: portfolioScore(caseData.portfolio),
    decision: valuationDecision(caseData.valuationScore, caseData.hardWarnings),
    capacity: buyCapacityScore(caseData.currentWeight, caseData.targetWeight),
    suitability: recurringSuitability(caseData.trend || null),
  })),
  analysisReports: (fixture.analysisCases || []).map(formatAnalysis),
  analysisComputations: (fixture.analysisComputations || []).map(input => {
    const result = analyzeNormalized({ ...input, snapshot: aggregateCoverage(input.aggregation) });
    return { result, report: formatAnalysis(result) };
  }),
};
process.stdout.write(JSON.stringify(results));
