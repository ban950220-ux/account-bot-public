// Production-boundary candidate. External read-only providers are injected; no default credentials.
import { admittedUpdateId, dueJobs } from './boundary.mjs';
import { routeUpdate } from './command-router.mjs';
import { buildPeriodicReport } from './periodic-report.mjs';
import { beginSend, enqueueReportChunks, enqueueUpdateChunks, finishSend } from './outbox.mjs';
import { sendTelegramOnce, splitTelegramMessage } from './telegram-transport.mjs';
import { createManualUploadHandler } from './manual-input.mjs';
import { createOperatingGateway } from './operating-gateway.mjs';
import { requireActiveCutover, legacyReportSuppressed } from './cutover-state.mjs';
import { createD1QueryBudget } from './d1-budget.mjs';
import { createExternalRequestBudget } from './external-budget.mjs';

const MAX_BODY = 64 * 1024;
function positiveId(value) {
  const number = Number(value);
  return Number.isSafeInteger(number) && number > 0 ? number : null;
}

export function operatingConfig(env) {
  const users = typeof env?.TELEGRAM_ALLOWED_USER_IDS === 'string' ?
    env.TELEGRAM_ALLOWED_USER_IDS.split(',').map(value => positiveId(value.trim())) : [];
  const chat = positiveId(env?.TELEGRAM_CHAT_ID);
  const weekday = Number(env?.WEEKLY_REPORT_WEEKDAY);
  let timezoneValid = false;
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: env?.REPORT_TIMEZONE });
    timezoneValid = true;
  } catch { /* invalid timezone is not accepted */ }
  const clock = value => typeof value === 'string' &&
    /^(?:[01]\d|2[0-3]):[0-5]\d(?::00)?$/.test(value);
  if (!env?.DB || !/^\d+:[A-Za-z0-9_-]{20,}$/.test(env.TELEGRAM_BOT_TOKEN || '') ||
      !/^[A-Za-z0-9_-]{1,256}$/.test(env.TELEGRAM_WEBHOOK_SECRET || '') ||
      !users.length || users.some(value => value === null) ||
      new Set(users).size !== users.length || chat === null || !users.includes(chat) ||
      typeof env.REPORT_TIMEZONE !== 'string' || !env.REPORT_TIMEZONE || !timezoneValid ||
      !clock(env.DAILY_REPORT_TIME) || !clock(env.WEEKLY_REPORT_TIME) ||
      !/^\d$/.test(env.WEEKLY_REPORT_WEEKDAY || '') ||
      !Number.isInteger(weekday) || weekday < 0 || weekday > 6) {
    throw new TypeError('operating Worker configuration unavailable');
  }
  return { db: env.DB, token: env.TELEGRAM_BOT_TOKEN,
    secret: env.TELEGRAM_WEBHOOK_SECRET, users, chat: String(chat),
    timezone: env.REPORT_TIMEZONE, dailyTime: env.DAILY_REPORT_TIME,
    weeklyTime: env.WEEKLY_REPORT_TIME, weeklyWeekday: weekday };
}

async function boundedBody(request) {
  if (Number(request.headers.get('content-length')) > MAX_BODY || !request.body) return null;
  const reader = request.body.getReader();
  const chunks = [];
  let length = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    length += value.byteLength;
    if (length > MAX_BODY) { await reader.cancel(); return null; }
    chunks.push(value);
  }
  const body = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength; }
  return body;
}

async function outboxRows(db, prefix) {
  const { results } = await db.prepare(`SELECT event_key, destination_key,
    payload_json, status FROM cloud_outbox WHERE event_key LIKE ?`)
    .bind(`${prefix}%`).all();
  return results.filter(row => row.event_key.startsWith(prefix) &&
    /^\d+$/.test(row.event_key.slice(prefix.length)))
    .sort((a, b) => Number(a.event_key.slice(prefix.length)) -
      Number(b.event_key.slice(prefix.length)));
}

async function deliverRows(db, prefix, config, destination, fetcher, now,
  { d1Budget = null, externalBudget = null } = {}) {
  const rows = await outboxRows(db, prefix);
  // Another invocation may have won the claim with a different chunk count.
  // This read is already charged; reserve writes against the stored rows.
  const pending = rows.filter(row => row.status === 'PENDING').length;
  d1Budget?.assertAvailable(2 * pending);
  externalBudget?.assertAvailable(pending);
  for (const row of rows) {
    if (row.destination_key !== destination) throw new Error('delivery account mismatch');
    if (row.status === 'SENT') continue;
    if (row.status === 'UNKNOWN' || row.status === 'SENDING') return 'UNKNOWN';
    if (row.status !== 'PENDING') throw new Error('unexpected delivery status');
    if (!await beginSend(db, row.event_key)) return 'UNKNOWN';
    let sent = false;
    try {
      const payload = JSON.parse(row.payload_json);
      sent = await sendTelegramOnce(fetcher, config.token, destination, payload.text);
    } catch { /* SENDING remains uncertain on a local failure. */ }
    try { await finishSend(db, row.event_key, sent, now()); }
    catch { return 'UNKNOWN'; }
    if (!sent) return 'UNKNOWN';
  }
  return 'DONE';
}

function localDay(iso, timezone) {
  const fields = Object.fromEntries(new Intl.DateTimeFormat('en-US', {
    timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(new Date(iso)).map(part => [part.type, part.value]));
  return `${fields.year}${fields.month}${fields.day}`;
}

export function createOperatingWorker({ gatewayFactory, fetcher = fetch,
  providerFetcher = fetch,
  now = () => new Date().toISOString(), authorizeManualOwner = null, requireCutover = false }) {
  if (typeof gatewayFactory !== 'function' || typeof fetcher !== 'function' ||
      typeof providerFetcher !== 'function') {
    throw new TypeError('read-only provider factory and transport required');
  }
  return {
    async fetch(request, env) {
      if (new URL(request.url).pathname === '/owner/manual-input') {
        if (typeof authorizeManualOwner !== 'function' || !env?.DB) {
          return new Response('not found', { status: 404 });
        }
        return createManualUploadHandler({ authorizeOwner: req =>
          authorizeManualOwner(req, env), db: env.DB, now })(request);
      }
      if (request.method !== 'POST' || new URL(request.url).pathname !== '/telegram/webhook') {
        return new Response('not found', { status: 404 });
      }
      let config;
      try { config = operatingConfig(env); if (requireCutover) await requireActiveCutover(env, config); } catch { return new Response('unavailable', { status: 503 }); }
      const body = await boundedBody(request);
      if (!body) return new Response('invalid update', { status: 413 });
      const headerEntries = [...request.headers.entries()];
      const id = admittedUpdateId(headerEntries, body, config.secret, config.users);
      if (id === null) return new Response('forbidden', { status: 403 });
      const update = JSON.parse(new TextDecoder().decode(body));
      const chatId = update.message?.chat?.id ?? update.callback_query?.message?.chat?.id;
      const destination = String(chatId);
      const prefix = `update:${id}:`;
      try {
        const existing = await outboxRows(config.db, prefix);
        if (existing.length) {
          await deliverRows(config.db, prefix, config, destination, fetcher, now);
          return new Response('accepted');
        }
        const claim = await config.db.prepare(`SELECT update_id FROM cloud_update_claims
          WHERE update_id = ?`).bind(id).first();
        if (claim) return new Response('unavailable', { status: 503 });
        const gateway = await gatewayFactory(env, config);
        const result = await routeUpdate({ headerEntries, body, secret: config.secret,
          allowedUserIds: config.users, gateway, atomicDelivery: true, now });
        if (result.status === 'IGNORED') return new Response('ignored');
        if (result.status !== 'READY') return new Response('unavailable', { status: 503 });
        if (!result.mutationAttempted) {
          await enqueueUpdateChunks(config.db, id, destination,
            splitTelegramMessage(result.reply), now());
        }
        if (!(await outboxRows(config.db, prefix)).length) {
          return new Response('unavailable', { status: 503 });
        }
        await deliverRows(config.db, prefix, config, destination, fetcher, now);
        return new Response('accepted');
      } catch { return new Response('unavailable', { status: 503 }); }
    },
    async scheduled(event, env) {
      const budget = createD1QueryBudget(env?.DB);
      const externalBudget = createExternalRequestBudget();
      const readFetcher = externalBudget.wrap(providerFetcher);
      const sendFetcher = externalBudget.wrap(fetcher);
      env = { ...env, DB: budget.db };
      const config = operatingConfig(env);
      const scheduledAt = new Date(event.scheduledTime).toISOString();
      if (requireCutover) await requireActiveCutover(env, config, { scheduledAt });
      const jobs = dueJobs(scheduledAt, config.timezone,
        config.dailyTime, config.weeklyTime, config.weeklyWeekday);
      if (!jobs.length) return;
      const reportDay = localDay(scheduledAt, config.timezone);
      // A delayed event must not replay an earlier local day's missed report
      // or relabel it as today's period. Same-day dispatch delays retain the
      // controller's scheduled minute and the existing atomic period claim.
      if (reportDay !== localDay(now(), config.timezone)) return;
      const gateway = await gatewayFactory(env, config, { fetcher: readFetcher, now });
      for (const job of jobs) {
        if (job === 'weekly_valuation') {
          if (typeof gateway.refreshValuations !== 'function') {
            throw new Error('valuation refresh adapter unavailable');
          }
          await gateway.refreshValuations();
          continue;
        }
        const key = `${job}:${reportDay}`;
        if (await legacyReportSuppressed(config.db,key)) continue;
        const prefix = `report:${key}:`;
        const existing = await outboxRows(config.db, prefix);
        if (!existing.length) {
          const claim = await config.db.prepare(`SELECT period_key FROM cloud_report_claims
            WHERE period_key = ?`).bind(key).first();
          if (claim) throw new Error('report claim without outbox');
          const [refresh, plans] = await Promise.all([
            gateway.refresh({ force: true, allBrokers: false }), gateway.listPlans(true),
          ]);
          const analyses = gateway.analysisProvider && plans.length ?
            await gateway.analyzePlans(plans, refresh.snapshot) : {};
          const report = buildPeriodicReport(job, refresh.snapshot, refresh, plans, analyses);
          const chunks = splitTelegramMessage(report);
          // Reserve enqueue batch, outbox read, and begin/finish for every chunk
          // before claiming the period or making the first external send.
          budget.assertAvailable(2 + 2 * chunks.length);
          externalBudget.assertAvailable(chunks.length);
          await enqueueReportChunks(config.db, key, config.chat, chunks, now());
        }
        await deliverRows(config.db, prefix, config, config.chat, sendFetcher, now,
          { d1Budget: budget, externalBudget });
      }
    },
  };
}

// Market/FX and owner-upload authentication are still unbound; those paths fail closed.
export default createOperatingWorker({ gatewayFactory: (env, _config, options) =>
  createOperatingGateway(env, options), requireCutover: true });
