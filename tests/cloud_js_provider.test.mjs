import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readPrice, refreshWithProviders } from '../deploy/cloudflare-js/provider-contracts.mjs';

const good = { name: 'kiwoom_main',
  getPositions: async () => ({ broker: 'kiwoom_main', status: 'ok', items: [{
    broker: 'kiwoom_main', accountId: 'synthetic', quantity: '1',
    instrument: { market: 'KRX', ticker: 'TEST', assetType: 'ETF', currency: 'KRW' },
    marketValue: '100', costBasis: '90',
  }] }),
  getCash: async () => ({ broker: 'kiwoom_main', status: 'ok', items: [] }),
  getAccounts: async () => ({ broker: 'kiwoom_main', status: 'ok', items: [] }),
};

test('explicit source selection cannot add a secondary source to main assets', async () => {
  const secondary = { ...good, name: 'meritz',
    getPositions: async () => ({ broker: 'meritz', status: 'ok', items: [] }),
    getCash: async () => ({ broker: 'meritz', status: 'ok', items: [] }),
    getAccounts: async () => ({ broker: 'meritz', status: 'ok', items: [] }) };
  const main = await refreshWithProviders([good, secondary], ['kiwoom_main']);
  assert.equal(main.snapshot.totalMarketValue, '100');
  assert.deepEqual(main.snapshot.expectedBrokers, ['kiwoom_main']);
  const all = await refreshWithProviders([good, secondary], ['kiwoom_main', 'meritz']);
  assert.deepEqual(all.snapshot.expectedBrokers, ['kiwoom_main', 'meritz']);
});

test('partial and thrown provider results are incomplete, not zero-valued', async () => {
  const partial = { ...good,
    getPositions: async () => ({ broker: 'kiwoom_main', status: 'partial',
      safeErrorType: 'SyntheticPageMissing', items: [{
        broker: 'kiwoom_main', accountId: 'synthetic', quantity: '1',
        instrument: { market: 'KRX', ticker: 'TEST', assetType: 'ETF', currency: 'KRW' },
        marketValue: '100', costBasis: '90',
      }] }),
    getCash: async () => { throw new Error('synthetic failure'); },
  };
  const result = await refreshWithProviders([partial], ['kiwoom_main']);
  assert.equal(result.snapshot.isComplete, false);
  assert.equal(result.snapshot.totalMarketValue, null);
  assert.equal(result.snapshot.includedMarketValue, '100');
  assert.equal(result.cashResults[0].status, 'unavailable');
  assert.equal(result.snapshot.positions[0].weightPct, null);
});

test('market price contract withholds missing or invalid quotes', async () => {
  const quote = { market: 'US', ticker: 'TEST', price: '10.25', currency: 'USD',
    asOf: '2026-02-02T00:00:00Z', provider: 'fixture', stale: false };
  assert.deepEqual(await readPrice({ getPrice: async () => quote }, 'US', 'TEST'), quote);
  assert.equal(await readPrice({ getPrice: async () => ({ ...quote, price: null }) },
    'US', 'TEST'), null);
  assert.equal(await readPrice({ getPrice: async () => { throw new Error('offline'); } },
    'US', 'TEST'), null);
});
