// Deterministic Python analysis core. Provider data must be normalized before entry.
import { add as exactAdd, compare, decimal, display, divide28, exact,
  multiply as exactMultiply, round28, subtract as exactSubtract } from './finance.mjs';

const add = (left, right) => round28(exactAdd(left, right));
const subtract = (left, right) => round28(exactSubtract(left, right));
const multiply = (left, right) => round28(exactMultiply(left, right));

const z = () => decimal('0');
const hundred = () => decimal('100');
const d = value => value === null || value === undefined ? null : decimal(value);
const rounded = value => decimal(display(value, 2).replaceAll(',', ''));
const sum = values => values.reduce((total, value) => add(total, value), z());
const clamp = value => compare(value, z()) < 0 ? z() :
  compare(value, hundred()) > 0 ? hundred() : value;
const linear = (value, low, high) => value === null || value === undefined ? null :
  clamp(multiply(divide28(subtract(d(value), d(low)), subtract(d(high), d(low))), hundred()));
const mean = values => {
  const available = values.filter(value => value !== null);
  return available.length ? divide28(sum(available), decimal(String(available.length))) : null;
};
const invert = value => value === null ? null : subtract(hundred(), value);
const nullable = value => value === null ? null : exact(value);

export function fundamentalComponent(data) {
  if (data === null) return null;
  let debt = null;
  if (data.totalCash != null && data.totalDebt != null) {
    debt = compare(d(data.totalDebt), z()) <= 0 ? hundred() :
      clamp(multiply(divide28(d(data.totalCash), d(data.totalDebt)), decimal('50')));
  }
  return nullable(mean([
    linear(data.revenueGrowth, '-0.10', '0.30'),
    linear(data.earningsGrowth, '-0.20', '0.40'),
    linear(data.grossMargin, '0.10', '0.70'),
    linear(data.operatingMargin, '0', '0.35'),
    linear(data.returnOnEquity, '0', '0.30'),
    data.freeCashFlow == null ? null :
      compare(d(data.freeCashFlow), z()) > 0 ? d('80') : d('20'), debt,
  ]));
}

export function growthComponent(data) {
  if (data === null) return null;
  return nullable(mean([linear(data.revenueGrowth, '-0.05', '0.35'),
    linear(data.earningsGrowth, '-0.10', '0.50')]));
}

export function valuationComponent(data) {
  if (data === null) return null;
  const metrics = [];
  if (data.forwardPe != null) metrics.push(invert(linear(data.forwardPe, '10', '60')));
  if (data.peg != null) metrics.push(invert(linear(data.peg, '0.5', '3')));
  if (data.enterpriseToEbitda != null) {
    metrics.push(invert(linear(data.enterpriseToEbitda, '5', '35')));
  }
  if (data.freeCashFlowYield != null) {
    metrics.push(linear(data.freeCashFlowYield, '0', '0.08'));
  }
  if (data.forwardPe != null && data.historicalForwardPeMedian != null &&
      compare(d(data.historicalForwardPeMedian), z()) !== 0) {
    metrics.push(invert(linear(nullable(divide28(d(data.forwardPe),
      d(data.historicalForwardPeMedian))), '0.7', '1.4')));
  }
  if (data.dilutionPct != null) metrics.push(invert(linear(data.dilutionPct, '0', '10')));
  return nullable(mean(metrics));
}

export function safetyComponent(data) {
  if (data === null) return null;
  const metrics = [];
  if (data.freeCashFlow != null) metrics.push(compare(d(data.freeCashFlow), z()) > 0 ? d('85') : d('15'));
  if (data.totalCash != null && data.totalDebt != null) {
    metrics.push(compare(d(data.totalDebt), z()) <= 0 ? hundred() :
      clamp(multiply(divide28(d(data.totalCash), d(data.totalDebt)), d('60'))));
  }
  if (data.debtToEquity != null) metrics.push(invert(linear(data.debtToEquity, '20', '250')));
  return nullable(mean(metrics));
}

const companyWeights = [['fundamental', '35'], ['valuation', '25'], ['growth', '20'],
  ['moat', '10'], ['safety', '10']];
const portfolioWeights = [['target_alignment', '50'], ['single_stock_capacity', '20'],
  ['sector_capacity', '15'], ['currency_capacity', '10'], ['correlation', '5']];

function weightedScore(values, weights, extraWarnings = []) {
  const components = weights.map(([name, weight]) => ({ name,
    score: values[name] ?? null, weight, reasons: [] }));
  for (const component of components) if (component.score !== null &&
      (compare(d(component.score), z()) < 0 || compare(d(component.score), hundred()) > 0)) {
    throw new TypeError('score outside 0..100');
  }
  const covered = components.filter(component => component.score !== null);
  const coveredWeight = sum(covered.map(component => d(component.weight)));
  const coverage = rounded(coveredWeight);
  const warnings = [...extraWarnings];
  if (compare(coverage, d('70')) < 0) {
    warnings.push(`score_withheld:coverage_${display(coverage, 2)}_below_70.00`);
    return { score: null, coveragePct: exact(coverage), isEligible: false,
      components, warnings };
  }
  const weightedSum = sum(covered.map(component => multiply(d(component.score), d(component.weight))));
  const score = rounded(divide28(weightedSum, coveredWeight));
  if (compare(coveredWeight, hundred()) < 0) {
    warnings.push('score_renormalized_over_available_components');
  }
  return { score: exact(score), coveragePct: exact(coverage), isEligible: true,
    components, warnings };
}

export function companyScore(values) { return weightedScore(values, companyWeights); }

export function portfolioScore(values) {
  const { complete, ...parts } = values;
  const result = weightedScore(parts, portfolioWeights,
    complete ? [] : ['score_withheld:portfolio_snapshot_incomplete']);
  return complete ? result : { ...result, score: null, isEligible: false };
}

const warningTypes = new Set(['accounting_issue', 'liquidity_crisis', 'going_concern',
  'fraud', 'major_customer_loss', 'structural_business_deterioration',
  'investment_thesis_breakdown']);
const decision = value => compare(value, d('85')) >= 0 ? 'aggressive_buy' :
  compare(value, d('70')) >= 0 ? 'continue_buy' :
  compare(value, d('55')) >= 0 ? 'maintain' :
  compare(value, d('40')) >= 0 ? 'reduce' : 'review_stop';

export function valuationDecision(score, hardWarnings = []) {
  const warnings = [...new Set(hardWarnings)];
  if (warnings.some(item => !warningTypes.has(item))) throw new TypeError('unsupported hard warning');
  if (score !== null && (compare(d(score), z()) < 0 || compare(d(score), hundred()) > 0)) {
    throw new TypeError('valuation score outside 0..100');
  }
  if (score === null) return { score: null, companyScore: null, portfolioScore: null,
    decision: warnings.length ? 'review_required' : 'unavailable', isActionable: false,
    hardWarnings: warnings, warnings: ['valuation_decision_withheld:data_unavailable'] };
  const value = exact(rounded(d(score)));
  return { score: value, companyScore: value, portfolioScore: null,
    decision: warnings.length ? 'review_required' : decision(d(value)),
    isActionable: !warnings.length, hardWarnings: warnings,
    warnings: warnings.length ? ['valuation_only_decision', 'hard_warning_override'] :
      ['valuation_only_decision'] };
}

export function buyCapacityScore(currentWeight, targetWeight) {
  const current = d(currentWeight);
  const target = d(targetWeight);
  if (current === null || target === null || compare(current, z()) < 0 ||
      compare(current, hundred()) > 0 || compare(target, z()) < 0 ||
      compare(target, hundred()) > 0) throw new TypeError('invalid exposure');
  if (compare(target, z()) === 0) return '0';
  if (compare(current, target) <= 0) return '100';
  const overshoot = divide28(subtract(current, target), target);
  return exact(rounded(clamp(multiply(subtract(d('1'), overshoot), hundred()))));
}

export function recurringSuitability(trend) {
  if (trend === null) return { score: null, reasons: ['장기 가격 이력 확인 불가'] };
  const metrics = [];
  if (trend.cagr3y != null) metrics.push(linear(trend.cagr3y, '-0.05', '0.15'));
  if (trend.return1y != null) metrics.push(linear(trend.return1y, '-0.20', '0.20'));
  if (trend.maxDrawdown3y != null) {
    metrics.push(linear(trend.maxDrawdown3y, '-0.60', '-0.15'));
  }
  if (trend.annualizedVolatility1y != null) {
    metrics.push(invert(linear(trend.annualizedVolatility1y, '0.15', '0.50')));
  }
  if (trend.above200dAverage != null) {
    metrics.push(trend.above200dAverage ? hundred() : d('30'));
  }
  if (metrics.length < 3) return { score: null, reasons: ['장기추세 지표 커버리지 부족'] };
  const reasons = [];
  const signed = value => `${compare(d(value), z()) >= 0 ? '+' : ''}${display(d(value), 2)}%`;
  if (trend.cagr3y != null) reasons.push(`3년 CAGR ${signed(nullable(multiply(d(trend.cagr3y), hundred())))}`);
  if (trend.maxDrawdown3y != null) {
    reasons.push(`최대낙폭 ${display(multiply(d(trend.maxDrawdown3y), hundred()), 2)}%`);
  }
  reasons.push(trend.above200dAverage ? '200일선 상회' : '200일선 하회');
  return { score: exact(rounded(mean(metrics))), reasons };
}
