// No-analysis recurring report path. Analysis-bearing plans remain an explicit gate.
import { add, compare, decimal, display, divide28, exact, multiply } from './finance.mjs';
import { html, money } from './assets-report.mjs';

const zero = () => decimal('0');
// Tickers added on 2026-11-13 follow Yahoo Finance sector/industry (equities)
// or the fund's published name and type (ETFs); see docs/exposure-classification.md.
const exposureGroups = [
  ['미국 대형 기술·성장 ETF', 'QQQ QLD MAGS VGT'],
  ['미국 광범위 지수', 'VOO SSO SPMO IWM VTWO VUG GRNY'],
  ['반도체', 'USD SOXQ SOXX ASX TSM KLAC LRCX ASML MPWR STM MRVL NVDA AMD AVGO MU ALGM TXN ARM QCOM AMAT'],
  ['금융', 'XLF CME COIN'], ['경기소비재', 'AMZN TSLA'],
  ['산업재', 'PAVE CAT RRX CGNX JCI TKR XYL TT ETN ROK'],
  ['산업재·기술', 'NOVT'], ['데이터저장·기술', 'STX WDC SNDK'],
  ['광통신·기술', 'COHR LITE GLW'], ['커뮤니케이션', 'GOOG LUMN'],
  ['양자컴퓨팅·기술', 'QNT'], ['청정에너지', 'BE FLNC'], ['소재', 'ECL'],
  ['헬스케어', 'XLV LLY UNH'], ['소프트웨어', 'PLTR CRWD PANW'], ['전력인프라', 'GEV VRT'],
  ['옵션인컴·배당 ETF', 'QDVO QDTE QYLD QQQI GPIQ JEPQ SCHD DIVO VYM DGRO JEPI VIG FEPI'],
  ['금', 'GLDM'], ['에너지', 'PBR.A'], ['미국 국채', 'IEF TLT'],
];
const exposures = new Map(exposureGroups.flatMap(([label, tickers]) =>
  tickers.split(' ').map(ticker => [ticker, label])));
const usMarkets = new Set(['US', 'NASDAQ', 'NYSE', 'AMEX']);
const etfTickers = new Set(('QQQ QLD MAGS VGT VOO SSO SPMO IWM VTWO VUG ' +
  'USD SOXQ SOXX XLF PAVE XLV QDVO QDTE QYLD QQQI GPIQ JEPQ SCHD DIVO VYM DGRO GLDM JEPI VIG IEF TLT')
  .split(' '));
// Funds that may be registered by hand as EQUITY; either stored type is accepted.
const eitherType = new Set(['FEPI', 'GRNY']);
const weekdayNames = ['월', '화', '수', '목', '금', '토', '일'];

const stableId = plan => `${plan.instrument.market}:${plan.instrument.ticker}`;
const fullId = plan => `${stableId(plan)}:${plan.instrument.assetType}:${plan.instrument.currency}`;
const weekly = plan => multiply(decimal(plan.dailyAmount),
  decimal(String(new Set(plan.scheduledWeekdays).size)));
export const UNCLASSIFIED = '분류 확인 불가';
export const exposureLabels = [...exposureGroups.map(([label]) => label), UNCLASSIFIED];
export const exposure = (plan, analysis) => {
  const ticker = plan.instrument.ticker;
  const mapped = exposures.get(ticker);
  const expectedType = etfTickers.has(ticker) ? 'ETF' : 'EQUITY';
  if (mapped && usMarkets.has(plan.instrument.market) &&
      (plan.instrument.assetType === expectedType || eitherType.has(ticker))) return mapped;
  return (plan.instrument.assetType === 'ETF' ? null : analysis?.fundamentals?.sector) ||
    '분류 확인 불가';
};
const percent = (numerator, denominator, places) =>
  `${display(multiply(divide28(numerator, denominator), decimal('100')), places)}`;

function schedule(plan) {
  const days = [...new Set(plan.scheduledWeekdays)].sort((a, b) => a - b);
  if (!days.length) return '예정 요일 없음';
  if (days.join(',') === '0,1,2,3,4') return '평일 매일';
  if (days.join(',') === '0') return '매주 월요일';
  if (days.join(',') === '4') return '매주 금요일';
  return '매주 ' + days.map(day => weekdayNames[day]).join('·');
}

function valuationScore(analysis) {
  return analysis?.companyScore?.components?.find(component =>
    component.name === 'valuation')?.score ?? null;
}

function recommended(plan) {
  const current = decimal(plan.dailyAmount);
  if (!plan.enabled) return { amount: current, action: '비활성 유지',
    reasons: ['비활성 계획은 금액 조정 대상에서 제외'] };
  return { amount: current, action: '유지',
    reasons: ['사용자 목표 미설정 · 현재 금액 유지'] };
}

// `signals` is an optional Map of plan id to cached public scores.
export function formatRecurring(plans, analyses = {}, signals = new Map()) {
  if (!plans.length) return '🌱 <b>매일모으기</b>\n\n등록된 종목이 없습니다.\n' +
    '<code>/recurring_add AMD 20USD 8%</code>로 로컬 계획을 등록할 수 있습니다.';
  const totals = new Map();
  const counts = new Map();
  const byExposure = new Map();
  const byWeekday = new Map();
  for (const plan of plans) {
    if (!plan.enabled) continue;
    const currency = plan.currency;
    const amount = weekly(plan);
    totals.set(currency, add(totals.get(currency) || zero(), amount));
    counts.set(currency, (counts.get(currency) || 0) + 1);
    if (!byWeekday.has(currency)) byWeekday.set(currency, new Map());
    for (const day of new Set(plan.scheduledWeekdays)) {
      const previous = byWeekday.get(currency).get(day) || zero();
      byWeekday.get(currency).set(day, add(previous, decimal(plan.dailyAmount)));
    }
    if (!byExposure.has(currency)) byExposure.set(currency, new Map());
    const group = exposure(plan, analyses[fullId(plan)]);
    byExposure.get(currency).set(group, add(byExposure.get(currency).get(group) || zero(), amount));
  }
  const lines = ['🌱 <b>매일모으기</b>'];
  const order = plan => ({ ETF: 0, EQUITY: 1 }[plan.instrument.assetType] ?? 2);
  const text = (left, right) => left < right ? -1 : left > right ? 1 : 0;
  const sorted = [...plans].sort((left, right) => order(left) - order(right) ||
    text(left.instrument.ticker, right.instrument.ticker) ||
    text(left.instrument.market, right.instrument.market) ||
    text(left.broker || '', right.broker || ''));
  const recommendations = new Map(plans.map(plan => [plan, recommended(plan)]));
  let lastType = null;
  let analysed = 0;
  for (const plan of sorted) {
    if (plan.instrument.assetType !== lastType) {
      lastType = plan.instrument.assetType;
      lines.push('', `📂 <b>${{ ETF: 'ETF', EQUITY: '개별주' }[lastType] || '기타'}</b>`);
    }
    const analysis = analyses[fullId(plan)];
    if (analysis && (!analysis.companyScore?.components ||
        !analysis.recurringSuitability || !analysis.finalScore)) {
      throw new TypeError('incomplete recurring analysis');
    }
    lines.push('', `<b>${html(stableId(plan))}</b> · ${html(exposure(plan, analysis))}`);
    const total = totals.get(plan.currency) || zero();
    const weight = !plan.enabled ? '중단' : compare(total, zero()) > 0 ?
      `비중 ${percent(weekly(plan), total, 2)}%` : '비중 확인 불가';
    lines.push(`${schedule(plan)} ${money(plan.dailyAmount, plan.currency)} · ${weight}`);
    if (analysis) {
      analysed += 1;
      const valuation = valuationScore(analysis);
      const score = value => value == null ? '보류' : display(decimal(value), 2).replaceAll(',', '');
      lines.push(`기업점수 ${score(analysis.companyScore.score)} / 100`,
        `Valuation ${score(valuation)} / 100`,
        `장기추세 적합성 ${score(analysis.recurringSuitability.score)} / 100`);
      if (analysis.recurringSuitability.reasons?.length) {
        lines.push('추세 근거: ' + html(analysis.recurringSuitability.reasons.join(' · ')));
      }
    }
    const signal = analysis ? null : signals.get(fullId(plan));
    if (signal && (signal.valuationScore != null || signal.trendScore != null)) {
      analysed += 1;
      const shown = value => display(decimal(value), 1);
      lines.push([signal.valuationScore != null ?
        `Valuation ${shown(signal.valuationScore)} / 100` : null,
      signal.trendScore != null ? `장기추세 ${shown(signal.trendScore)} / 100` : null]
        .filter(Boolean).join(' · '));
    }
    const rec = recommendations.get(plan);
    if (rec.action === '감액' || rec.action === '증액') {
      lines.push(`1회 금액 비교: ${money(plan.dailyAmount, plan.currency)} → ` +
        `${money(exact(rec.amount), plan.currency)} (${rec.action})`,
      '조정 근거: ' + rec.reasons.map(html).join(' · '));
    }
    if (plan.investmentThesis) lines.push(`Thesis: ${html(plan.investmentThesis.slice(0, 180))}`);
  }
  if (byExposure.size) {
    lines.push('', '📌 <b>최종 요약 · 주간 환산</b>');
    for (const currency of [...byExposure.keys()].sort()) {
      const total = totals.get(currency);
      lines.push('', `<b>주간 예정 투입 ${money(exact(total), currency)}</b>`);
      const daily = [...byWeekday.get(currency)].sort((a, b) => a[0] - b[0])
        .map(([day, amount]) => `${weekdayNames[day]} ${money(exact(amount), currency)}`)
        .join(' · ');
      lines.push(`요일별 1회 합계 ${daily || '예정 요일 없음'}`);
      lines.push(`월평균 환산 ${money(exact(divide28(multiply(total,
        decimal('52')), decimal('12'))), currency)}`);
      const count = counts.get(currency);
      lines.push(`활성 ${count}종목 · 균등 기준 ` +
        `${percent(decimal('1'), decimal(String(count)), 2)}%`);
      lines.push('노출군별 예정 투입 비중 (같은 통화의 주간 합계 기준)');
      for (const [group, amount] of [...byExposure.get(currency)].sort((a, b) =>
        -compare(a[1], b[1]))) {
        const pct = compare(total, zero()) > 0 ? percent(amount, total, 1) : null;
        const warning = compare(multiply(amount, decimal('100')),
          multiply(total, decimal('25'))) >= 0;
        lines.push(`• ${html(group)} ${pct === null ? '확인 불가' : `${pct}%`}` +
          `${pct !== null && warning ? ' ⚠️' : ''}`);
      }
      lines.push('⚠️는 기존 참고 경고선 25% 이상이며 목표 비중이 아닙니다.');
    }
  }
  lines.push('', '요일별·주간은 설정된 요일마다 1회 실행을 가정하며 휴장일은 차감하지 않습니다.',
    '월평균은 주간 × 52 ÷ 12이며 실제 월별 거래일 합계가 아닙니다.',
    '비중은 신규 모으기 투입액 기준이며 통화별로 따로 계산합니다. ETF는 한 노출군으로만 분류합니다.');
  if (!analysed) lines.push('종목 분석 데이터가 없어 점수와 금액 추천은 표시하지 않습니다.');
  const changed = action => sorted.filter(plan => recommendations.get(plan).action === action);
  for (const [action, title, label] of [['감액', '🔻 <b>줄여야 할 종목</b>', '이유'],
    ['증액', '🔺 <b>늘릴 수 있는 종목</b>', '근거']]) {
    const items = changed(action);
    if (!items.length) continue;
    lines.push('', title);
    for (const plan of items) {
      const rec = recommendations.get(plan);
      lines.push(`• <b>${html(stableId(plan))}</b> ${money(plan.dailyAmount, plan.currency)} → ` +
        money(exact(rec.amount), plan.currency), `  ${label}: ` + rec.reasons.map(html).join(' · '));
    }
  }
  return lines.join('\n');
}
