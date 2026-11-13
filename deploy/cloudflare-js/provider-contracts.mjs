// Injected read-only provider contracts. Tests use mocks; no HTTP is made here.
import { aggregateCoverage } from './aggregation.mjs';
import { compare, decimal } from './finance.mjs';

const STATUSES = new Set(['ok', 'partial', 'stale', 'not_configured',
  'auth_failed', 'rate_limited', 'unavailable']);

function result(source, value) {
  if (!value || value.broker !== source || !STATUSES.has(value.status) ||
      !Array.isArray(value.items) ||
      (value.status === 'partial' && !value.safeErrorType && !value.missingAccounts?.length)) {
    throw new TypeError('invalid normalized provider result');
  }
  return value;
}

async function safeRead(source, operation) {
  try { return result(source, await operation()); }
  catch (error) {
    return { broker: source, status: 'unavailable', items: [],
      safeErrorType: error instanceof TypeError ? 'InvalidProviderResult' :
        'ProviderUnavailable' };
  }
}

export async function refreshWithProviders(providers, selectedNames, fxQuotes = []) {
  if (!Array.isArray(selectedNames) || !selectedNames.length ||
      new Set(selectedNames).size !== selectedNames.length) {
    throw new TypeError('explicit unique source scope required');
  }
  const byName = new Map(providers.map(provider => [provider.name, provider]));
  const rows = await Promise.all(selectedNames.map(async name => {
    const provider = byName.get(name);
    if (!provider || !['getPositions', 'getCash', 'getAccounts'].every(
      method => typeof provider[method] === 'function')) {
      const missing = { broker: name, status: 'not_configured', items: [] };
      return { positions: missing, cash: missing, accounts: missing };
    }
    const [positions, cash, accounts] = await Promise.all([
      safeRead(name, () => provider.getPositions()),
      safeRead(name, () => provider.getCash()),
      safeRead(name, () => provider.getAccounts()),
    ]);
    return { positions, cash, accounts };
  }));
  const positionResults = rows.map(row => row.positions);
  const cashResults = rows.map(row => row.cash);
  return {
    snapshot: aggregateCoverage({ baseCurrency: 'KRW', expectedBrokers: selectedNames,
      fxQuotes, positionResults, cashResults }),
    positionResults, cashResults, accountResults: rows.map(row => row.accounts),
    fxQuote: fxQuotes.find(quote => quote.base === 'USD' && quote.quote === 'KRW') || null,
  };
}

export async function readPrice(provider, market, ticker) {
  if (!provider || typeof provider.getPrice !== 'function') return null;
  let quote;
  try { quote = await provider.getPrice(market, ticker); }
  catch { return null; }
  try {
    if (!quote || quote.market !== market || quote.ticker !== ticker ||
        !['KRW', 'USD'].includes(quote.currency) || typeof quote.asOf !== 'string' ||
        typeof quote.provider !== 'string' || !quote.provider ||
        typeof quote.stale !== 'boolean' ||
        compare(decimal(quote.price), decimal('0')) <= 0) return null;
  } catch { return null; }
  return quote;
}
