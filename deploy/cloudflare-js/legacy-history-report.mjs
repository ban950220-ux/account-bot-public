import { html } from './assets-report.mjs';

const KIND_LABEL = new Map([['accounts', '계좌'], ['ledger', '원장'],
  ['reports', '보고서'], ['occurrences', '납입 발생'], ['snapshots', '자산 스냅샷']]);
const SAFE_MASK = /^\*{3,}(?:[A-Za-z_-]{1,16}|[0-9]{1,4})$/;
const show = value => value === null || value === undefined || value === '' ?
  '미확정' : html(String(value));
const label = value => SAFE_MASK.test(value || '') ? html(value) : '등록 계좌';

export function formatLegacyHistory(kind, result) {
  if (!KIND_LABEL.has(kind) || !result || !Array.isArray(result.rows)) {
    throw new TypeError('invalid legacy history');
  }
  const lines = [`📚 <b>과거 ${KIND_LABEL.get(kind)} 기록</b> · 읽기 전용`];
  if (!result.archived) lines.push('이전된 과거 기록이 없습니다.');
  else if (result.supported === false) lines.push('이전 원본에 해당 기록 범주가 없습니다.');
  else if (!result.rows.length) lines.push('이 페이지에 표시할 과거 기록이 없습니다.');
  for (const row of result.rows) {
    if (kind === 'accounts') lines.push(`• ${show(row.broker)} · ${label(row.maskedLabel)} ` +
      `(${show(row.baseCurrency)})`);
    if (kind === 'ledger') lines.push(`• ${show(row.broker)} ${label(row.accountLabel)} ` +
      `${show(row.tradedAt)} · ${show(row.transactionType)} ` +
      `${show(row.instrumentMarket)}:${show(row.instrumentTicker)} · ` +
      `${show(row.quantity)}주 · 거래금액 ${show(row.grossAmount)} ${show(row.currency)} ` +
      `· 상태 ${show(row.status)}`);
    if (kind === 'reports') {
      const delivery = row.deliveries.length ? row.deliveries
        .map(item => show(item.status)).join(', ') : '발송 기록 없음';
      lines.push(`• ${show(row.report_type)} ${show(row.period_key)} · ` +
        `상태 ${show(row.status)} · 완료 ${show(row.completed_at)} · 발송 ${delivery}`);
    }
    if (kind === 'occurrences') lines.push(`• ${show(row.local_due_date)} · ` +
      `계획 ID ${show(row.plan_id)} · 예정 ${show(row.planned_amount)} ` +
      `${show(row.currency)} · 실제 ${show(row.actual_amount)} · 상태 ${show(row.status)}`);
    if (kind === 'snapshots') lines.push(`• ${show(row.data_as_of)} · ` +
      `상태 ${show(row.status)} · 평가액 ${row.status === 'COMPLETE' ?
        show(row.total_market_value) : '미확정'} ${show(row.base_currency)}`);
  }
  lines.push('과거 기록은 현재 자산·납입 합계에 더하지 않으며 재실행·재발송하지 않습니다.');
  if (result.nextCursor !== null) lines.push(
    `다음: <code>/history ${kind} ${result.nextCursor}</code>`);
  return lines.join('\n');
}
