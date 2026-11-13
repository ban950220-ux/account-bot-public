// Read-only, new-money-only proposal. No order or sale transport exists here.
import { add, compare, decimal, divide28, exact, multiply, round28, subtract } from './finance.mjs';
import { html, money, percent } from './assets-report.mjs';

const zero = () => decimal('0');
const hundred = () => decimal('100');
const key = item => `${item.market}:${item.ticker}:${item.assetType}:${item.currency}`;
const displayId = item => `${item.market}:${item.ticker}`;
const usMarkets = new Set(['US', 'USA', 'NASDAQ', 'NAS', 'NYSE', 'AMEX', 'ARCA', 'NYSEARCA']);
function compatible(left, right) {
  if (left.ticker !== right.ticker || left.assetType !== right.assetType ||
      left.currency !== right.currency) return false;
  return left.market === right.market ||
    (['US', 'USA'].includes(left.market) && usMarkets.has(right.market)) ||
    (['US', 'USA'].includes(right.market) && usMarkets.has(left.market));
}

function pct(value, denominator) {
  return compare(denominator, zero()) > 0 ?
    round28(multiply(divide28(value, denominator), hundred())) : null;
}

export function suggestNewMoneyRebalance(snapshot, targets, newMoney) {
  const incoming = decimal(newMoney);
  if (compare(incoming, zero()) < 0) throw new TypeError('negative new money');
  const targetInput = new Map();
  let targetSum = zero();
  for (const target of targets) {
    const identity = key(target.instrument);
    const weight = decimal(target.targetWeightPct);
    if (targetInput.has(identity) || compare(weight, zero()) < 0 ||
        compare(weight, hundred()) > 0) throw new TypeError('invalid target');
    targetInput.set(identity, { instrument: target.instrument, weight });
    targetSum = add(targetSum, weight);
  }
  if (compare(targetSum, hundred()) > 0) throw new TypeError('target sum above 100');
  const withheld = reason => ({ isEligible: false, baseCurrency: snapshot.baseCurrency,
    newMoney, allocatedNewMoney: '0', unallocatedCash: newMoney,
    projectedTotalValue: null, items: [], usesSales: false, warnings: [reason] });
  if (!snapshot.isComplete || snapshot.totalMarketValue === null) {
    return withheld('rebalance_withheld:portfolio_snapshot_incomplete');
  }
  const current = new Map();
  for (const position of snapshot.positions) {
    if (position.marketValueBase === null) {
      return withheld('rebalance_withheld:position_valuation_missing');
    }
    current.set(key(position.instrument), { instrument: position.instrument,
      value: decimal(position.marketValueBase) });
  }
  const canonical = new Map();
  const canonicalInstruments = new Map();
  const aliasWarnings = [];
  for (const target of targetInput.values()) {
    let identity = key(target.instrument);
    if (!current.has(identity)) {
      const matches = [...current.values()].filter(row => compatible(row.instrument, target.instrument));
      if (matches.length > 1) return withheld('rebalance_withheld:ambiguous_instrument_alias');
      if (matches.length === 1) identity = key(matches[0].instrument);
    }
    if (canonical.has(identity)) throw new TypeError('duplicate compatible target');
    canonical.set(identity, target.weight);
    canonicalInstruments.set(identity, current.get(identity)?.instrument || target.instrument);
    if (identity !== key(target.instrument)) {
      aliasWarnings.push(`instrument_alias_resolved:${displayId(target.instrument)}->` +
        displayId(current.get(identity).instrument));
    }
  }
  const currentTotal = decimal(snapshot.totalMarketValue);
  const projectedTotal = add(currentTotal, incoming);
  const all = new Map(current);
  for (const [identity, instrument] of canonicalInstruments) {
    if (!all.has(identity)) all.set(identity, { instrument, value: zero() });
  }
  const deficits = new Map();
  let totalDeficit = zero();
  for (const [identity, row] of all) {
    const targetValue = divide28(multiply(projectedTotal, canonical.get(identity) || zero()), hundred());
    const gap = subtract(targetValue, row.value);
    const deficit = compare(gap, zero()) > 0 ? gap : zero();
    deficits.set(identity, deficit);
    totalDeficit = add(totalDeficit, deficit);
  }
  const available = compare(incoming, totalDeficit) < 0 ? incoming : totalDeficit;
  const allocations = new Map([...all.keys()].map(identity => [identity, zero()]));
  if (compare(totalDeficit, zero()) > 0 && compare(available, zero()) > 0) {
    if (compare(totalDeficit, available) <= 0) {
      for (const [identity, deficit] of deficits) allocations.set(identity, deficit);
    } else {
      for (const [identity, deficit] of deficits) {
        allocations.set(identity, divide28(multiply(available, deficit), totalDeficit));
      }
      const allocated = [...allocations.values()].reduce(add, zero());
      const residual = subtract(available, allocated);
      if (compare(residual, zero()) !== 0) {
        const largest = [...deficits.keys()].sort((a, b) =>
          compare(deficits.get(b), deficits.get(a)) || a.localeCompare(b))[0];
        allocations.set(largest, add(allocations.get(largest), residual));
      }
    }
  }
  const allocated = [...allocations.values()].reduce(add, zero());
  const items = [...all.keys()].sort().map(identity => {
    const row = all.get(identity);
    const targetWeight = canonical.get(identity) || zero();
    const suggestion = allocations.get(identity);
    const currentWeight = pct(row.value, currentTotal);
    const projectedWeight = pct(add(row.value, suggestion), projectedTotal);
    const remainingGap = projectedWeight === null ? null : subtract(targetWeight, projectedWeight);
    const action = compare(suggestion, zero()) > 0 ? 'add_with_new_money' :
      currentWeight !== null && compare(currentWeight, targetWeight) > 0 ?
        'overweight_no_new_money' : 'hold';
    return { instrument: row.instrument, currentValue: exact(row.value),
      currentWeightPct: currentWeight === null ? null : exact(currentWeight),
      targetWeightPct: exact(targetWeight), suggestedNewMoney: exact(suggestion),
      projectedWeightPct: projectedWeight === null ? null : exact(projectedWeight),
      remainingGapPct: remainingGap === null ? null : exact(remainingGap), action };
  });
  return { isEligible: true, baseCurrency: snapshot.baseCurrency, newMoney,
    allocatedNewMoney: exact(allocated), unallocatedCash: exact(subtract(incoming, allocated)),
    projectedTotalValue: exact(projectedTotal), items, usesSales: false,
    warnings: ['new_money_first:no_sales_suggested', ...aliasWarnings] };
}

export function formatRebalance(plan) {
  const lines = ['⚖️ <b>Portfolio Rebalancing</b>'];
  if (!plan.isEligible) return [...lines, '', '불완전한 포트폴리오 분모이므로 제안 보류',
    '누락 계좌를 0원으로 간주하지 않습니다.'].join('\n');
  for (const item of plan.items) {
    const action = item.action === 'add_with_new_money' ?
      `신규 자금 ${money(item.suggestedNewMoney, plan.baseCurrency)} 배분` :
      item.action === 'overweight_no_new_money' ? '과다 — 신규 자금 배분 중단' : '유지';
    lines.push('', `<b>${html(displayId(item.instrument))}</b>`,
      `현재 ${percent(item.currentWeightPct === null ? null : decimal(item.currentWeightPct))}` +
      ` · 목표 ${percent(decimal(item.targetWeightPct))}`, action);
  }
  lines.push('', '━━━━━━━━━━', '',
    `배분 합계 ${money(plan.allocatedNewMoney, plan.baseCurrency)}`,
    `현금 유지 ${money(plan.unallocatedCash, plan.baseCurrency)}`, '',
    '매도를 제안하지 않고 신규 자금 배분을 우선했습니다.');
  return lines.join('\n');
}
