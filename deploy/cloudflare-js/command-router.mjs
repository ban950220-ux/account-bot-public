// Owner-only Telegram command planning. Returns HTML; it never sends a message.
import { admittedUpdateId } from './boundary.mjs';
import { START, HELP } from './static-commands.mjs';
import { formatAssets, html, money } from './assets-report.mjs';
import { formatPortfolio, formatAccounts } from './portfolio-report.mjs';
import { exposure, exposureLabels, formatRecurring } from './recurring-report.mjs';
import { editPolicy, evaluate, formatEvaluation, formatPolicy, formatWeeklySummary, policyNotice,
  proposalFingerprint } from './allocation-policy.mjs';
import { stockReply, instrumentParts, rebalanceReply } from './dynamic-commands.mjs';
import { buildPeriodicReport } from './periodic-report.mjs';
import { analyzeWithProvider } from './analysis-service.mjs';
import { formatAnalysis } from './analysis-report.mjs';
import { compare, decimal, display } from './finance.mjs';
import { formatLegacyHistory } from './legacy-history-report.mjs';
import { changeEvents, diffToMoment, formatKst, momentArgs, momentToken, parseKstMoment,
  weeklyKrw } from './plan-history.mjs';
import { collate } from './collate.mjs';
import { COMMAND_KINDS } from './precomputed-store.mjs';

const ALIASES = new Map([
  ['start', 'start'], ['help', 'help'], ['도움말', 'help'],
  ['assets', 'assets'], ['자산', 'assets'], ['portfolio', 'portfolio'],
  ['포트폴리오', 'portfolio'],
  ['recurring', 'recurring'], ['모으기', 'recurring'],
  ['recurring_add', 'recurring_add'], ['모으기추가', 'recurring_add'],
  ['recurring_set', 'recurring_set'], ['모으기설정', 'recurring_set'],
  ['recurring_update', 'recurring_update'], ['모으기변경', 'recurring_update'],
  ['recurring_archive', 'recurring_archive'], ['모으기삭제', 'recurring_archive'],
  ['모으기보관', 'recurring_archive'],
  ['recurring_restore', 'recurring_restore'], ['모으기복원', 'recurring_restore'],
  ['recurring_at', 'recurring_at'], ['모으기시점', 'recurring_at'],
  ['recurring_history', 'recurring_history'], ['모으기이력', 'recurring_history'],
  ['stock', 'stock'], ['종목', 'stock'], ['analyze', 'analyze'], ['분석', 'analyze'],
  ['thesis', 'thesis'], ['rebalance', 'rebalance'], ['리밸런싱', 'rebalance'],
  ['weekly', 'weekly'], ['주간리포트', 'weekly'],
  ['history', 'history'], ['이력', 'history'],
  ['policy', 'policy'], ['목표', 'policy'], ['recommend', 'recommend'], ['추천', 'recommend'],
  ['recommend_apply', 'recommend_apply'], ['추천적용', 'recommend_apply'],
  ['signals', 'signals'], ['분석갱신', 'signals'],
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
// `adjust` with known plans keeps them adjustable without listing them again.
async function buildProposal(gateway, known = null, adjust = !known) {
  for (const method of [...(known ? [] : ['listPlans']), 'currentPolicy',
    ...(adjust ? ['editablePlanKeys'] : [])]) requireGateway(gateway, method);
  const [plans, state, editable] = await Promise.all([known || gateway.listPlans(true),
    gateway.currentPolicy(), adjust ? gateway.editablePlanKeys() : new Set()]);
  let signals = new Map();
  if (typeof gateway.signals?.read === 'function') {
    try { signals = await gateway.signals.read(plans); }
    catch { /* Missing scores leave amounts unchanged; they never block the reply. */ }
  }
  const inputs = plans.filter(plan => plan.enabled).map(plan => {
    const label = `${plan.instrument.market}:${plan.instrument.ticker}`;
    return { id: plan.planKey || `${plan.broker}:${label}`, planKey: plan.planKey ?? null,
      version: plan.version ?? null, ticker: plan.instrument.ticker, label,
      broker: plan.broker ?? null,
      signal: signals.get(`${label}:${plan.instrument.assetType}:${plan.instrument.currency}`) ?? null,
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
  return { state, results, token, signals };
}

// Scores and the recurring summary for the weekly report. Both are optional:
// a gateway without policy support, or any failure here, keeps the plain report.
export async function weeklyExtras(gateway, plans) {
  try {
    if (typeof gateway.currentPolicy !== 'function' ||
        typeof gateway.editablePlanKeys !== 'function') throw new Error('no policy support');
    const proposal = await buildProposal(gateway, plans, true);
    return { signals: proposal.signals,
      summary: proposal.state.installed === false ? null :
        formatWeeklySummary(proposal.results, proposal.token) };
  } catch {
    let signals = new Map();
    try { if (typeof gateway.signals?.read === 'function') signals = await gateway.signals.read(plans); }
    catch { /* Scores are optional; the report is not. */ }
    return { signals, summary: null };
  }
}

const SET_PREFIX = /^\/(?:모으기설정|recurring_set)(?:@[A-Za-z0-9_]+)?(?=\s|$)/i;
const SET_LIMIT = 30;
const SET_USAGE = '한 줄에 하나씩 보내 주세요.\n<code>/모으기설정 QQQ 4000</code>\n' +
  '<code>/모으기설정 MPWR OFF</code>\n<code>/모으기설정 MPWR 2000 ON</code>';

// All-or-nothing: any bad line changes nothing and names the line to fix.
async function setPlansBatch(lines, gateway, updateId, signal, delivery) {
  if (lines.length > SET_LIMIT) {
    return `한 번에 ${SET_LIMIT}줄까지 바꿀 수 있습니다. 나눠서 보내 주세요. 변경된 것은 없습니다.`;
  }
  for (const method of ['listPlans', 'editPlans', 'replayedGroup']) requireGateway(gateway, method);
  const mutationKey = `update:${updateId}`;
  const label = plan => `${plan.instrument.market}:${plan.instrument.ticker}`;
  const render = (changed, unchanged = []) => {
    const rows = [...changed].sort((a, b) => collate(label(a), label(b)))
      .map(plan => `<b>${html(label(plan))}</b> ${money(plan.dailyAmount, plan.currency)} · ` +
        `${plan.enabled ? 'ON' : 'OFF'}`);
    return [`✅ <b>모으기 ${changed.length}종목 변경</b>`, ...rows,
      ...(unchanged.length ? ['', `변경 없음(이미 같은 값): ${html(unchanged.join(', '))}`] : []),
      '', '봇의 모으기 계획만 변경했습니다. 증권사 주문은 실행하지 않습니다.'].join('\n');
  };
  const replayed = await gateway.replayedGroup(mutationKey);
  if (replayed) return render(replayed);
  const plans = await gateway.listPlans(false);
  const errors = [];
  const seen = new Set();
  const work = [];
  const unchanged = [];
  lines.forEach((line, index) => {
    const fail = reason => errors.push(`${index + 1}번째 줄 <code>${html(line)}</code>: ${reason}`);
    const [name, ...rest] = line.split(/\s+/);
    const flags = rest.filter(value => /^(ON|OFF)$/i.test(value));
    const amounts = rest.filter(value => /^\d[\d,]*(?:원)?$/.test(value));
    if (!rest.length || flags.length > 1 || amounts.length > 1 ||
        flags.length + amounts.length !== rest.length ||
        !/^[A-Za-z0-9.]+(?::[A-Za-z0-9.]+)?$/.test(name)) return fail('형식 오류');
    const amount = amounts[0]?.replace(/[,원]/g, '');
    if (amount !== undefined && compare(decimal(amount), decimal('0')) === 0) {
      return fail('0원은 쓸 수 없습니다. 멈추려면 OFF를 써 주세요');
    }
    const [first, second] = name.toUpperCase().split(':');
    const ticker = second ?? first;
    const market = second ? first : null;
    const matches = plans.filter(plan => plan.planKey && plan.instrument.ticker === ticker &&
      (!market || plan.instrument.market === market));
    if (!matches.length) return fail('계획을 찾지 못했습니다');
    if (matches.length > 1) return fail('같은 종목 계획이 여러 개입니다. 한 줄 명령으로 따로 바꿔 주세요');
    const [target] = matches;
    if (seen.has(target.planKey)) return fail('같은 종목이 두 번 나왔습니다');
    seen.add(target.planKey);
    const dailyAmount = amount ?? target.dailyAmount;
    const enabled = flags.length ? flags[0].toUpperCase() === 'ON' : target.enabled;
    if (compare(decimal(dailyAmount), decimal(target.dailyAmount)) === 0 &&
        enabled === target.enabled) {
      unchanged.push(label(target));
      return;
    }
    work.push({ planKey: target.planKey, expectedVersion: target.version, dailyAmount, enabled });
  });
  if (errors.length) {
    return ['변경된 것은 없습니다. 아래 줄을 고쳐 전체를 다시 보내 주세요.', ...errors, '',
      SET_USAGE].join('\n');
  }
  if (!work.length) return `바꿀 내용이 없습니다. 이미 같은 값입니다: ${html(unchanged.join(', '))}`;
  mutationAllowed(signal);
  if (delivery) delivery.mutationAttempted = true;
  const changed = await gateway.editPlans({ changes: work, mutationKey,
    delivery: delivery && { ...delivery, render: plans => render(plans, unchanged) } });
  return render(changed, unchanged);
}

const ARCHIVE_NOTE = '봇의 모으기 계획만 변경했습니다. 증권사 주문은 실행하지 않습니다.';

// /모으기삭제 hides a plan (turning it OFF); /모으기복원 brings it back OFF.
// Rows and revisions are kept, so either can be undone.
async function archiveReply(command, args, gateway, updateId, signal, delivery) {
  const archive = command === 'recurring_archive';
  const verb = archive ? '모으기삭제' : '모으기복원';
  if (args.length !== 1 || !/^[A-Za-z0-9._-]+(?::[A-Za-z0-9._-]+)*$/.test(args[0]) ||
      args[0].length > 128) {
    return `사용법: <code>/${verb} ROK</code> · <code>/${verb} 계획키</code>\n` +
      '삭제는 목록·합계·보고서에서 빼는 보관이며, <code>/모으기복원</code>으로 되살릴 수 있습니다.';
  }
  for (const method of ['planArchiveInstalled', 'archiveTargets', 'replayedArchive', 'setArchived']) {
    requireGateway(gateway, method);
  }
  const mutationKey = `update:${updateId}`;
  const label = plan => `${plan.instrument.market}:${plan.instrument.ticker}`;
  const render = (plan, archived) => archived ?
    `🗄 <b>${html(label(plan))}</b> 계획을 삭제(보관)했습니다.\n` +
      `<code>${html(plan.planKey)}</code> · ${money(plan.dailyAmount, plan.currency)} · OFF\n` +
      '목록·합계·보고서·종목 찾기에서 빠집니다. 변경 이력은 남습니다.\n' +
      `되살리기: <code>/모으기복원 ${html(plan.planKey)}</code>\n${ARCHIVE_NOTE}` :
    `♻️ <b>${html(label(plan))}</b> 계획을 복원했습니다.\n` +
      `<code>${html(plan.planKey)}</code> · ${money(plan.dailyAmount, plan.currency)} · ` +
      `${plan.enabled ? 'ON' : 'OFF'}\n` +
      (plan.enabled ? '' : `다시 모으려면: <code>/모으기설정 ${html(plan.planKey)} ` +
        `${plan.dailyAmount} ${plan.currency} ON ${plan.version}</code>\n`) + ARCHIVE_NOTE;
  const replayed = await gateway.replayedArchive(mutationKey);
  if (replayed) return render(replayed.plan, replayed.archived);
  if (!await gateway.planArchiveInstalled()) {
    return '삭제(보관) 기능이 아직 운영 DB에 설치되지 않았습니다. 변경된 것은 없습니다.';
  }
  const targets = await gateway.archiveTargets(args[0], !archive);
  if (!targets.length) {
    return archive ? `${html(args[0])} 계획을 찾지 못했습니다. 이미 삭제(보관)했을 수 있습니다.` :
      `보관된 ${html(args[0])} 계획이 없습니다.`;
  }
  if (targets.length > 1) {
    return '같은 종목의 계획이 여러 개입니다. 변경된 것은 없습니다. 아래 중 하나를 보내 주세요.\n' +
      targets.map(plan => `<code>/${verb} ${html(plan.planKey)}</code> · ` +
        `${money(plan.dailyAmount, plan.currency)} · ${plan.enabled ? 'ON' : 'OFF'}`).join('\n');
  }
  const [target] = targets;
  mutationAllowed(signal);
  if (delivery) delivery.mutationAttempted = true;
  const plan = await gateway.setArchived({ planKey: target.planKey,
    expectedVersion: target.version, archived: archive, mutationKey,
    delivery: delivery && { ...delivery, render: after => render(after, archive) } });
  return render(plan, archive);
}

const MOMENT_USAGE = '사용법: <code>/모으기시점 11/20</code> · <code>/모으기시점 11/20 08:00</code>' +
  ' · <code>/모으기시점 2026-11-20 08:00:30</code>\n시각은 KST, 날짜만 쓰면 그날 23:59:59입니다.';
const MOMENT_PREVIEW_LIMIT = 40;
const planLabel = plan => `${plan.instrument.market}:${plan.instrument.ticker}`;
const stateText = (amount, currency, enabled) => `${money(amount, currency)} ${enabled ? 'ON' : 'OFF'}`;

// /모으기시점: compare every live editable plan with a past moment; with the token
// shown in the preview, put amounts and ON/OFF back as they were (later plans go OFF).
async function momentReply(args, gateway, updateId, signal, delivery) {
  const token = args.length && /^[0-9a-f]{8}$/.test(args[args.length - 1]) ? args.pop() : null;
  if (!args.length || args.length > 2) return MOMENT_USAGE;
  const moment = parseKstMoment(args[0], args[1] ?? null, new Date());
  if (!moment) return MOMENT_USAGE;
  if (moment.at > new Date()) return '미래 시점은 쓸 수 없습니다.';
  for (const method of ['planHistory', 'editPlans', 'replayedGroup']) requireGateway(gateway, method);
  const mutationKey = `update:${updateId}`;
  const render = changed => {
    const names = [...changed].sort((a, b) => collate(planLabel(a), planLabel(b)))
      .map(plan => `${plan.instrument.ticker} ${money(plan.dailyAmount, plan.currency)} ` +
        `${plan.enabled ? 'ON' : 'OFF'}`);
    return [`↩️ <b>${html(moment.label)} 시점으로 ${changed.length}종목 되돌림</b>`,
      html(names.slice(0, 30).join(' · ')) + (names.length > 30 ? ` 외 ${names.length - 30}개` : ''),
      '', '이 변경도 이력에 남으므로 <code>/모으기이력</code>에서 다시 되돌릴 수 있습니다.',
      '봇의 모으기 계획만 변경했습니다. 증권사 주문은 실행하지 않습니다.'].join('\n');
  };
  if (token) {
    const replayed = await gateway.replayedGroup(mutationKey);
    if (replayed) return render(replayed);
  }
  const { plans, revisions } = await gateway.planHistory();
  const live = plans.filter(plan => !plan.archived);
  const changes = diffToMoment(live, revisions, moment.at);
  if (!changes.length) return `${html(moment.label)} 시점과 지금 모으기 계획이 같습니다. 바꿀 것이 없습니다.`;
  const expected = await momentToken(moment.at, changes);
  if (token && token !== expected) {
    return '확인 번호가 지금 계산과 맞지 않습니다. 그 사이 계획이 바뀌었을 수 있으니 ' +
      `<code>/모으기시점 ${html(args.join(' '))}</code>로 다시 확인해 주세요. 변경된 것은 없습니다.`;
  }
  if (!token) {
    const before = weeklyKrw(live.map(plan => ({ amount: plan.dailyAmount, enabled: plan.enabled, plan })));
    const afterTargets = new Map(changes.map(change => [change.plan.planKey, change.target]));
    const after = weeklyKrw(live.map(plan => {
      const target = afterTargets.get(plan.planKey);
      return { amount: target?.dailyAmount ?? plan.dailyAmount,
        enabled: target?.enabled ?? plan.enabled, plan };
    }));
    const rows = changes.slice(0, MOMENT_PREVIEW_LIMIT).map(change =>
      `<b>${html(change.plan.instrument.ticker)}</b> ` +
      `${stateText(change.plan.dailyAmount, change.plan.currency, change.plan.enabled)} → ` +
      (change.existed ? stateText(change.target.dailyAmount, change.plan.currency,
        change.target.enabled) : '(그때 없던 계획) OFF'));
    return [`🕘 <b>${html(moment.label)} 시점과 비교</b> (다른 계획 ${changes.length}개)`, ...rows,
      ...(changes.length > MOMENT_PREVIEW_LIMIT ?
        [`… 외 ${changes.length - MOMENT_PREVIEW_LIMIT}개`] : []),
      '', `주간 합계 ${money(before.total, 'KRW')} → ${money(after.total, 'KRW')}` +
        (before.other || after.other ? ' (원화 계획만)' : ''),
      '보관한 계획은 대상에서 빠집니다. 금액과 ON/OFF만 바뀝니다.',
      `반영: <code>/모으기시점 ${html(momentArgs(moment.at))} ${expected}</code>`].join('\n');
  }
  mutationAllowed(signal);
  if (delivery) delivery.mutationAttempted = true;
  const changed = await gateway.editPlans({ changes: changes.map(change => ({
    planKey: change.plan.planKey, expectedVersion: change.plan.version,
    dailyAmount: change.target.dailyAmount, enabled: change.target.enabled })),
  mutationKey, delivery: delivery && { ...delivery, render } });
  return render(changed);
}

// /모으기이력: recent change events with a way back to just before each one,
// or the last revisions of one plan.
async function historyReply(args, gateway) {
  if (args.length > 1 || (args[0] && !/^[A-Za-z0-9._-]+(?::[A-Za-z0-9._-]+)*$/.test(args[0]))) {
    return '사용법: <code>/모으기이력</code> · <code>/모으기이력 ROK</code> · <code>/모으기이력 계획키</code>';
  }
  requireGateway(gateway, 'planHistory');
  const { plans, revisions } = await gateway.planHistory();
  const byKey = new Map(plans.map(plan => [plan.planKey, plan]));
  if (!args.length) {
    const events = changeEvents(revisions, 15);
    if (!events.length) return '모으기 계획 변경 이력이 없습니다.';
    const lines = ['🗂 <b>최근 모으기 변경</b> (최신순)'];
    for (const event of events) {
      const tickers = [...new Set(event.members.map(row => row.ticker))];
      lines.push('', `<b>${formatKst(event.at)}</b> · ${event.members.length}종목 · ` +
        html(tickers.slice(0, 4).join(', ')) + (tickers.length > 4 ? ` 외 ${tickers.length - 4}` : ''),
      `이 변경 직전으로: <code>/모으기시점 ${momentArgs(new Date(event.at.getTime() - 1000))}</code>`);
    }
    return lines.join('\n');
  }
  const name = args[0];
  let targets = plans.filter(plan => plan.planKey === name);
  if (!targets.length) {
    const parts = name.toUpperCase().split(':');
    const [ticker, market] = parts.length === 2 ? [parts[1], parts[0]] : [parts[0], null];
    targets = parts.length > 2 ? [] : plans.filter(plan => !plan.archived &&
      plan.instrument.ticker === ticker && (!market || plan.instrument.market === market));
  }
  if (!targets.length) return `${html(name)} 계획을 찾지 못했습니다.`;
  if (targets.length > 1) {
    return '같은 종목의 계획이 여러 개입니다. 아래 중 하나를 보내 주세요.\n' +
      targets.map(plan => `<code>/모으기이력 ${html(plan.planKey)}</code>`).join('\n');
  }
  const [plan] = targets;
  const rows = revisions.filter(row => row.planKey === plan.planKey)
    .sort((a, b) => b.revisionNo - a.revisionNo).slice(0, 10);
  return [`🗂 <b>${html(planLabel(byKey.get(plan.planKey)))}</b> 최근 변경` +
    (plan.archived ? ' (보관됨)' : ''), `<code>${html(plan.planKey)}</code>`, '',
  ...rows.map(row => `${formatKst(new Date(Date.parse(row.effectiveFrom)))} · ` +
    stateText(row.dailyAmount, row.currency, row.enabled))].join('\n');
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
    // A gateway without policy support keeps the plain report.
    if (typeof gateway.currentPolicy !== 'function') return formatRecurring(plans, analyses);
    const proposal = await buildProposal(gateway, plans);
    return formatRecurring(plans, analyses, proposal.signals) +
      policyNotice(proposal.results, proposal.state.policy);
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
  if (command === 'signals') {
    if (args.length > 1 || (args[0] && !/^\d{1,2}$/.test(args[0]))) {
      return '사용법: <code>/분석갱신</code> · 한 번에 일부 종목만 갱신합니다.';
    }
    if (typeof gateway.signals?.refresh !== 'function') {
      return '시장 데이터 수집이 설정되지 않았습니다.';
    }
    requireGateway(gateway, 'listPlans');
    const budget = Math.min(args[0] ? Number(args[0]) : 10, 30);
    const result = await gateway.signals.refresh(await gateway.listPlans(true), budget);
    return ['📡 <b>분석 데이터 갱신</b>',
      `이번에 ${result.refreshed}종목 갱신 · 실패 ${result.failed}` +
        (result.reasons?.length ? ` (${html(result.reasons.join(', '))})` : ''),
      `갱신이 더 필요한 종목 ${result.remaining} / 전체 ${result.symbols}`,
      result.remaining ? '남은 종목이 있으니 <code>/분석갱신</code>을 다시 보내 주세요.' :
        '모든 종목이 최신입니다. <code>/추천</code>에서 결과를 확인하세요.',
      '공개 시세·지표만 조회하며 계좌 정보는 보내지 않습니다.'].join('\n');
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
      html([...changed].sort((a, b) => collate(a.instrument.ticker, b.instrument.ticker))
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
    const extra = await weeklyExtras(gateway, plans);
    return buildPeriodicReport('weekly', refresh.snapshot, refresh, plans, analyses,
      extra.signals, extra.summary);
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
    // Several lines in one message: `/모으기설정 QQQ 4000` per line, or the command
    // alone on the first line followed by `QQQ 4000` lines. One line keeps the old path.
    const lines = raw.split(/\r?\n/).map(line => line.trim().replace(SET_PREFIX, '').trim())
      .filter(Boolean);
    if (lines.length > 1) return setPlansBatch(lines, gateway, updateId, signal, delivery);
  }
  if (command === 'recurring_set' && [2, 3].includes(args.length)) {
    // Ticker form: `/모으기설정 NVDA 3000`, `/모으기설정 NVDA OFF`, `/모으기설정 NVDA 3000 OFF`.
    const usage = '사용법: <code>/모으기설정 NVDA 3000</code> · <code>/모으기설정 NVDA OFF</code>' +
      ' · <code>/모으기설정 NVDA 3000 ON</code>\n봇 계획만 변경, 증권사 주문 없음';
    const flags = args.slice(1).filter(value => /^(ON|OFF)$/i.test(value));
    const amounts = args.slice(1).filter(value => /^\d[\d,]*(?:원)?$/.test(value));
    if (flags.length > 1 || amounts.length > 1 || flags.length + amounts.length !== args.length - 1 ||
        !/^[A-Za-z0-9.]+(?::[A-Za-z0-9.]+)?$/.test(args[0])) return usage;
    requireGateway(gateway, 'listPlans');
    requireGateway(gateway, 'editPlan');
    const [first, second] = args[0].toUpperCase().split(':');
    const ticker = second ?? first;
    const market = second ? first : null;
    const matches = (await gateway.listPlans(false)).filter(plan => plan.planKey &&
      plan.instrument.ticker === ticker && (!market || plan.instrument.market === market));
    if (!matches.length) return `${html(args[0].toUpperCase())} 계획을 찾지 못했습니다.`;
    if (matches.length > 1) {
      return '같은 종목의 계획이 여러 개입니다. 아래 중 하나로 지정해 주세요.\n' +
        matches.map(plan => `<code>/recurring_set ${html(plan.planKey)} ` +
          `${amounts[0]?.replace(/[,원]/g, '') ?? plan.dailyAmount} ${plan.currency} ` +
          `${(flags[0] ?? (plan.enabled ? 'ON' : 'OFF')).toUpperCase()} ${plan.version}</code>`)
          .join('\n');
    }
    const [target] = matches;
    mutationAllowed(signal);
    if (delivery) delivery.mutationAttempted = true;
    const render = plan => `✅ <b>${html(`${plan.instrument.market}:${plan.instrument.ticker}`)}</b> ` +
      `${money(plan.dailyAmount, plan.currency)} · ${plan.enabled ? 'ON' : 'OFF'}\n` +
      '봇의 모으기 계획만 변경했습니다. 증권사 주문은 실행하지 않습니다.';
    const plan = await gateway.editPlan({ planKey: target.planKey,
      amount: amounts[0]?.replace(/[,원]/g, '') ?? target.dailyAmount, currency: target.currency,
      enabled: flags.length ? flags[0].toUpperCase() === 'ON' : target.enabled,
      expectedVersion: target.version, mutationKey: `update:${updateId}`,
      delivery: delivery && { ...delivery, render } });
    return render(plan);
  }
  if (command === 'recurring_set') {
    if (args.length !== 5 || !/^[A-Za-z0-9._:-]{1,128}$/.test(args[0]) ||
        !/^[0-9]+(?:\.[0-9]+)?$/.test(args[1]) || !['KRW','USD'].includes(args[2]) ||
        !['ON','OFF'].includes(args[3].toUpperCase()) || !/^[1-9][0-9]*$/.test(args[4]) ||
        !Number.isSafeInteger(Number(args[4]))) {
      return '사용법: <code>/모으기설정 NVDA 3000</code> · <code>/모으기설정 NVDA OFF</code>\n' +
        '봇 계획만 변경, 증권사 주문 없음';
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
  if (command === 'recurring_at') return momentReply([...args], gateway, updateId, signal, delivery);
  if (command === 'recurring_history') return historyReply(args, gateway);
  if (command === 'recurring_archive' || command === 'recurring_restore') {
    return archiveReply(command, args, gateway, updateId, signal, delivery);
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
        collate(a.instrument.ticker, b.instrument.ticker))
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

// Read-only command replies that the Actions job precomputes. They take no
// arguments and write nothing, so the same reply can be rendered outside the Worker.
export const PRECOMPUTED_COMMANDS = Object.freeze(['recurring', 'weekly', 'recommend']);
export async function renderCommand(command, gateway) {
  if (!PRECOMPUTED_COMMANDS.includes(command)) throw new TypeError('command is not precomputed');
  return replyFor({ command, raw: '', args: [] }, gateway, null, undefined, null);
}

// One line per invocation for Workers Logs: the canonical command name only,
// never its arguments or the message text, so per-request CPU can be attributed.
export function logCommand(name) {
  if (/^[a-z_]{1,40}$/.test(name)) console.log(`cmd=${name}`);
}

export async function routeUpdate({ headerEntries, body, secret, allowedUserIds,
  botUsername = null, gateway, signal, atomicDelivery = false,
  now = () => new Date().toISOString() }) {
  const updateId = admittedUpdateId(headerEntries, body, secret, allowedUserIds);
  if (updateId === null) return { status: 'DENIED', updateId: null, reply: null };
  const update = JSON.parse(new TextDecoder().decode(body));
  const parsed = parse(update.message?.text || '', botUsername);
  logCommand(parsed?.command || 'none');
  if (!parsed?.command) return { status: 'IGNORED', updateId, reply: null };
  // A reply the Actions job precomputed from unchanged data; otherwise compute it here.
  if (PRECOMPUTED_COMMANDS.includes(parsed.command) && !parsed.args.length &&
      typeof gateway?.precomputedReply === 'function') {
    let stored = null;
    try { stored = await gateway.precomputedReply(COMMAND_KINDS[parsed.command]); }
    catch { /* Fall back to computing the reply. */ }
    if (stored) {
      return { status: 'READY', updateId, reply: stored,
        ...(atomicDelivery ? { mutationAttempted: false } : {}) };
    }
  }
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
