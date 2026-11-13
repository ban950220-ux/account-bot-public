// Pure /assets formatter candidate. Input is normalized, synthetic or
// read-only broker results plus an aggregateCoverage snapshot.
import { add, compare, decimal, display, divide28, exact, multiply, round28, subtract } from './finance.mjs';

const zero = () => decimal('0');
const usable = new Set(['ok', 'partial', 'stale']);
const koreanMarkets = new Set(['KRX', 'KOSPI', 'KOSDAQ', 'NXT']);
const statusPriority = new Map([['ok', 0], ['stale', 1], ['partial', 2],
  ['not_configured', 3], ['rate_limited', 4], ['auth_failed', 5], ['unavailable', 6]]);
const statusText = new Map([['ok', '정상'], ['partial', '일부만 조회'],
  ['stale', '마지막 정상값'], ['not_configured', '미설정'],
  ['auth_failed', '인증 실패'], ['rate_limited', '호출 제한'],
  ['unavailable', '조회 실패']]);
const brokerLabels = new Map([['kiwoom', '키움'], ['kiwoom_main', '키움 메인'],
  ['kiwoom_accum', '키움 모으기'], ['toss', '토스'], ['meritz', '메리츠']]);

export function html(value) {
  return value.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#x27;');
}

export function money(value, currency) {
  return currency === 'KRW' ? `${display(decimal(value), 0)}원` :
    currency === 'USD' ? `$${display(decimal(value), 2)}` :
    `${display(decimal(value), 2)} ${html(currency)}`;
}

export function percent(value) {
  if (value === null) return '확인 불가';
  return `${compare(value, zero()) > 0 ? '+' : ''}${display(value, 2)}%`;
}

export function localTime(iso) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).formatToParts(new Date(iso)).map(({ type, value }) => [type, value]));
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}

function positionValue(row) {
  if (row.marketValue !== null && row.marketValue !== undefined) return decimal(row.marketValue);
  if (row.currentPrice !== null && row.currentPrice !== undefined) {
    return multiply(decimal(row.currentPrice), decimal(row.quantity));
  }
  return null;
}

function brokerTotals(positionResults, cashResults, fxQuote) {
  const totals = new Map();
  const valid = new Map();
  const convert = (value, currency) => {
    if (currency === 'KRW') return value;
    if (currency === 'USD' && fxQuote?.base === 'USD' && fxQuote.quote === 'KRW') {
      return multiply(value, decimal(fxQuote.rate));
    }
    return null;
  };
  for (const result of [...positionResults, ...cashResults]) {
    if (!totals.has(result.broker)) totals.set(result.broker, zero());
    if (!valid.has(result.broker)) valid.set(result.broker, true);
    if (result.status !== 'ok') valid.set(result.broker, false);
    if (!usable.has(result.status)) continue;
    for (const row of result.items) {
      const native = 'instrument' in row ? positionValue(row) : decimal(row.amount);
      const currency = 'instrument' in row ? row.instrument.currency : row.currency;
      const converted = native === null ? null : convert(native, currency);
      if (converted === null) valid.set(result.broker, false);
      else totals.set(result.broker, add(totals.get(result.broker), converted));
    }
  }
  return new Map([...totals].map(([broker, value]) => [broker, valid.get(broker) ? value : null]));
}

export function formatAssets(snapshot, { positionResults, cashResults = [],
  collectedAt, fxQuote = null }) {
  const total = snapshot.isComplete ? snapshot.totalMarketValue : snapshot.includedMarketValue;
  const scope = snapshot.expectedBrokers?.length === 1 &&
    snapshot.expectedBrokers[0] === 'kiwoom_main' ? '키움 메인' : '선택 출처';
  const lines = [snapshot.isComplete ? `💰 <b>${scope} 전체자산</b>` :
    `⚠️ <b>${scope} 확인 자산 부분 합계</b>`, '', '평가금액',
  `<b>${money(total, snapshot.baseCurrency)}</b>`];
  const nativeTotals = new Map();
  const accumulate = (currency, value) =>
    nativeTotals.set(currency, add(nativeTotals.get(currency) || zero(), value));
  for (const result of positionResults) if (usable.has(result.status)) {
    for (const row of result.items) {
      const value = positionValue(row);
      if (value !== null) accumulate(row.instrument.currency, value);
    }
  }
  for (const result of cashResults) if (usable.has(result.status)) {
    for (const row of result.items) accumulate(row.currency, decimal(row.amount));
  }
  if (nativeTotals.size) {
    lines.push('', '확인된 현지통화 합계');
    for (const currency of ['KRW', 'USD']) if (nativeTotals.has(currency)) {
      lines.push(money(exact(nativeTotals.get(currency)), currency));
    }
  }
  const cost = snapshot.isComplete ? snapshot.totalCostBasis : snapshot.includedCostBasis;
  if (cost !== null) {
    const profit = subtract(decimal(total), decimal(cost));
    const costValue = decimal(cost);
    const returnPct = compare(costValue, zero()) === 0 ? null :
      round28(multiply(divide28(profit, costValue), decimal('100')));
    lines.push('', '투자원금', money(cost, snapshot.baseCurrency), '', '평가손익',
      money(exact(profit), snapshot.baseCurrency), '', '수익률', percent(returnPct));
  } else lines.push('', '투자원금·수익률', '일부 원가 데이터가 없어 계산 보류');

  lines.push('', '━━━━━━━━━━', '', '🏦 <b>증권사</b>');
  const amounts = brokerTotals(positionResults, cashResults, fxQuote);
  const statuses = new Map();
  for (const result of [...positionResults, ...cashResults]) {
    if (!statuses.has(result.broker) ||
        statusPriority.get(result.status) > statusPriority.get(statuses.get(result.broker))) {
      statuses.set(result.broker, result.status);
    }
  }
  for (const broker of snapshot.expectedBrokers) {
    const value = amounts.get(broker);
    const status = statuses.get(broker) || 'unavailable';
    lines.push('', html(brokerLabels.get(broker) || broker), value === null || value === undefined ?
      `${statusText.get(status)} — 총액에서 0원 처리하지 않음` :
      money(exact(value), 'KRW'));
  }

  if (snapshot.isComplete && compare(decimal(total), zero()) > 0) {
    let korea = zero();
    let us = zero();
    for (const position of snapshot.positions) {
      const value = decimal(position.weightPct);
      if (koreanMarkets.has(position.instrument.market)) korea = add(korea, value);
      else us = add(us, value);
    }
    lines.push('', '━━━━━━━━━━', '', '🌎 <b>지역</b>', '',
      `미국주식 ${percent(us)}`, `한국주식 ${percent(korea)}`,
      `현금 ${percent(decimal(snapshot.cashWeightPct))}`);
  } else lines.push('', '━━━━━━━━━━', '', '🌎 지역 비중', '불완전한 분모이므로 계산 보류');
  if (fxQuote !== null) lines.push('',
    `USD/KRW ${display(decimal(fxQuote.rate), 2)}` +
    (fxQuote.isStale ? ' · 마지막 정상값' : ''));
  lines.push('', html(`기준 ${localTime(collectedAt)} KST · ` +
    (snapshot.isComplete ? '전체 조회' : '불완전 조회')));
  return lines.join('\n');
}
