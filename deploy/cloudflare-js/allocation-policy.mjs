// Owner-defined allocation policy: validation, deterministic proposal and HTML.
// Pure calculation over recurring plan amounts. No network, storage or order path.
import { add, compare, decimal, display, divide28, exact, multiply, subtract } from './finance.mjs';
import { html, money } from './assets-report.mjs';

const zero = () => decimal('0');
const hundred = () => decimal('100');
const DEFAULT_STEP = { KRW: '1000', USD: '1' };
// Broker minimums for a KRW recurring buy; an owner setting overrides them.
const DEFAULT_MINIMUM = { KRW: '2000', USD: '1' };
const DEFAULT_BROKER_MINIMUM = { meritz: '1000' };
const BROKER_WORDS = { 메리츠: 'meritz' };
const VALUATION_LOW = '35';
const VALUATION_HIGH = '70';
const CURRENCIES = ['KRW', 'USD'];
const TICKER = /^[A-Z0-9][A-Z0-9.]{0,11}$/;
const MAX_ENTRIES = 50;
const MAX_ITERATIONS = 5000;

function positive(value, name) {
  let parsed;
  try { parsed = decimal(value); } catch { throw new TypeError(`${name} must be a decimal string`); }
  if (compare(parsed, zero()) <= 0) throw new TypeError(`${name} must be positive`);
  return exact(parsed);
}

function pct(value, name) {
  const text = positive(value, name);
  if (compare(decimal(text), hundred()) > 0) throw new TypeError(`${name} must be at most 100`);
  return text;
}

function sortedMap(input, check, name) {
  if (input == null) return {};
  if (typeof input !== 'object' || Array.isArray(input)) throw new TypeError(`${name} must be a map`);
  const keys = Object.keys(input).sort();
  if (keys.length > MAX_ENTRIES) throw new TypeError(`${name} has too many entries`);
  return Object.fromEntries(keys.map(key => {
    if (!key || key.length > 40 || /[\u0000-\u001f]/.test(key)) throw new TypeError(`${name} key invalid`);
    return [key, check(input[key], `${name}.${key}`)];
  }));
}

function currencyMap(input, name) {
  const map = sortedMap(input, positive, name);
  if (Object.keys(map).some(key => !CURRENCIES.includes(key))) {
    throw new TypeError(`${name} currency unsupported`);
  }
  return map;
}

// Canonical form: fixed key order and normalized decimal strings, so equal
// policies serialize identically and proposal tokens are reproducible.
export function normalizePolicy(input = {}) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) throw new TypeError('policy object required');
  const pinned = [...new Set(input.pinned || [])].sort();
  if (pinned.length > MAX_ENTRIES || pinned.some(item => typeof item !== 'string' || !TICKER.test(item))) {
    throw new TypeError('pinned tickers invalid');
  }
  return {
    dailyBudget: currencyMap(input.dailyBudget, 'dailyBudget'),
    groupCaps: sortedMap(input.groupCaps, pct, 'groupCaps'),
    defaultGroupCap: input.defaultGroupCap == null ? null : pct(input.defaultGroupCap, 'defaultGroupCap'),
    tickerCap: input.tickerCap == null ? null : pct(input.tickerCap, 'tickerCap'),
    groupTargets: sortedMap(input.groupTargets, pct, 'groupTargets'),
    pinned,
    step: currencyMap(input.step, 'step'),
    minimum: currencyMap(input.minimum, 'minimum'),
    brokerMinimum: sortedMap(input.brokerMinimum, positive, 'brokerMinimum'),
  };
}

export function hasRules(policy) {
  return Boolean(Object.keys(policy.dailyBudget).length || Object.keys(policy.groupCaps).length ||
    policy.defaultGroupCap || policy.tickerCap || Object.keys(policy.groupTargets).length);
}

const USAGE = [
  '<code>/목표 예산 30000</code> — 평일 하루 합계(원)',
  '<code>/목표 섹터 반도체 30%</code> — 노출군 상한',
  '<code>/목표 기본상한 25%</code> — 따로 정하지 않은 노출군 상한',
  '<code>/목표 종목 10%</code> — 한 종목 상한',
  '<code>/목표 비중 헬스케어 10%</code> — 남는 예산을 채울 노출군 목표',
  '<code>/목표 고정 QQQ,VOO</code> — 추천에서 제외할 종목',
  '<code>/목표 단위 1000</code> · <code>/목표 최소 2000</code> — 변경 단위·최소 금액(원)',
  '<code>/목표 최소 메리츠 1000</code> — 증권사별 최소 금액',
  '값 자리에 <code>없음</code>을 쓰면 해당 설정을 지웁니다.',
].join('\n');

// Returns { policy } for a valid edit or { error } (HTML) for a usage problem.
export function editPolicy(current, args, knownGroups) {
  const fail = message => ({ error: `${message}\n\n${USAGE}` });
  if (!args.length) return fail('변경할 항목을 입력해 주세요.');
  const [kind, ...rest] = args;
  const last = rest.at(-1);
  const clear = last === '없음';
  const next = structuredClone(current);
  const percentValue = text => /^(\d+(?:\.\d+)?)%$/.exec(text || '')?.[1] ?? null;
  const amountValue = text => /^\d[\d,]*$/.test(text || '') ? text.replaceAll(',', '') : null;
  try {
    if (kind === '최소' && rest.length === 2 && rest[0] in BROKER_WORDS) {
      if (clear) delete next.brokerMinimum[BROKER_WORDS[rest[0]]];
      else {
        const amount = amountValue(last);
        if (amount === null) return fail('금액은 숫자로 입력해 주세요.');
        next.brokerMinimum[BROKER_WORDS[rest[0]]] = amount;
      }
    } else if (kind === '예산' || kind === '단위' || kind === '최소') {
      const field = { 예산: 'dailyBudget', 단위: 'step', 최소: 'minimum' }[kind];
      if (rest.length !== 1) return fail(`${kind}은 금액 하나만 입력합니다.`);
      if (clear) delete next[field].KRW;
      else {
        const amount = amountValue(last);
        if (amount === null) return fail('금액은 숫자로 입력해 주세요.');
        next[field].KRW = amount;
      }
    } else if (kind === '기본상한' || kind === '종목') {
      const field = kind === '종목' ? 'tickerCap' : 'defaultGroupCap';
      if (rest.length !== 1) return fail(`${kind}은 비율 하나만 입력합니다.`);
      if (clear) next[field] = null;
      else {
        const value = percentValue(last);
        if (value === null) return fail('비율은 30%처럼 입력해 주세요.');
        next[field] = value;
      }
    } else if (kind === '섹터' || kind === '비중') {
      const field = kind === '섹터' ? 'groupCaps' : 'groupTargets';
      const group = rest.slice(0, -1).join(' ');
      if (!group) return fail('노출군 이름과 비율을 입력해 주세요.');
      if (!knownGroups.includes(group)) {
        return fail(`알 수 없는 노출군입니다. 사용 가능: ${knownGroups.map(html).join(', ')}`);
      }
      if (clear) delete next[field][group];
      else {
        const value = percentValue(last);
        if (value === null) return fail('비율은 30%처럼 입력해 주세요.');
        next[field][group] = value;
      }
    } else if (kind === '고정') {
      if (!rest.length) return fail('고정할 종목을 입력해 주세요.');
      next.pinned = clear ? [] : rest.join(',').split(/[\s,]+/).filter(Boolean)
        .map(value => value.toUpperCase());
    } else return fail('알 수 없는 항목입니다.');
    return { policy: normalizePolicy(next) };
  } catch (error) {
    if (!(error instanceof TypeError)) throw error;
    return fail('값이 허용 범위를 벗어났습니다.');
  }
}

function share(amount, total) {
  return compare(total, zero()) > 0 ?
    `${display(multiply(divide28(amount, total), hundred()), 1)}%` : '확인 불가';
}

// amount / total > capPct / 100, without division.
function above(amount, capPct, total) {
  return compare(multiply(amount, hundred()), multiply(total, decimal(capPct))) > 0;
}

export function minimumFor(plan, policy) {
  return policy.brokerMinimum[plan.broker] ?? policy.minimum[plan.currency] ??
    (plan.currency === 'KRW' ? DEFAULT_BROKER_MINIMUM[plan.broker] : undefined) ??
    DEFAULT_MINIMUM[plan.currency];
}

function evaluateCurrency(currency, plans, policy) {
  const step = decimal(policy.step[currency] || DEFAULT_STEP[currency]);
  const pinned = new Set(policy.pinned);
  const budgetWeekly = policy.dailyBudget[currency] ?
    multiply(decimal(policy.dailyBudget[currency]), decimal('5')) : null;
  const capOf = group => policy.groupCaps[group] ?? policy.defaultGroupCap;
  const items = plans.map(plan => ({ plan, amount: decimal(plan.dailyAmount),
    days: decimal(String(plan.days)), minimum: decimal(minimumFor(plan, policy)), groups: plan.groups.map(([group, weight]) =>
      [group, decimal(weight)]), reasons: [],
    movable: plan.adjustable && !pinned.has(plan.ticker) && plan.days > 0 }));
  const weeklyOf = item => multiply(item.amount, item.days);
  const contribution = (item, group) => item.groups.filter(([name]) => name === group)
    .reduce((sum, [, weight]) => add(sum, multiply(weeklyOf(item), weight)), zero());

  function totals() {
    let total = zero();
    const groups = new Map();
    const tickers = new Map();
    for (const item of items) {
      const weekly = weeklyOf(item);
      total = add(total, weekly);
      tickers.set(item.plan.ticker, add(tickers.get(item.plan.ticker) || zero(), weekly));
      for (const [group, weight] of item.groups) {
        groups.set(group, add(groups.get(group) || zero(), multiply(weekly, weight)));
      }
    }
    return { total, groups, tickers };
  }

  function violations({ total, groups, tickers }) {
    const found = [];
    if (compare(total, zero()) <= 0) return found;
    if (policy.tickerCap) for (const [ticker, amount] of tickers) {
      if (above(amount, policy.tickerCap, total)) found.push({ kind: 'ticker', key: ticker, amount,
        text: `종목 ${ticker} ${share(amount, total)} > 상한 ${policy.tickerCap}%` });
    }
    for (const [group, amount] of groups) {
      const cap = capOf(group);
      if (cap && above(amount, cap, total)) found.push({ kind: 'group', key: group, amount,
        text: `${group} ${share(amount, total)} > 상한 ${cap}%` });
    }
    if (budgetWeekly && compare(total, budgetWeekly) > 0) found.push({ kind: 'budget', key: currency,
      amount: total, text: `주간 합계 ${money(exact(total), currency)} > 예산 ` +
        `${money(exact(budgetWeekly), currency)}` });
    return found;
  }

  const byLargest = (score, pool, id = item => item.plan.id) => [...pool].sort((a, b) =>
    -compare(score(a), score(b)) || id(a).localeCompare(id(b)));
  const reducible = item => item.movable &&
    compare(subtract(item.amount, step), item.minimum) >= 0;
  function reduce(item, reason) {
    item.amount = subtract(item.amount, step);
    if (!item.reasons.includes(reason)) item.reasons.push(reason);
  }

  const before = totals();
  const violationsBefore = violations(before);
  // Phase 0: one step per plan from its valuation score, where data exists.
  // A raise must fit the budget and every cap at the moment it is made.
  const heldAtMinimum = [];
  for (const item of items) {
    const raw = item.plan.signal?.valuationScore;
    if (!item.movable || raw == null) continue;
    const score = decimal(raw);
    const shown = display(score, 1);
    if (compare(score, decimal(VALUATION_LOW)) < 0) {
      if (reducible(item)) reduce(item, `Valuation ${shown}점 · 가격 부담`);
      else heldAtMinimum.push(`${item.plan.label} ${shown}점`);
    } else if (compare(score, decimal(VALUATION_HIGH)) >= 0) {
      const state = totals();
      const increase = multiply(step, item.days);
      const nextTotal = add(state.total, increase);
      if (budgetWeekly && compare(nextTotal, budgetWeekly) > 0) continue;
      if (policy.tickerCap && above(add(state.tickers.get(item.plan.ticker), increase),
        policy.tickerCap, nextTotal)) continue;
      if (item.groups.some(([name, weight]) => capOf(name) && above(
        add(state.groups.get(name), multiply(increase, weight)), capOf(name), nextTotal))) continue;
      item.amount = add(item.amount, step);
      item.reasons.push(`Valuation ${shown}점 · 가격 매력`);
    }
  }
  // Phase 1: reductions only. Ticker caps, then group caps, then the budget.
  for (let iteration = 0; iteration < MAX_ITERATIONS; iteration += 1) {
    const state = totals();
    const open = violations(state);
    let target = null;
    for (const item of byLargest(v => v.amount, open.filter(v => v.kind === 'ticker'), v => v.key)) {
      target = byLargest(weeklyOf, items.filter(candidate =>
        candidate.plan.ticker === item.key && reducible(candidate)))[0];
      if (target) { reduce(target, '종목 상한 초과'); break; }
    }
    if (!target) for (const item of byLargest(v => v.amount, open.filter(v => v.kind === 'group'), v => v.key)) {
      target = byLargest(candidate => contribution(candidate, item.key), items.filter(candidate =>
        reducible(candidate) && compare(contribution(candidate, item.key), zero()) > 0))[0];
      if (target) { reduce(target, `${item.key} 상한 초과`); break; }
    }
    if (!target && open.some(v => v.kind === 'budget')) {
      for (const [group] of [...state.groups].sort((a, b) =>
        -compare(a[1], b[1]) || a[0].localeCompare(b[0]))) {
        target = byLargest(candidate => contribution(candidate, group), items.filter(candidate =>
          reducible(candidate) && compare(contribution(candidate, group), zero()) > 0))[0];
        if (target) { reduce(target, '예산 초과'); break; }
      }
    }
    if (!target) break;
  }
  // Phase 2: spend spare budget only toward owner-set group targets.
  if (budgetWeekly && Object.keys(policy.groupTargets).length) {
    for (let iteration = 0; iteration < MAX_ITERATIONS; iteration += 1) {
      const state = totals();
      const spare = subtract(budgetWeekly, state.total);
      if (compare(spare, zero()) <= 0) break;
      const deficits = Object.entries(policy.groupTargets).map(([group, target]) =>
        [group, subtract(multiply(state.total, decimal(target)),
          multiply(state.groups.get(group) || zero(), hundred()))])
        .filter(([, gap]) => compare(gap, zero()) > 0)
        .sort((a, b) => -compare(a[1], b[1]) || a[0].localeCompare(b[0]));
      let grown = null;
      for (const [group] of deficits) {
        const pool = byLargest(weeklyOf, items.filter(candidate => candidate.movable &&
          compare(contribution(candidate, group), zero()) > 0)).reverse();
        for (const candidate of pool) {
          const increase = multiply(step, candidate.days);
          if (compare(increase, spare) > 0) continue;
          const nextTotal = add(state.total, increase);
          const tickerAfter = add(state.tickers.get(candidate.plan.ticker), increase);
          if (policy.tickerCap && above(tickerAfter, policy.tickerCap, nextTotal)) continue;
          if (candidate.groups.some(([name, weight]) => capOf(name) && above(
            add(state.groups.get(name), multiply(increase, weight)), capOf(name), nextTotal))) continue;
          candidate.amount = add(candidate.amount, step);
          const reason = `${group} 목표 미달 · 여유 예산 배분`;
          if (!candidate.reasons.includes(reason)) candidate.reasons.push(reason);
          grown = candidate;
          break;
        }
        if (grown) break;
      }
      if (!grown) break;
    }
  }
  const after = totals();
  const unmet = violations(after);
  const feasible = unmet.length === 0;
  const changes = feasible ? items.filter(item =>
    compare(item.amount, decimal(item.plan.dailyAmount)) !== 0).map(item => ({
    plan: item.plan, from: exact(decimal(item.plan.dailyAmount)), to: exact(item.amount),
    action: compare(item.amount, decimal(item.plan.dailyAmount)) > 0 ? '증액' : '감액',
    reasons: item.reasons })) : [];
  const shown = feasible ? after : before;
  return { currency, feasible, changes, heldAtMinimum,
    violationsBefore: violationsBefore.map(item => item.text),
    unmet: unmet.map(item => item.text),
    budgetWeekly: budgetWeekly === null ? null : exact(budgetWeekly),
    before: { weekly: exact(before.total), groups: [...before.groups].map(([group, amount]) =>
      [group, exact(amount)]) },
    after: { weekly: exact(shown.total), groups: [...shown.groups].map(([group, amount]) =>
      [group, exact(amount)]) },
    spareWeekly: budgetWeekly === null || compare(budgetWeekly, shown.total) <= 0 ? null :
      exact(subtract(budgetWeekly, shown.total)) };
}

// plans: enabled plans as { id, planKey, version, ticker, label, currency,
// dailyAmount, days, adjustable, groups: [[group, weight]] }.
export function evaluate(plans, policy) {
  for (const plan of plans) {
    if (typeof plan.id !== 'string' || !CURRENCIES.includes(plan.currency) ||
        !Number.isInteger(plan.days) || plan.days < 0 || plan.days > 7 ||
        typeof plan.adjustable !== 'boolean' || !Array.isArray(plan.groups) || !plan.groups.length ||
        compare(decimal(plan.dailyAmount), zero()) <= 0) {
      throw new TypeError('invalid allocation plan input');
    }
  }
  return CURRENCIES.filter(currency => plans.some(plan => plan.currency === currency))
    .map(currency => evaluateCurrency(currency,
      plans.filter(plan => plan.currency === currency), policy));
}

// Everything an approval must match: the policy version and each changed
// plan's key, version and proposed amount.
export function proposalFingerprint(policyVersion, results) {
  const changes = results.flatMap(result => result.changes).map(change =>
    [change.plan.planKey, change.plan.version, change.to])
    .sort((a, b) => a[0].localeCompare(b[0]));
  return changes.length ? JSON.stringify([policyVersion, changes]) : null;
}

export function formatPolicy(policy, version) {
  const lines = ['🎯 <b>모으기 목표·한도</b>', `설정 버전 ${version}`];
  if (!hasRules(policy)) lines.push('', '설정된 예산·상한·목표가 없습니다.');
  for (const [currency, amount] of Object.entries(policy.dailyBudget)) {
    lines.push(`하루 예산 ${money(amount, currency)} · 주간 ` +
      money(exact(multiply(decimal(amount), decimal('5'))), currency));
  }
  if (policy.tickerCap) lines.push(`종목 상한 ${policy.tickerCap}%`);
  if (policy.defaultGroupCap) lines.push(`노출군 기본 상한 ${policy.defaultGroupCap}%`);
  for (const [group, cap] of Object.entries(policy.groupCaps)) {
    lines.push(`• ${html(group)} 상한 ${cap}%`);
  }
  for (const [group, target] of Object.entries(policy.groupTargets)) {
    lines.push(`• ${html(group)} 목표 ${target}%`);
  }
  if (policy.pinned.length) lines.push(`고정 종목 ${html(policy.pinned.join(', '))}`);
  lines.push(`변경 단위 ${money(policy.step.KRW || DEFAULT_STEP.KRW, 'KRW')} · 최소 ` +
    `${money(policy.minimum.KRW || DEFAULT_MINIMUM.KRW, 'KRW')} · 메리츠 최소 ` +
    money(policy.brokerMinimum.meritz || policy.minimum.KRW || DEFAULT_BROKER_MINIMUM.meritz, 'KRW'));
  lines.push('', USAGE, '', '이 설정은 계산 기준일 뿐이며 증권사 주문을 만들지 않습니다.');
  return lines.join('\n');
}

function groupLines(result, side) {
  const total = decimal(result[side].weekly);
  return [...result[side].groups].sort((a, b) => -compare(decimal(a[1]), decimal(b[1])) ||
    a[0].localeCompare(b[0])).map(([group, amount]) => {
    const prior = result.before.groups.find(([name]) => name === group)?.[1] ?? '0';
    const was = share(decimal(prior), decimal(result.before.weekly));
    const now = share(decimal(amount), total);
    return `• ${html(group)} ${was === now ? now : `${was} → ${now}`}`;
  });
}

export function formatEvaluation(results, token) {
  const lines = ['🧮 <b>모으기 추천 계산</b>'];
  if (!results.length) return [...lines, '', '활성 모으기 계획이 없습니다.'].join('\n');
  for (const result of results) {
    const cur = result.currency;
    const monthly = value => money(exact(divide28(multiply(decimal(value), decimal('52')),
      decimal('12'))), cur);
    lines.push('', `<b>${cur}</b> 주간 ${money(result.before.weekly, cur)}` +
      (result.budgetWeekly ? ` · 예산 ${money(result.budgetWeekly, cur)}` : ''));
    if (!result.violationsBefore.length) lines.push('현재 설정 기준 위반 없음');
    else lines.push('현재 위반:', ...result.violationsBefore.map(text => `• ${html(text)}`));
    if (!result.feasible) {
      lines.push('⚠️ 고정 종목·최소 금액을 지키면서 모든 기준을 맞추는 안을 만들 수 없어 ' +
        '변경안을 내지 않습니다.', '줄여도 남는 위반:',
      ...result.unmet.map(text => `• ${html(text)}`));
      continue;
    }
    for (const change of result.changes) {
      lines.push(`${change.action === '증액' ? '🔺' : '🔻'} <b>${html(change.plan.label)}</b> ` +
        `${money(change.from, cur)} → ${money(change.to, cur)}`,
      `  이유: ${html(change.reasons.join(' · '))}`);
    }
    if (result.changes.length) {
      lines.push(`주간 ${money(result.before.weekly, cur)} → ${money(result.after.weekly, cur)}`,
        `월평균 ${monthly(result.before.weekly)} → ${monthly(result.after.weekly)}`,
        '노출군 비중 (전 → 후)', ...groupLines(result, 'after'));
    }
    if (result.heldAtMinimum.length) lines.push(
      'Valuation 35점 미만이지만 최소 금액이거나 고정이라 그대로 둔 종목: ' +
      html(result.heldAtMinimum.join(', ')));
    if (result.spareWeekly) lines.push(`여유 예산 주간 ${money(result.spareWeekly, cur)} · ` +
      '채울 노출군 목표가 없거나 상한에 막혀 배분하지 않음');
  }
  if (token) lines.push('', `반영하려면 <code>/추천적용 ${token}</code>`,
    '금액이나 설정이 바뀌면 이 번호는 무효가 됩니다.');
  else if (results.every(result => result.feasible)) lines.push('', '변경할 종목이 없습니다.');
  lines.push('', '사용자가 정한 기준에 따른 계산이며 투자 권유가 아닙니다. ' +
    '봇의 계획 금액만 바뀌고 증권사 주문은 실행하지 않습니다.',
  'Valuation 점수는 공개 지표를 정해진 규칙으로 계산한 값이며, 35점 미만은 한 단계 감액, ' +
    '70점 이상은 한 단계 증액으로 반영합니다. ETF는 점수가 없어 금액을 바꾸지 않습니다.',
  '월평균은 주간 × 52 ÷ 12이며 휴장일을 차감하지 않습니다.');
  return lines.join('\n');
}

// Short addendum for the recurring report; empty when nothing is configured.
export function policyNotice(results, policy) {
  if (!hasRules(policy)) return '';
  const open = results.flatMap(result => result.violationsBefore);
  return ['', '🎯 <b>목표·한도 점검</b>',
    ...(open.length ? [...open.map(text => `• ${html(text)}`),
      '변경안은 <code>/추천</code>에서 확인하세요.'] : ['설정 기준 위반 없음'])].join('\n');
}
