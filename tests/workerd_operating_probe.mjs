// Local-only wrapper. This file must never be deployed with a real Telegram token.
import { createOperatingWorker } from '../deploy/cloudflare-js/operating-worker.mjs';
import { SyntheticCloudGateway } from '../deploy/cloudflare-js/cloud-gateway.mjs';
import { initSyntheticTables } from '../deploy/cloudflare-js/synthetic-harness.mjs';
import { importAccount } from '../deploy/cloudflare-js/storage.mjs';

const ACCOUNT = `fixture_${'f'.repeat(64)}`;
const provider = { name: 'kiwoom_main',
  getPositions: async () => ({ broker: 'kiwoom_main', status: 'ok', items: [] }),
  getCash: async () => ({ broker: 'kiwoom_main', status: 'ok', items: [] }),
  getAccounts: async () => ({ broker: 'kiwoom_main', status: 'ok', items: [] }) };
const worker = createOperatingWorker({
  gatewayFactory: async env => new SyntheticCloudGateway({ db: env.DB,
    providers: [provider], scopeAccountFingerprint: ACCOUNT,
    externalRecurringPlans: async () => [], now: () => '2026-02-02T01:00:00Z' }),
  fetcher: async () => ({ ok: true,
    json: async () => ({ ok: true, result: { message_id: 1 } }) }),
  now: () => '2026-02-02T01:00:00Z',
});

export default {
  fetch: (request, env) => worker.fetch(request, env),
  async scheduled(event, env) {
    await initSyntheticTables(env.DB);
    const account = await env.DB.prepare(`SELECT account_fingerprint FROM cloud_accounts
      WHERE account_fingerprint = ?`).bind(ACCOUNT).first();
    if (!account) await importAccount(env.DB, { fingerprint: ACCOUNT,
      broker: 'synthetic', baseCurrency: 'KRW' });
    if (env.RUN_REPORT === '1') {
      await worker.scheduled({ scheduledTime: Date.parse('2026-02-02T01:00:00Z') }, env);
    }
  },
};
