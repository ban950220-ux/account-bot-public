// Pure HTML formatter for a normalized, provider-attributed analysis result.
import { compare, decimal, display, exact, multiply } from './finance.mjs';
import { html, money, percent } from './assets-report.mjs';

const score = value => value == null ? '보류' : display(decimal(value), 2).replaceAll(',', '');
export function fixed(value, places) {
  // Python's direct Decimal f-format uses ROUND_HALF_EVEN; format_money uses HALF_UP.
  const parsed = decimal(value);
  const shift = parsed.scale - places;
  let rounded = parsed.coefficient;
  if (shift > 0) {
    const divisor = 10n ** BigInt(shift);
    const negative = rounded < 0n;
    const absolute = negative ? -rounded : rounded;
    let quotient = absolute / divisor;
    const remainder = absolute % divisor;
    if (remainder * 2n > divisor || (remainder * 2n === divisor && quotient % 2n === 1n)) {
      quotient += 1n;
    }
    rounded = negative ? -quotient : quotient;
  } else if (shift < 0) rounded *= 10n ** BigInt(-shift);
  const negative = rounded < 0n ? '-' : '';
  const digits = (rounded < 0n ? -rounded : rounded).toString().padStart(places + 1, '0');
  const whole = digits.slice(0, digits.length - places).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  return negative + whole + (places ? `.${digits.slice(-places)}` : '');
}
const ratioPct = value => percent(multiply(decimal(value), decimal('100')));
const decisionLabels = new Map([
  ['aggressive_buy', '🟢 적극 매수'], ['continue_buy', '🟢 계속 매수'],
  ['maintain', '🟡 현재 수준 유지'], ['reduce', '🟠 매수 축소'],
  ['review_stop', '🔴 매수 중단 검토'], ['review_required', '🔴 중요 경고 검토 필요'],
  ['unavailable', '⚪ 판단 보류'],
]);

export function formatAnalysis(result) {
  if (!result?.companyScore?.components || result.companyScore.components.length < 5 ||
      !result.portfolioScore || !result.finalScore ||
      !decisionLabels.has(result.finalScore.decision)) {
    throw new TypeError('incomplete normalized analysis');
  }
  const lines = [`🔍 <b>${html(result.market)}:${html(result.ticker)} 투자분석</b>`];
  if (result.price) lines.push('', '현재가',
    money(result.price.price, result.price.currency) +
      (result.price.stale ? ' · 마지막 정상값' : ''));
  else lines.push('', '현재가', '확인 불가');
  lines.push('', '━━━━━━━━━━', '', '📈 <b>Fundamental</b>',
    `${score(result.companyScore.components[0].score)} / 100`);
  if (result.fundamentals) {
    const f = result.fundamentals;
    if (f.revenueGrowth != null) lines.push(`Revenue growth ${ratioPct(f.revenueGrowth)}`);
    if (f.operatingMargin != null) lines.push(`Operating margin ${ratioPct(f.operatingMargin)}`);
    if (f.freeCashFlow != null) lines.push(`FCF ${fixed(f.freeCashFlow, 0)}`);
  } else lines.push('정량 데이터 부족');
  lines.push('', '📑 <b>최근 실적</b>');
  if (result.earnings) {
    const e = result.earnings;
    lines.push(html(e.period || '최근 확인 분기'));
    if (e.revenue != null) lines.push(`Revenue ${fixed(e.revenue, 0)} (provider 표시 단위)`);
    if (e.revenueYoy != null) lines.push(`Revenue YoY ${ratioPct(e.revenueYoy)}`);
    if (e.eps != null) lines.push(`EPS ${fixed(e.eps, 2)}`);
    if (e.epsYoy != null) lines.push(`EPS YoY ${ratioPct(e.epsYoy)}`);
    if (e.grossMargin != null) lines.push(`Gross margin ${ratioPct(e.grossMargin)}`);
    if (e.operatingMargin != null) lines.push(`Operating margin ${ratioPct(e.operatingMargin)}`);
    if (e.guidance) lines.push('Guidance ' + html(e.guidance.slice(0, 240)));
  } else lines.push('확인 가능한 분기 실적 데이터 없음');
  lines.push('', '💰 <b>Valuation</b>',
    `${score(result.companyScore.components[1].score)} / 100`);
  if (result.valuation) {
    const v = result.valuation;
    if (v.closePrice != null) {
      const date = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Seoul',
        year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(v.asOf));
      lines.push(`주간 기준종가 ${fixed(v.closePrice, 2)} · ${date}`);
    }
    if (v.forwardPe != null) lines.push(`Forward PER ${fixed(v.forwardPe, 2)}x`);
    if (v.historicalForwardPeMedian != null) {
      lines.push(`과거 Median ${fixed(v.historicalForwardPeMedian, 2)}x`);
    } else lines.push('과거 5Y median 미제공 — 절대 PER만으로 단정하지 않음');
    if (v.peg != null) lines.push(`PEG ${fixed(v.peg, 2)}`);
    if (v.enterpriseToEbitda != null) lines.push(`EV/EBITDA ${fixed(v.enterpriseToEbitda, 2)}x`);
    if (v.freeCashFlowYield != null) {
      lines.push(`FCF Yield ${fixed(exact(multiply(decimal(v.freeCashFlowYield),
        decimal('100'))), 2)}%`);
    }
    if (v.dilutionPct != null) {
      lines.push(`주식수 변화 ${compare(decimal(v.dilutionPct), decimal('0')) >= 0 ? '+' : ''}` +
        `${fixed(v.dilutionPct, 2)}%`);
    }
  } else lines.push('정량 데이터 부족');
  lines.push('', '🚀 <b>Growth</b>', `${score(result.companyScore.components[2].score)} / 100`,
    '', '🏰 <b>Moat</b>', '검증 가능한 경쟁우위 근거 미입력 시 점수 보류',
    '', '⚠️ <b>Safety</b>', `${score(result.companyScore.components[4].score)} / 100`,
    '', '━━━━━━━━━━', '', `기업점수 ${score(result.companyScore.score)} / 100`,
    `Portfolio 점수 ${score(result.portfolioScore.score)} / 100`,
    `최종 ${score(result.finalScore.score)} / 100`,
    `장기추세 적합성 ${score(result.recurringSuitability.score)} / 100`,
    '', `<b>${decisionLabels.get(result.finalScore.decision)}</b>`, '', '이유');
  (result.reasons || []).forEach((reason, index) => lines.push(`${index + 1}. ${html(reason)}`));
  const news = (result.news || []).filter(item => item.fundamentalImpact === 'REVIEW_REQUIRED');
  if (news.length) {
    lines.push('', '📰 <b>Fundamental 영향 확인 필요 뉴스</b>');
    for (const item of news.slice(0, 3)) {
      const title = html(item.title);
      lines.push(item.url.startsWith('https://') ?
        `• <a href="${html(item.url)}">${title}</a>` : `• ${title}`);
    }
    lines.push('뉴스 자체는 점수에 직접 반영하지 않았습니다.');
  }
  if (result.warnings?.length) lines.push('', '데이터 경고: ' +
    html(result.warnings.join(', ')));
  return lines.join('\n');
}
