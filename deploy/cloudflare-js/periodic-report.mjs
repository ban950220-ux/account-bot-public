// Pure report composition; real provider/analysis delivery remains outside this module.
import { formatAssets } from './assets-report.mjs';
import { formatPortfolio } from './portfolio-report.mjs';
import { formatRecurring } from './recurring-report.mjs';
import { suggestNewMoneyRebalance } from './rebalance.mjs';
import { compare, decimal, display, exact, multiply, weeklyBudget } from './finance.mjs';
import { html, money } from './assets-report.mjs';
import { fixed } from './analysis-report.mjs';

const EMPTY_RECURRING = '🌱 <b>매일모으기</b>\n\n등록된 종목이 없습니다.\n' +
  '<code>/recurring_add AMD 20USD 8%</code>로 로컬 계획을 등록할 수 있습니다.';

export function buildPeriodicReport(type, snapshot, refresh, plans = [], analyses = {},
  signals = new Map()) {
  const assets = formatAssets(snapshot, refresh);
  if (type === 'daily') return [
    '📊 <b>Daily Portfolio Report</b>', '', assets, '', '━━━━━━━━━━', '',
    plans.length ? formatRecurring(plans, analyses, signals) : EMPTY_RECURRING,
    '', '━━━━━━━━━━', '', '⚠️ <b>주요 변화</b>', '',
    '실적·뉴스 변화는 확인된 데이터만 표시하며, 단기 가격 움직임은 Thesis 변화로 간주하지 않습니다.',
    '', snapshot.isComplete ? '특별 대응 여부는 위 적립 종목별 근거를 확인하세요.' :
      '오늘은 계좌 데이터가 불완전해 Portfolio 판단을 보류합니다.',
  ].join('\n');
  if (type === 'weekly') {
    const lines = [
    '📅 <b>Weekly Portfolio Report</b>', '', assets, '', '━━━━━━━━━━', '',
    formatPortfolio(snapshot), '', '━━━━━━━━━━', '', '📈 <b>성과</b>',
    '1W/YTD 수익률은 완전한 과거 snapshot과 입출금 조정 이력이 쌓인 뒤 제공합니다.',
    '', '━━━━━━━━━━', '', '🧭 <b>Thesis·Valuation</b>'];
    if (!plans.length) lines.push('', '등록된 적립 종목 없음');
    const decisions = new Map([['aggressive_buy', '적극 증액'],
      ['continue_buy', '증액'], ['maintain', '유지'], ['reduce', '감액'],
      ['review_stop', '중단 검토'], ['review_required', '중요 경고 재검토'],
      ['unavailable', 'Valuation 데이터 부족으로 판단 보류']]);
    let compact = false;
    for (const plan of plans) {
      const identity = `${plan.instrument.market}:${plan.instrument.ticker}`;
      const full = `${identity}:${plan.instrument.assetType}:${plan.instrument.currency}`;
      const analysis = analyses[full];
      if (!analysis) {
        // Cached public scores stand in when no full analysis provider is bound.
        const signal = signals.get(full);
        const shown = value => display(decimal(value), 1);
        const scores = [signal?.valuationScore != null ?
          `Valuation ${shown(signal.valuationScore)}` : null,
        signal?.trendScore != null ? `장기추세 ${shown(signal.trendScore)}` : null]
          .filter(Boolean).join(' · ');
        if (scores) {
          if (!compact) lines.push('');
          lines.push(`<b>${html(identity)}</b> ${scores}`);
          compact = true;
          continue;
        }
        compact = false;
        lines.push('', `<b>${html(identity)}</b>`, '분석 데이터 부족');
        continue;
      }
      compact = false;
      if (analysis.companyScore.components.length < 5 ||
          !decisions.has(analysis.finalScore.decision)) {
        throw new TypeError('incomplete weekly analysis');
      }
      const valuation = analysis.companyScore.components.find(component =>
        component.name === 'valuation')?.score ?? null;
      const safety = analysis.companyScore.components[4].score;
      lines.push('', `<b>${html(identity)}</b>`,
        `주식봇 Valuation ${valuation === null ? '보류' : valuation}`,
        `결론 ${decisions.get(analysis.finalScore.decision)}`,
        `장기추세 적합성 ${analysis.recurringSuitability.score ?? '보류'}`);
      if (plan.investmentThesis) lines.push(`Thesis: ${html(plan.investmentThesis.slice(0, 240))}`);
      const v = analysis.valuation;
      if (v?.forwardPe != null) lines.push(`금요일 종가 기준 Forward PER ${fixed(v.forwardPe, 2)}x` +
        (v.historicalForwardPeMedian != null ?
          ` · 과거 median ${fixed(v.historicalForwardPeMedian, 2)}x` :
          ' · 과거 median 미제공'));
      if (analysis.earnings) {
        const e = analysis.earnings;
        lines.push(`최근 실적 ${html(e.period || '확인 분기')}` +
          (e.revenueYoy != null ?
            ` · Revenue YoY ${compare(decimal(e.revenueYoy), decimal('0')) >= 0 ? '+' : ''}` +
            `${fixed(exact(multiply(decimal(e.revenueYoy), decimal('100'))), 2)}%` : ''));
      }
      const news = (analysis.news || []).filter(item =>
        item.fundamentalImpact === 'REVIEW_REQUIRED');
      if (news.length) lines.push(`Fundamental 영향 원문 확인 필요 뉴스 ${news.length}건: ` +
        html(news[0].title.slice(0, 160)));
      lines.push(`Safety ${safety ?? '보류'}`);
    }
    lines.push('', '━━━━━━━━━━', '', '⚖️ <b>다음 주 배분</b>');
    const budget = plans.length ? weeklyBudget(plans, refresh.fxQuote?.rate ?? null) : null;
    if (budget === null) lines.push('예상 신규 투자금이 설정되지 않아 배분안은 보류합니다.');
    else if (!snapshot.isComplete) lines.push('계좌 데이터가 불완전해 신규 자금 배분안을 계산하지 않습니다.');
    else {
      if (snapshot.baseCurrency !== 'KRW') {
        throw new Error('weekly KRW budget requires a KRW base-currency snapshot');
      }
      lines.push(`예상 신규 투자금 ${display(budget, 0)}원`);
      const targets = plans.filter(plan => plan.targetWeightPct != null)
        .map(plan => ({ instrument: plan.instrument, targetWeightPct: plan.targetWeightPct }));
      try {
        const rebalance = suggestNewMoneyRebalance(snapshot, targets, exact(budget));
        for (const item of rebalance.items) if (compare(decimal(item.suggestedNewMoney), decimal('0')) > 0) {
          lines.push(`${html(`${item.instrument.market}:${item.instrument.ticker}`)} → ` +
            money(item.suggestedNewMoney, 'KRW'));
        }
        if (compare(decimal(rebalance.unallocatedCash), decimal('0')) > 0) {
          lines.push('현금 유지 → ' + money(rebalance.unallocatedCash, 'KRW'));
        }
      } catch (error) {
        if (!(error instanceof TypeError)) throw error;
        lines.push('목표 비중 합계가 올바르지 않아 배분 계산을 보류합니다.');
      }
    }
    lines.push('', '리밸런싱은 매도 대신 신규 자금 배분 변경을 우선합니다.');
    return lines.join('\n');
  }
  throw new TypeError('unsupported report type');
}
