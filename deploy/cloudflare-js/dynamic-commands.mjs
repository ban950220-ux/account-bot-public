// Pure dynamic reply boundaries. External reads are injected through contracts.
import { html, money } from './assets-report.mjs';
import { readPrice } from './provider-contracts.mjs';
import { compare, decimal } from './finance.mjs';
import { formatRebalance, suggestNewMoneyRebalance } from './rebalance.mjs';

export function instrumentParts(raw, positions = []) {
  const token = raw.trim().toUpperCase();
  if (token.includes(':')) {
    const index = token.indexOf(':');
    const market = token.slice(0, index);
    const ticker = token.slice(index + 1);
    if (market && ticker) return [market, ticker];
  }
  const markets = new Set(positions.filter(position => position.instrument.ticker === token)
    .map(position => position.instrument.market));
  if (markets.size === 1) return [[...markets][0], token];
  return /^\d+$/.test(token) ? ['KOSPI', token.padStart(6, '0')] : ['NASDAQ', token];
}

export async function stockReply(raw, provider, positions = []) {
  if (!raw.trim()) return '사용법: <code>/stock AMD</code>';
  if (!provider) return '시장데이터 provider가 설정되지 않았습니다.';
  const [market, ticker] = instrumentParts(raw, positions);
  const quote = await readPrice(provider, market, ticker);
  if (quote === null) return '현재가를 확인할 수 없습니다. 잠시 후 다시 시도해 주세요.';
  const at = new Date(quote.asOf);
  if (Number.isNaN(at.getTime())) return '현재가를 확인할 수 없습니다. 잠시 후 다시 시도해 주세요.';
  const timestamp = at.toISOString().slice(0, 16).replace('T', ' ');
  return `📈 <b>${html(market)}:${html(ticker)}</b>\n\n` +
    `현재가 ${money(quote.price, quote.currency)}` +
    (quote.stale ? ' · 마지막 정상값' : '') + '\n' +
    `기준 ${timestamp} UTC · ${html(quote.provider)}`;
}

export function rebalanceReply(rawAmount, snapshot, plans) {
  const raw = rawAmount.trim().replaceAll(',', '');
  let amount;
  try {
    amount = raw === '' ? decimal('0') : decimal(raw);
  } catch {
    return '사용법: <code>/rebalance 500000</code> (원화 신규 자금)';
  }
  if (compare(amount, decimal('0')) < 0) return '신규 자금은 0 이상이어야 합니다.';
  const targets = plans.filter(plan => plan.enabled && plan.targetWeightPct != null)
    .map(plan => ({ instrument: plan.instrument, targetWeightPct: plan.targetWeightPct }));
  if (!targets.length) return '목표 비중이 있는 적립 계획을 먼저 등록해 주세요.';
  return formatRebalance(suggestNewMoneyRebalance(snapshot, targets, raw || '0'));
}
