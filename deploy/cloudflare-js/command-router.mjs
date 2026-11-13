// Owner-only Telegram command planning. Returns HTML; it never sends a message.
import { admittedUpdateId } from './boundary.mjs';
import { START, HELP } from './static-commands.mjs';
import { formatAssets, html, money } from './assets-report.mjs';
import { formatPortfolio, formatAccounts } from './portfolio-report.mjs';
import { exposure, exposureLabels, formatRecurring } from './recurring-report.mjs';
import { editPolicy, evaluate, formatEvaluation, formatPolicy, policyNotice,
  proposalFingerprint } from './allocation-policy.mjs';
import { stockReply, instrumentParts, rebalanceReply } from './dynamic-commands.mjs';
import { buildPeriodicReport } from './periodic-report.mjs';
import { analyzeWithProvider } from './analysis-service.mjs';
import { formatAnalysis } from './analysis-report.mjs';
import { compare, decimal, display } from './finance.mjs';
import { formatLegacyHistory } from './legacy-history-report.mjs';

const ALIASES = new Map([
  ['start', 'start'], ['help', 'help'], ['도움말', 'help'],
  ['assets', 'assets'], ['자산', 'assets'], ['portfolio', 'portfolio'],
  ['포트폴리오', 'portfolio'],
  ['recurring', 'recurring'], ['모으기', 'recurring'],
  ['recurring_add', 'recurring_add'], ['모으기추가', 'recurring_add'],
  ['recurring_set', 'recurring_set'], ['모으기설정', 'recurring_set'],
  ['recurring_update', 'recurring_update'], ['모으기변경', 'recurring_update'],
  ['stock', 'stock'], ['종목', 'stock'], ['analyze', 'analyze'], ['분석', 'analyze'],
  ['thesis', 'thesis'], ['rebalance', 'rebalance'], ['리밸런싱', 'rebalance'],
  ['weekly', 'weekly'], ['주간리포트', 'weekly'],
  ['history', 'history'], ['이력', 'history'],
  ['policy', 'policy'], ['목표', 'policy'], ['recommend', 'recommend'], ['추천', 'recommend'],
  ['recommend_apply', 'recommend_apply'], ['추천적용', 'recommend_apply'],
]);
const KR_MARKETS = new Set(['KOSPI', 'KOSDAQ', 'KRX', 'NXT']);
const US_MARKETS = new Set(['US','USA','NASDAQ','NAS','NYSE','AMEX','ARCA','NYSEARCA']);
const FAILURE = '요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.';
const USAGE_UPDATE = '사용법:\n<code>/모으기변경 ETF 전부 +1000</code>\n' +
  '<code>/모으기변경 개별주 NVDA,AMD 빼고 -1000</code>\n' +
  '<code>/모으기변경 ETF 전부 모으기중단</code>';

function parse(text, botUsername) {
  if (typeof text !== 'string') return null;
  const match = /^\/([^\s@]+)(?:@([A-Za-z0-9_]+))?(?:\s+([\s\S]*))?$/.exec(text.trim());
  if (!match || (match[2] && match[2].toLowerCase() !== botUsername?.toLowerCase())) {
    return null;
  }
  return { command: ALIASES.get(match[1]) || null,
    raw: (match[3] || '').trim(), args: (match[3] || '').trim().split(/\s+/).filter(Boolean) };
}

function requireGateway(gateway, method) {
  if (typeof gateway?.[method] !== 'function') throw new Error(`gateway ${method} unavailable`);
}

function mutationAllowed(signal) {
  if (signal?.aborted) throw new DOMException('cancelled before mutation', 'AbortError');
}

async function planAnalyses(gateway, plans, snapshot) {
  if (!gateway.analysisProvider || !plans.length) return {};
  requireGateway(gateway, 'analyzePlans');
  return gateway.analyzePlans(plans, snapshot);
}

// Deterministic proposal from current plans and the owner's policy. The token
// binds an approval to the exact policy version, plan versions and amounts shown.
// With `known` plans the result is for display only: nothing is adjustable.
async function buildProposal(gateway, known = null) {
  for (const method of known ? ['currentPolicy'] :
    ['listPlans', 'currentPolicy', 'editablePlanKeys']) requireGateway(gateway, method);
  const [plans, state, editable] = await Promise.all([known || gateway.listPlans(true),
    gateway.currentPolicy(), known ? new Set() : gateway.editablePlanKeys()]);
  const inputs = plans.filter(plan => plan.enabled).map(plan => {
    const label = `${plan.instrument.market}:${plan.instrument.ticker}`;
    return { id: plan.planKey || `${plan.broker}:${label}`, planKey: plan.planKey ?? null,
      version: plan.version ?? null, ticker: plan.instrument.ticker, label,
      currency: plan.currency, dailyAmount: plan.dailyAmount,
      days: new Set(plan.scheduledWeekdays).size,
      adjustable: Boolean(plan.planKey) && Number.isSafeInteger(plan.version) &&
        editable.has(plan.planKey),
      groups: [[exposure(plan, null), '1']] };
  });
  const results = evaluate(inputs, state.policy);
  const fingerprint = proposalFingerprint(state.version, results);
  let token = null;
  if (fingerprint) {
    const bytes = new Uint8Array(await crypto.subtle.digest('SHA-256',
      new TextEncoder().encode(fingerprint)));
    token = [...bytes.slice(0, 4)].map(byte => byte.toString(16).padStart(2, '0')).join('');
  }
  return { state, results, token };
}

async function replyFor(parsed, gateway, updateId, signal, delivery) {
  const { command, raw, args } = parsed;
  if (command === 'start') return START;
  if (command === 'help') return HELP;
  if (command === 'history') {
    const kind = args[0]?.toLowerCase();
    if (!['accounts', 'ledger', 'reports', 'occurrences', 'snapshots'].includes(kind) ||
        (args.length > 2) || (args[1] && !/^\d{1,12}$/.test(args[1]))) {
      return '사용법: <code>/history accounts|ledger|reports|occurrences|snapshots [cursor]</code>';
    }
    requireGateway(gateway, 'legacyHistory');
    const cursor = args[1] ? Number(args[1]) : -1;
    if (!Number.isSafeInteger(cursor)) return '잘못된 페이지 위치입니다.';
    return formatLegacyHistory(kind, await gateway.legacyHistory(kind, { cursor }));
  }
  if (['assets', 'portfolio', 'accounts'].includes(command)) {
    requireGateway(gateway, 'refresh');
    const refresh = await gateway.refresh({ force: true, allBrokers: command === 'accounts' });
    if (command === 'assets') return formatAssets(refresh.snapshot, refresh);
    if (command === 'portfolio') return formatPortfolio(refresh.snapshot);
    return formatAccounts(refresh.accountResults, refresh.collectedAt,
      refresh.snapshot.isComplete);
  }
  if (command === 'stock') return stockReply(args[0] || '', gateway.marketProvider,
    gateway.lastSnapshot?.positions || []);
  if (command === 'recurring') {
    requireGateway(gateway, 'refresh');
    requireGateway(gateway, 'listPlans');
    const [refresh, plans] = await Promise.all([gateway.refresh({ force: false,
      allBrokers: false }), gateway.listPlans(true)]);
    const analyses = await planAnalyses(gateway, plans, refresh.snapshot);
    const report = formatRecurring(plans, analyses);
    // Optional addendum; a gateway without policy support keeps the legacy reply.
    if (typeof gateway.currentPolicy !== 'function') return report;
    const proposal = await buildProposal(gateway, plans);
    return report + policyNotice(proposal.results, proposal.state.policy);
  }
  if (command === 'policy') {
    requireGateway(gateway, 'currentPolicy');
    const state = await gateway.currentPolicy();
    if (!state.installed) return '목표·한도 저장소가 아직 준비되지 않았습니다.';
    if (!args.length) return formatPolicy(state.policy, state.version);
    const edit = editPolicy(state.policy, args, exposureLabels);
    if (edit.error) return edit.error;
    requireGateway(gateway, 'savePolicy');
    mutationAllowed(signal);
    if (delivery) delivery.mutationAttempted = true;
    const render = saved => '✅ 목표·한도를 저장했습니다.\n\n' +
      formatPolicy(saved.policy, saved.version);
    const saved = await gateway.savePolicy({ policy: edit.policy, expectedVersion: state.version,
      mutationKey: `update:${updateId}`, delivery: delivery && { ...delivery, render } });
    return render(saved);
  }
  if (command === 'recommend') {
    const proposal = await buildProposal(gateway);
    return formatEvaluation(proposal.results, proposal.token);
  }
  if (command === 'recommend_apply') {
    if (args.length !== 1 || !/^[0-9a-f]{8}$/.test(args[0])) {
      return '사용법: <code>/추천적용 번호</code> · 번호는 <code>/추천</code> 결과에 표시됩니다.';
    }
    requireGateway(gateway, 'applyAmounts');
    requireGateway(gateway, 'replayedGroup');
    const mutationKey = `update:${updateId}`;
    const render = changed => `✅ <b>추천 ${changed.length}종목 반영</b>\n` +
      html([...changed].sort((a, b) => a.instrument.ticker.localeCompare(b.instrument.ticker))
        .map(plan => `${plan.instrument.ticker}(${money(plan.dailyAmount, plan.currency)})`)
        .join(', ')) + '\n\n봇의 모으기 계획만 변경했으며 증권사 주문은 실행하지 않았습니다.';
    const replayed = await gateway.replayedGroup(mutationKey);
    if (replayed) return render(replayed);
    const proposal = await buildProposal(gateway);
    if (proposal.token !== args[0]) {
      return '번호가 현재 계산과 맞지 않습니다. 금액이나 설정이 바뀌었을 수 있으니 ' +
        '<code>/추천</code>을 다시 확인해 주세요. 변경된 것은 없습니다.';
    }
    mutationAllowed(signal);
    if (delivery) delivery.mutationAttempted = true;
    const changes = proposal.results.flatMap(result => result.changes).map(change => ({
      planKey: change.plan.planKey, expectedVersion: change.plan.version,
      dailyAmount: change.to }));
    return render(await gateway.applyAmounts({ changes, mutationKey,
      delivery: delivery && { ...delivery, render } }));
  }
  if (command === 'rebalance') {
    requireGateway(gateway, 'refresh');
    requireGateway(gateway, 'listPlans');
    const [refresh, plans] = await Promise.all([gateway.refresh({ force: false,
      allBrokers: false }), gateway.listPlans(true)]);
    return rebalanceReply(args[0] || '', refresh.snapshot, plans);
  }
  if (command === 'weekly') {
    requireGateway(gateway, 'refresh');
    requireGateway(gateway, 'listPlans');
    const [refresh, plans] = await Promise.all([gateway.refresh({ force: true,
      allBrokers: false }), gateway.listPlans(true)]);
    const analyses = await planAnalyses(gateway, plans, refresh.snapshot);
    return buildPeriodicReport('weekly', refresh.snapshot, refresh, plans, analyses);
  }
  if (command === 'analyze') {
    if (!args.length) return '사용법: <code>/analyze AMD</code>';
    if (!gateway.analysisProvider) return '시장데이터 provider가 설정되지 않아 분석을 보류합니다.';
    requireGateway(gateway, 'refresh');
    requireGateway(gateway, 'listPlans');
    const [market, ticker] = instrumentParts(args[0], gateway.lastSnapshot?.positions || []);
    const [refresh, plans] = await Promise.all([gateway.refresh({ force: false,
      allBrokers: false }), gateway.listPlans(false)]);
    const target = plans.find(plan => plan.instrument.ticker === ticker &&
      (plan.instrument.market === market || plan.instrument.market === 'US'));
    return formatAnalysis(await analyzeWithProvider(gateway.analysisProvider,
      { market, ticker, snapshot: refresh.snapshot,
        targetWeightPct: target?.targetWeightPct ?? null }));
  }
  if (command === 'recurring_add') {
    if (args.length < 3) return '사용법: <code>/recurring_add AMD 20USD 8%</code>';
    let [market, ticker] = instrumentParts(args[0], gateway.lastSnapshot?.positions || []);
    if (!args[0].includes(':') && !/^\d+$/.test(args[0]) &&
        !(gateway.lastSnapshot?.positions || []).some(p => p.instrument.ticker === ticker)) market = 'US';
    const instrumentCurrency = KR_MARKETS.has(market) ? 'KRW' : US_MARKETS.has(market) ? 'USD' : null;
    if (!instrumentCurrency) return '지원 시장을 명시해 주세요: US 또는 KOSPI/KOSDAQ.';
    const amountMatch = /^(\d+(?:\.\d+)?)(USD|KRW)$/i.exec(args[1]);
    const weightMatch = /^(\d+(?:\.\d+)?)%$/.exec(args[2]);
    if (!amountMatch || !weightMatch) {
      return '금액은 20USD/10000KRW, 목표는 8% 형식이어야 합니다.';
    }
    const currency = amountMatch[2].toUpperCase();
    if (compare(decimal(amountMatch[1]), decimal('0')) <= 0 ||
        compare(decimal(weightMatch[1]), decimal('100')) > 0) {
      return '금액은 0보다 커야 하고 목표 비중은 0~100%여야 합니다.';
    }
    if (currency === 'USD' && KR_MARKETS.has(market)) {
      return '한국 종목 적립금은 KRW로 입력해 주세요.';
    }
    requireGateway(gateway, 'createPlan');
    mutationAllowed(signal);
    if (delivery) delivery.mutationAttempted = true;
    const render = plan => `✅ <b>${html(`${plan.instrument.market}:${plan.instrument.ticker}`)}</b> ` +
      `로컬 적립 계획을 저장했습니다.\n매일 ${money(plan.dailyAmount, plan.currency)}` +
      ` · 목표 ${plan.targetWeightPct}%\n종목 메타데이터는 사용자 입력이며 검증 전입니다. 환전·주문은 실행되지 않습니다.`;
    const plan = await gateway.createPlan({ market, ticker, amount: amountMatch[1],
      currency, instrumentCurrency, targetWeightPct: weightMatch[1], memo: args.slice(3).join(' '),
      mutationKey: `update:${updateId}`, delivery: delivery && { ...delivery, render } });
    return render(plan);
  }
  if (command === 'recurring_set') {
    if (args.length !== 5 || !/^[A-Za-z0-9._:-]{1,128}$/.test(args[0]) ||
        !/^[0-9]+(?:\.[0-9]+)?$/.test(args[1]) || !['KRW','USD'].includes(args[2]) ||
        !['ON','OFF'].includes(args[3].toUpperCase()) || !/^[1-9][0-9]*$/.test(args[4]) ||
        !Number.isSafeInteger(Number(args[4]))) {
      return '사용법: <code>/recurring_set 계획키 금액 KRW ON 현재버전</code> · 봇 계획만 변경, 증권사 주문 없음';
    }
    requireGateway(gateway, 'editPlan');
    mutationAllowed(signal);
    if (delivery) delivery.mutationAttempted = true;
    const render = plan => `✅ ${html(plan.planKey)} · ${money(plan.dailyAmount, plan.currency)} · ` +
      `${plan.enabled ? 'ON' : 'OFF'} · 버전 ${plan.version}\n봇의 모으기 계획만 변경했습니다. 증권사 주문은 실행하지 않습니다.`;
    const plan = await gateway.editPlan({ planKey: args[0], amount: args[1], currency: args[2],
      enabled: args[3].toUpperCase() === 'ON', expectedVersion: Number(args[4]),
      mutationKey: `update:${updateId}`, delivery: delivery && { ...delivery, render } });
    return render(plan);
  }
  if (command === 'recurring_update') {
    if (args.length < 2) return USAGE_UPDATE;
    const group = args[0].toUpperCase();
    const assetType = group === 'ETF' ? 'ETF' :
      ['개별주', '주식', 'EQUITY'].includes(group) ? 'EQUITY' : null;
    if (!assetType) return '첫 항목은 ETF 또는 개별주여야 합니다.\n' + USAGE_UPDATE;
    const text = args.slice(1).join(' ');
    const stop = /(?:모으기)?(?:중단|정지)$/.test(text);
    const delta = stop ? null : /([+-]\d[\d,]*)(?:원)?$/.exec(text)?.[1]?.replaceAll(',', '');
    if (!stop && delta == null) return USAGE_UPDATE;
    if (!stop && (compare(decimal(delta), decimal('0')) === 0 ||
        decimal(delta).coefficient % 1000n !== 0n)) {
      return '변경 금액은 1,000원 단위의 0이 아닌 값이어야 합니다.';
    }
    const excluded = text.includes('빼고') ? text.split('빼고')[0]
      .replaceAll('전부', '').split(/[\s,]+/).filter(Boolean).map(value => value.toUpperCase()) : [];
    requireGateway(gateway, 'changeGroup');
    mutationAllowed(signal);
    if (delivery) delivery.mutationAttempted = true;
    const render = changed => {
      if (!changed.length) return '조건에 맞는 변경 대상이 없습니다.';
      const action = stop ? '모으기 중단' : `${compare(decimal(delta), decimal('0')) > 0 ? '+' : ''}` +
        `${display(decimal(delta), 0)}원 변경`;
      const details = [...changed].sort((a, b) =>
        a.instrument.ticker.localeCompare(b.instrument.ticker))
        .map(plan => `${plan.instrument.ticker}(${money(plan.dailyAmount, plan.currency)})`).join(', ');
      return `✅ <b>${assetType === 'ETF' ? 'ETF' : '개별주'} ${changed.length}종목</b> ` +
        `${action}\n${html(details)}\n\n` +
        '봇의 모으기 계획만 변경했으며 증권사 주문은 실행하지 않았습니다.';
    };
    const changed = await gateway.changeGroup({ assetType, delta, stop, excluded,
      mutationKey: `update:${updateId}`, delivery: delivery && { ...delivery, render } });
    return render(changed);
  }
  if (command === 'thesis') {
    if (args.length < 2) return '사용법: <code>/thesis AMD 투자논리...</code>';
    const [market, ticker] = instrumentParts(args[0], gateway.lastSnapshot?.positions || []);
    const thesis = args.slice(1).join(' ').trim();
    if (thesis.length > 4000) return 'Thesis는 4,000자 이내로 입력해 주세요.';
    requireGateway(gateway, 'setThesis');
    mutationAllowed(signal);
    if (delivery) delivery.mutationAttempted = true;
    const render = saved => `📝 <b>${html(saved.market)}:${html(saved.ticker)}</b> ` +
      'Thesis revision을 저장했습니다.';
    const saved = await gateway.setThesis({ market, ticker, thesis,
      mutationKey: `update:${updateId}`, delivery: delivery && { ...delivery, render } });
    return render(saved);
  }
  return null;
}

export async function routeUpdate({ headerEntries, body, secret, allowedUserIds,
  botUsername = null, gateway, signal, atomicDelivery = false,
  now = () => new Date().toISOString() }) {
  const updateId = admittedUpdateId(headerEntries, body, secret, allowedUserIds);
  if (updateId === null) return { status: 'DENIED', updateId: null, reply: null };
  const update = JSON.parse(new TextDecoder().decode(body));
  const parsed = parse(update.message?.text || '', botUsername);
  if (!parsed?.command) return { status: 'IGNORED', updateId, reply: null };
  const delivery = atomicDelivery ? { updateId,
    destination: String(update.message.chat.id), now: now() } : null;
  try {
    const reply = await replyFor(parsed, gateway, updateId, signal, delivery);
    return { status: reply === null ? 'IGNORED' : 'READY', updateId, reply,
      ...(atomicDelivery ? { mutationAttempted: Boolean(delivery.mutationAttempted) } : {}) };
  } catch (error) {
    if (error?.name === 'AbortError') return { status: 'CANCELLED', updateId, reply: null };
    return { status: 'ERROR', updateId, reply: FAILURE };
  }
}
