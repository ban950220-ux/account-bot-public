// Source-scoped aggregation candidate with synthetic Decimal and alias parity.
import { add, compare, decimal, divide28, exact, multiply, round28, subtract } from './finance.mjs';

const US_MARKETS = new Set(['US', 'USA', 'NASDAQ', 'NAS', 'NYSE', 'AMEX', 'ARCA', 'NYSEARCA']);
const US_GENERIC = new Set(['US', 'USA']);
const USABLE = new Set(['ok', 'partial', 'stale']);
const STATUSES = new Set([...USABLE, 'not_configured', 'auth_failed', 'rate_limited', 'unavailable']);
const ASSET_TYPES = new Set(['EQUITY', 'ETF', 'FUND', 'BOND', 'CASH', 'OTHER']);
const zero = () => decimal('0');
const instrumentKey = item => `${item.market}:${item.ticker}:${item.assetType}:${item.currency}`;

function nonempty(value) {
  return typeof value === 'string' && value.trim().length > 0;
}

function validateInstrument(item) {
  if (item === null || typeof item !== 'object' ||
      !nonempty(item.market) || !nonempty(item.ticker) ||
      !ASSET_TYPES.has(item.assetType) ||
      !['KRW', 'USD'].includes(item.currency) ||
      [item.market, item.ticker, item.assetType].some(value =>
        typeof value !== 'string' || value.trim() !== value || value !== value.toUpperCase())) {
    throw new TypeError('invalid normalized instrument');
  }
}

function resultMap(results) {
  const mapped = new Map();
  for (const result of results) {
    if (!result.broker || result.broker !== result.broker.toLowerCase() ||
        !STATUSES.has(result.status) || !Array.isArray(result.items) ||
        mapped.has(result.broker)) throw new TypeError('invalid or duplicate broker result');
    mapped.set(result.broker, result);
  }
  return mapped;
}

function fxBook(quotes, baseCurrency) {
  const pairs = new Map();
  for (const quote of quotes) {
    const key = `${quote.base}/${quote.quote}`;
    if (quote.base === quote.quote || pairs.has(key) ||
        compare(decimal(quote.rate), zero()) <= 0) {
      throw new TypeError('duplicate or invalid FX quote');
    }
    pairs.set(key, quote);
  }
  return (value, currency) => {
    if (currency === baseCurrency) return [value, false];
    const quote = pairs.get(`${currency}/${baseCurrency}`);
    if (quote) return [multiply(value, decimal(quote.rate)), quote.isStale === true];
    const inverse = pairs.get(`${baseCurrency}/${currency}`);
    if (inverse) return [divide28(value, decimal(inverse.rate)), inverse.isStale === true];
    return [null, false];
  };
}

function effectiveValue(position, field, fallback) {
  if (position[field] !== null && position[field] !== undefined) return decimal(position[field]);
  if (position[fallback] !== null && position[fallback] !== undefined) {
    return multiply(decimal(position[fallback]), decimal(position.quantity));
  }
  return null;
}

export function aggregateCoverage({ baseCurrency = 'KRW', fxQuotes = [],
  positionResults, cashResults = [], expectedBrokers = null }) {
  if (!['KRW', 'USD'].includes(baseCurrency)) throw new TypeError('unsupported base currency');
  const positions = resultMap(positionResults);
  const cash = resultMap(cashResults);
  const convert = fxBook(fxQuotes, baseCurrency);
  const expected = new Set(expectedBrokers === null ? [] : expectedBrokers.map(broker => {
    if (!nonempty(broker)) throw new TypeError('empty expected broker');
    return broker.trim().toLowerCase();
  }));
  for (const broker of [...positions.keys(), ...cash.keys()]) expected.add(broker);
  if (expected.has('')) throw new TypeError('empty expected broker');
  const expectedSorted = [...expected].sort();
  const includesCash = cashResults.length > 0;
  const incomplete = new Set();
  const included = new Set();
  const warnings = [];
  for (const broker of expectedSorted) {
    const result = positions.get(broker);
    if (!result) { incomplete.add(broker); warnings.push(`${broker}:position_result_missing`); }
    else {
      if (result.status !== 'ok') {
        incomplete.add(broker); warnings.push(`${broker}:positions_${result.status}`);
      }
      if (USABLE.has(result.status)) included.add(broker);
    }
  }
  if (includesCash) for (const broker of expectedSorted) {
    const result = cash.get(broker);
    if (!result) { incomplete.add(broker); warnings.push(`${broker}:cash_result_missing`); }
    else {
      if (result.status !== 'ok') {
        incomplete.add(broker); warnings.push(`${broker}:cash_${result.status}`);
      }
      if (USABLE.has(result.status)) included.add(broker);
    }
  }

  const lots = [];
  for (const result of positionResults) {
    if (!USABLE.has(result.status)) continue;
    for (const position of result.items) {
      if (position.broker !== result.broker || !nonempty(position.accountId)) {
        throw new TypeError('invalid position source or account');
      }
      validateInstrument(position.instrument);
      const quantity = decimal(position.quantity);
      if (compare(quantity, zero()) < 0) throw new TypeError('negative position quantity');
      for (const field of ['marketValue', 'costBasis', 'currentPrice', 'averagePrice']) {
        if (position[field] !== null && position[field] !== undefined &&
            compare(decimal(position[field]), zero()) < 0) {
          throw new TypeError(`negative ${field}`);
        }
      }
      if (compare(quantity, zero()) > 0) lots.push(position);
    }
  }
  const specific = new Map();
  for (const lot of lots) {
    const item = lot.instrument;
    if (!US_MARKETS.has(item.market) || US_GENERIC.has(item.market)) continue;
    const key = `${item.ticker}:${item.assetType}:${item.currency}`;
    if (!specific.has(key)) specific.set(key, new Set());
    specific.get(key).add(item.market);
  }
  const grouped = new Map();
  for (const lot of lots) {
    const item = lot.instrument;
    const choices = specific.get(`${item.ticker}:${item.assetType}:${item.currency}`);
    const market = US_GENERIC.has(item.market) && choices?.size === 1 ? [...choices][0] : item.market;
    const instrument = { ...item, market };
    const key = instrumentKey(instrument);
    if (!grouped.has(key)) grouped.set(key, { instrument, lots: [] });
    grouped.get(key).lots.push(lot);
  }

  let positionAmount = zero();
  let costAmount = zero();
  let cashAmount = zero();
  let valuationComplete = true;
  let allCostsComplete = true;
  let staleFx = false;
  const aggregated = [];
  for (const key of [...grouped.keys()].sort()) {
    const { instrument, lots: group } = grouped.get(key);
    const quantity = group.reduce((sum, item) => add(sum, decimal(item.quantity)), zero());
    const marketValues = group.map(item => effectiveValue(item, 'marketValue', 'currentPrice'));
    const costs = group.map(item => effectiveValue(item, 'costBasis', 'averagePrice'));
    const marketValue = marketValues.some(item => item === null) ? null :
      marketValues.reduce(add, zero());
    const cost = costs.some(item => item === null) ? null : costs.reduce(add, zero());
    if (cost === null) allCostsComplete = false;
    const unrealized = marketValue === null || cost === null ? null : subtract(marketValue, cost);
    const returnPct = unrealized === null || compare(cost, zero()) === 0 ? null :
      round28(multiply(divide28(unrealized, cost), decimal('100')));
    let marketValueBase = null;
    if (marketValue === null) {
      valuationComplete = false; warnings.push(`${key}:market_value_missing`);
    } else {
      const [converted, stale] = convert(marketValue, instrument.currency);
      marketValueBase = converted;
      staleFx ||= stale;
      if (converted === null) {
        valuationComplete = false; warnings.push(`${key}:fx_to_${baseCurrency}_missing`);
      } else positionAmount = add(positionAmount, converted);
    }
    let costBasisBase = null;
    if (cost !== null) {
      const [converted, stale] = convert(cost, instrument.currency);
      costBasisBase = converted;
      staleFx ||= stale;
      if (converted === null) allCostsComplete = false;
      else costAmount = add(costAmount, converted);
    }
    const brokerQuantities = new Map();
    for (const lot of group) {
      brokerQuantities.set(lot.broker, add(brokerQuantities.get(lot.broker) || zero(),
        decimal(lot.quantity)));
    }
    aggregated.push({
      instrument, quantity: exact(quantity),
      companyName: group.find(item => item.companyName)?.companyName || '',
      averagePrice: cost === null ? null : exact(divide28(cost, quantity)),
      currentPrice: marketValue === null ? null : exact(divide28(marketValue, quantity)),
      costBasis: cost === null ? null : exact(cost),
      marketValue: marketValue === null ? null : exact(marketValue),
      unrealizedProfit: unrealized === null ? null : exact(unrealized),
      returnPct: returnPct === null ? null : exact(returnPct),
      costBasisBase: costBasisBase === null ? null : exact(costBasisBase),
      marketValueBase: marketValueBase === null ? null : exact(marketValueBase),
      weightPct: null,
      brokerQuantities: [...brokerQuantities].sort(([a], [b]) => a.localeCompare(b))
        .map(([broker, value]) => [broker, exact(value)]),
    });
  }
  const convertedCash = [];
  if (includesCash) for (const result of cashResults) {
    if (!USABLE.has(result.status)) continue;
    for (const balance of result.items) {
      if (balance.broker !== result.broker || !nonempty(balance.accountId) ||
          !['KRW', 'USD'].includes(balance.currency)) {
        throw new TypeError('invalid cash source, account or currency');
      }
      const [converted, stale] = convert(decimal(balance.amount), balance.currency);
      staleFx ||= stale;
      if (converted === null) {
        valuationComplete = false;
        warnings.push(`${balance.broker}:cash_fx_${balance.currency}_to_${baseCurrency}_missing`);
      } else cashAmount = add(cashAmount, converted);
      convertedCash.push({ broker: balance.broker, accountId: balance.accountId,
        currency: balance.currency, amount: balance.amount,
        amountBase: converted === null ? null : exact(converted) });
    }
  }
  if (staleFx) warnings.push('fx_quote_stale');
  const complete = expectedSorted.length > 0 && incomplete.size === 0 && valuationComplete;
  const subtotal = add(positionAmount, cashAmount);
  let cashWeightPct = null;
  if (complete && compare(subtotal, zero()) > 0) {
    for (const position of aggregated) {
      if (position.marketValueBase !== null) {
        position.weightPct = exact(round28(multiply(
          divide28(decimal(position.marketValueBase), subtotal), decimal('100'))));
      }
    }
    cashWeightPct = exact(round28(multiply(divide28(cashAmount, subtotal), decimal('100'))));
  }
  return {
    baseCurrency,
    expectedBrokers: expectedSorted, includedBrokers: [...included].sort(),
    incompleteBrokers: [...incomplete].sort(), includesCash, isComplete: complete,
    includedPositionValue: exact(positionAmount), includedCashValue: exact(cashAmount),
    includedMarketValue: exact(subtotal), totalMarketValue: complete ? exact(subtotal) : null,
    includedCostBasis: allCostsComplete ? exact(costAmount) : null,
    totalCostBasis: complete && allCostsComplete ? exact(costAmount) : null,
    cashWeightPct, positions: aggregated, cashBalances: convertedCash,
    warnings: [...new Set(warnings)],
  };
}
