import { compare, decimal, exact } from './finance.mjs';
import { html, localTime, money, percent } from './assets-report.mjs';

const labels = new Map([['kiwoom', '키움'], ['kiwoom_main', '키움 메인'],
  ['kiwoom_accum', '키움 모으기'], ['toss', '토스'], ['meritz', '메리츠']]);
const statuses = new Map([['ok', '정상'], ['partial', '일부만 조회'],
  ['stale', '마지막 정상값'], ['not_configured', '미설정'],
  ['auth_failed', '인증 실패'], ['rate_limited', '호출 제한'],
  ['unavailable', '조회 실패']]);
const SAFE_MASK = /^\*{3,}(?:[A-Za-z_-]{1,16}|[0-9]{1,4})$/;

function normalizedQuantity(value) {
  let result = exact(decimal(value));
  if (result.includes('.')) result = result.replace(/0+$/, '').replace(/\.$/, '');
  return result;
}

export function formatAccounts(accountResults, collectedAt, isComplete) {
  const lines = ['🏦 <b>증권사별 계좌</b>'];
  for (const result of accountResults) {
    lines.push('', `<b>${html(labels.get(result.broker) || result.broker)}</b> · ` +
      statuses.get(result.status));
    if (result.items.length) {
      for (const account of result.items) {
        // Do not render account_id, even if a provider accidentally supplies it.
        const label = SAFE_MASK.test(account.displayName || '') ? account.displayName : '등록 계좌';
        lines.push(`• ${html(label)}`);
      }
    } else lines.push('• 표시 가능한 계좌 없음');
  }
  lines.push('', '전체 계좌번호는 저장·표시하지 않습니다.',
    html(`기준 ${localTime(collectedAt)} KST · ${isComplete ? '전체 조회' : '불완전 조회'}`));
  return lines.join('\n');
}

export function formatPortfolio(snapshot, limit = 30) {
  if (!Number.isSafeInteger(limit) || limit < 0) throw new TypeError('invalid limit');
  const scope = snapshot.expectedBrokers?.length === 1 &&
    snapshot.expectedBrokers[0] === 'kiwoom_main' ? '키움 메인' : '선택 출처';
  const title = snapshot.isComplete ? `📊 <b>${scope} 포트폴리오</b>` :
    `⚠️ <b>${scope} 포트폴리오 부분 조회</b>`;
  const rankingValue = item => item.marketValueBase === null ||
    compare(decimal(item.marketValueBase), decimal('0')) === 0 ? decimal('-1') :
    decimal(item.marketValueBase);
  const positions = [...snapshot.positions].sort((a, b) =>
    compare(rankingValue(b), rankingValue(a)));
  const lines = [title];
  if (!positions.length) lines.push('', '조회된 보유 종목이 없습니다.');
  positions.slice(0, limit).forEach((position, index) => {
    const name = html(position.companyName || position.instrument.ticker);
    const identity = html(`${position.instrument.market}:${position.instrument.ticker}`);
    lines.push('', `<b>${index + 1}. ${name} (${identity})</b>`);
    lines.push(position.marketValueBase === null ? '평가액 확인 불가' :
      `평가액 ${money(position.marketValueBase, snapshot.baseCurrency)}`);
    lines.push(`비중 ${percent(position.weightPct === null ? null : decimal(position.weightPct))}`);
    lines.push(`수익률 ${percent(position.returnPct === null ? null : decimal(position.returnPct))}`);
    if (position.brokerQuantities.length > 1) {
      const detail = position.brokerQuantities.map(([broker, quantity]) =>
        `${html(broker)} ${normalizedQuantity(quantity)}주`).join(' · ');
      lines.push(`합산 ${normalizedQuantity(position.quantity)}주 (${detail})`);
    }
  });
  if (positions.length > limit) lines.push('', `외 ${positions.length - limit}개 종목`);
  if (snapshot.isComplete && snapshot.totalMarketValue !== null &&
      compare(decimal(snapshot.totalMarketValue), decimal('0')) > 0 &&
      positions.length && positions[0].weightPct !== null &&
      compare(decimal(positions[0].weightPct), decimal('15')) >= 0) {
    const top = positions[0];
    lines.push('', '⚠️ <b>집중도</b>',
      `${html(`${top.instrument.market}:${top.instrument.ticker}`)} ` +
      `비중이 ${percent(decimal(top.weightPct))}입니다.`);
  }
  if (!snapshot.isComplete) {
    lines.push('', '비중·집중도는 누락 증권사를 0원으로 두지 않기 위해 보류됩니다.');
  }
  lines.push('', '섹터·국가 분류는 시장데이터가 확인된 종목만 주간 분석에 반영됩니다.');
  return lines.join('\n');
}
