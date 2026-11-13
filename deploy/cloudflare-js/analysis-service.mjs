// Network-free analysis composition. Any provider is injected and must be read-only.
import { add, compare, decimal, exact, multiply } from './finance.mjs';
import { fixed } from './analysis-report.mjs';
import { buyCapacityScore, companyScore, fundamentalComponent, growthComponent,
  portfolioScore, recurringSuitability, safetyComponent, valuationComponent,
  valuationDecision } from './scoring.mjs';

const US = new Set(['US', 'USA', 'NASDAQ', 'NAS', 'NYSE', 'AMEX', 'ARCA', 'NYSEARCA']);
const KEYWORDS = new Map([
  ['ACCOUNTING', ['accounting', 'restatement', 'auditor', '회계', '감사 의견']],
  ['LIQUIDITY', ['bankruptcy', 'default', 'liquidity crisis', '파산', '채무불이행', '유동성']],
  ['CUSTOMER', ['contract cancelled', 'customer loss', '계약 해지', '고객사 이탈']],
  ['GUIDANCE', ['guidance', 'forecast cut', 'forecast raised', '가이던스', '전망 하향', '전망 상향']],
  ['EARNINGS', ['earnings', 'revenue', 'profit warning', '실적', '매출', '이익 경고']],
  ['REGULATORY', ['investigation', 'antitrust', 'sanction', '조사', '독점', '제재']],
  ['CAPITAL', ['share offering', 'debt offering', 'capital raise', '유상증자', '회사채', '자본 조달']],
]);

function compatible(left, market, ticker) {
  if (left.ticker !== ticker) return false;
  return left.market === market ||
    (['US', 'USA'].includes(left.market) && US.has(market)) ||
    (['US', 'USA'].includes(market) && US.has(left.market));
}

function assessNews(items) {
  return items.map(item => {
    const words = `${item.title} ${item.summary || ''}`.toLocaleLowerCase();
    const hits = [...KEYWORDS.keys()].filter(category =>
      KEYWORDS.get(category).some(keyword => words.includes(keyword.toLocaleLowerCase())));
    return { ...item, fundamentalImpact: hits.length ? 'REVIEW_REQUIRED' : 'NO_DIRECT_EVIDENCE',
      impactReasons: hits.length ? hits.slice(0, 3).map(category =>
        `${category} 관련 사건 가능성—원문과 1차 자료 확인 필요`) :
        ['정량 펀더멘털 영향 근거가 제목/요약에서 확인되지 않음'] };
  });
}

export function analyzeNormalized({ market, ticker, snapshot, targetWeightPct = null,
  price = null, fundamentals = null, valuation = null, earnings = null,
  news = [], trend = null, errors = {}, hardWarnings = [] }) {
  if (!snapshot || !Array.isArray(snapshot.positions) || !Array.isArray(news) ||
      typeof market !== 'string' || typeof ticker !== 'string') {
    throw new TypeError('invalid normalized analysis input');
  }
  const valuationValue = valuationComponent(valuation);
  const company = companyScore({ fundamental: fundamentalComponent(fundamentals),
    valuation: valuationValue, growth: growthComponent(fundamentals),
    moat: null, safety: safetyComponent(fundamentals) });
  const match = snapshot.positions.find(position => compatible(position.instrument, market, ticker));
  const weightsAvailable = snapshot.isComplete && snapshot.totalMarketValue !== null &&
    compare(decimal(snapshot.totalMarketValue), decimal('0')) > 0;
  const currentWeight = match ? match.weightPct : weightsAvailable ? '0' : null;
  const targetAlignment = currentWeight !== null && targetWeightPct !== null ?
    buyCapacityScore(currentWeight, targetWeightPct) : null;
  const limit = targetWeightPct !== null &&
    compare(decimal(targetWeightPct), decimal('10')) > 0 ? targetWeightPct : '10';
  const singleCapacity = currentWeight !== null ? buyCapacityScore(currentWeight, limit) : null;
  let currencyExposure = null;
  if (weightsAvailable && match) {
    currencyExposure = exact(snapshot.positions.filter(position =>
      position.instrument.currency === match.instrument.currency).reduce((total, position) =>
      add(total, decimal(position.weightPct || '0')), decimal('0')));
  }
  const currencyCapacity = currencyExposure === null ? null :
    buyCapacityScore(currencyExposure, '80');
  const portfolio = portfolioScore({ complete: snapshot.isComplete,
    target_alignment: targetAlignment, single_stock_capacity: singleCapacity,
    sector_capacity: null, currency_capacity: currencyCapacity, correlation: null });
  const final = valuationDecision(valuationValue, hardWarnings);
  const reasons = [];
  if (fundamentals?.revenueGrowth != null) reasons.push(
    `매출 성장률 ${fixed(exact(multiply(decimal(fundamentals.revenueGrowth), decimal('100'))), 1)}` +
    `%(${fundamentals.provider}, 기준 ${fundamentals.asOf.slice(0, 10)})`);
  if (valuation?.forwardPe != null) reasons.push(
    `Forward PER ${fixed(valuation.forwardPe, 2)}배를 성장·현금흐름 지표와 함께 평가함`);
  if (currentWeight !== null && targetWeightPct !== null) reasons.push(
    `현재 비중 ${fixed(currentWeight, 2)}%가 목표 ${fixed(targetWeightPct, 2)}% ` +
    (compare(decimal(currentWeight), decimal(targetWeightPct)) <= 0 ? '이하' : '초과'));
  if (!reasons.length) reasons.push('판단에 필요한 정량 데이터가 충분하지 않음');
  if (reasons.length < 3) reasons.push(
    `기업 점수 ${company.score === null ? '커버리지 미달로 보류' : `${fixed(company.score, 2)}점`}` +
    `(정량 데이터 커버리지 ${fixed(company.coveragePct, 2)}%)`);
  if (reasons.length < 3) reasons.push(
    `포트폴리오 점수 ${portfolio.score === null ? '불완전 데이터로 보류' :
      `${fixed(portfolio.score, 2)}점`}` +
    `(스냅샷 ${snapshot.isComplete ? '완전' : '부분'})`);
  const warnings = ['price', 'fundamental', 'valuation', 'earnings', 'news', 'trend']
    .filter(name => errors[name]).map(name => `${name}:${errors[name]}`);
  return { market: market.toUpperCase(), ticker: ticker.toUpperCase(), price,
    fundamentals, valuation, earnings, news: assessNews(news), trend,
    recurringSuitability: recurringSuitability(trend), companyScore: company,
    portfolioScore: portfolio, finalScore: final, reasons, warnings };
}

export async function analyzeWithProvider(provider, options) {
  const calls = [
    ['price', 'getPrice'], ['fundamental', 'getFundamentals'],
    ['valuation', 'getValuation'], ['earnings', 'getEarnings'],
    ['news', 'getNews'], ['trend', 'getTrend'],
  ];
  const entries = await Promise.all(calls.map(async ([key, method]) => {
    if (typeof provider?.[method] !== 'function') return [key, null, 'NotSupported'];
    try {
      const value = await provider[method](options.market, options.ticker,
        ...(key === 'news' ? [10] : []));
      return [key, value, null];
    } catch (error) { return [key, null, error?.name || 'ProviderUnavailable']; }
  }));
  const rows = Object.fromEntries(entries.map(([key, value]) => [key, value]));
  const errors = Object.fromEntries(entries.filter(([, , error]) => error)
    .map(([key, , error]) => [key, error]));
  return analyzeNormalized({ ...options, price: rows.price,
    fundamentals: rows.fundamental, valuation: rows.valuation,
    earnings: rows.earnings, news: rows.news || [], trend: rows.trend, errors });
}
