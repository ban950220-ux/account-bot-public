// Offline Worker candidate. Money enters and leaves as decimal strings.
// Number arithmetic must never be used for an account amount or FX rate.
const DECIMAL = /^([+-]?)(\d+)(?:\.(\d+))?$/;

export function decimal(value) {
  if (typeof value !== 'string' || value.length > 100) throw new TypeError('decimal string required');
  const match = DECIMAL.exec(value);
  if (!match) throw new TypeError('invalid decimal');
  const scale = (match[3] || '').length;
  const digits = BigInt(match[2] + (match[3] || ''));
  return { coefficient: match[1] === '-' ? -digits : digits, scale };
}

function align(left, right) {
  const scale = Math.max(left.scale, right.scale);
  return [left.coefficient * 10n ** BigInt(scale - left.scale),
    right.coefficient * 10n ** BigInt(scale - right.scale), scale];
}

export function add(left, right) {
  const [a, b, scale] = align(left, right);
  return { coefficient: a + b, scale };
}

export function subtract(left, right) {
  const [a, b, scale] = align(left, right);
  return { coefficient: a - b, scale };
}

export function multiply(left, right) {
  return { coefficient: left.coefficient * right.coefficient,
    scale: left.scale + right.scale };
}

export function round28(value) {
  const absolute = value.coefficient < 0n ? -value.coefficient : value.coefficient;
  const places = absolute.toString().length - 28;
  if (places <= 0) return value;
  const divisor = 10n ** BigInt(places);
  let quotient = absolute / divisor;
  const remainder = absolute % divisor;
  if (remainder * 2n > divisor || (remainder * 2n === divisor && quotient % 2n === 1n)) {
    quotient += 1n;
  }
  return { coefficient: (value.coefficient < 0n ? -quotient : quotient) * divisor,
    scale: value.scale };
}

export function divide28(left, right) {
  if (right.coefficient === 0n) throw new RangeError('divide by zero');
  if (left.coefficient === 0n) return decimal('0');
  const positiveLeft = left.coefficient < 0n ? -left.coefficient : left.coefficient;
  const positiveRight = right.coefficient < 0n ? -right.coefficient : right.coefficient;
  const numerator = positiveLeft * 10n ** BigInt(right.scale);
  const denominator = positiveRight * 10n ** BigInt(left.scale);
  let exponent = numerator.toString().length - denominator.toString().length;
  const belowPower = power => power >= 0 ?
    numerator < denominator * 10n ** BigInt(power) :
    numerator * 10n ** BigInt(-power) < denominator;
  if (belowPower(exponent)) exponent -= 1;
  const targetScale = 27 - exponent;
  const scaledNumerator = targetScale >= 0 ?
    numerator * 10n ** BigInt(targetScale) : numerator;
  const scaledDenominator = targetScale >= 0 ?
    denominator : denominator * 10n ** BigInt(-targetScale);
  let quotient = scaledNumerator / scaledDenominator;
  const remainder = scaledNumerator % scaledDenominator;
  if (remainder * 2n > scaledDenominator ||
      (remainder * 2n === scaledDenominator && quotient % 2n === 1n)) quotient += 1n;
  if ((left.coefficient < 0n) !== (right.coefficient < 0n)) quotient = -quotient;
  return targetScale >= 0 ? { coefficient: quotient, scale: targetScale } :
    { coefficient: quotient * 10n ** BigInt(-targetScale), scale: 0 };
}

export function compare(left, right) {
  const [a, b] = align(left, right);
  return a < b ? -1 : a > b ? 1 : 0;
}

export function exact(value) {
  const negative = value.coefficient < 0n ? '-' : '';
  const absolute = value.coefficient < 0n ? -value.coefficient : value.coefficient;
  const digits = absolute.toString().padStart(value.scale + 1, '0');
  return negative + (value.scale ?
    `${digits.slice(0, -value.scale)}.${digits.slice(-value.scale)}` : digits);
}

export function display(value, places) {
  if (!Number.isInteger(places) || places < 0 || places > 20) throw new RangeError('places');
  const shift = value.scale - places;
  let rounded = value.coefficient;
  if (shift > 0) {
    const divisor = 10n ** BigInt(shift);
    const negative = rounded < 0n;
    const absolute = negative ? -rounded : rounded;
    let quotient = absolute / divisor;
    if ((absolute % divisor) * 2n >= divisor) quotient += 1n; // Decimal ROUND_HALF_UP
    rounded = negative ? -quotient : quotient;
  } else if (shift < 0) {
    rounded *= 10n ** BigInt(-shift);
  }
  const negative = rounded < 0n ? '-' : '';
  const digits = (rounded < 0n ? -rounded : rounded).toString().padStart(places + 1, '0');
  const whole = digits.slice(0, digits.length - places).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  return negative + whole + (places ? `.${digits.slice(-places)}` : '');
}

export function weeklyBudget(plans, fxRate = null) {
  let total = decimal('0');
  if (fxRate !== null && compare(decimal(fxRate), total) <= 0) {
    throw new TypeError('invalid FX rate');
  }
  for (const plan of plans) {
    if (typeof plan.enabled !== 'boolean') throw new TypeError('plan enabled state required');
    if (!plan.enabled) continue;
    if (!Array.isArray(plan.scheduledWeekdays) ||
        plan.scheduledWeekdays.some(day => !Number.isInteger(day) || day < 0 || day > 6)) {
      throw new TypeError('invalid scheduled weekdays');
    }
    const amount = multiply(decimal(plan.dailyAmount), decimal(String(new Set(plan.scheduledWeekdays).size)));
    if (compare(decimal(plan.dailyAmount), decimal('0')) <= 0) {
      throw new TypeError('invalid daily amount');
    }
    if (plan.currency === 'KRW') total = add(total, amount);
    else if (plan.currency === 'USD') {
      if (fxRate === null) return null;
      total = add(total, multiply(amount, decimal(fxRate)));
    } else throw new TypeError('unsupported currency');
  }
  return total;
}

const US_MARKETS = new Set(['US', 'USA', 'NASDAQ', 'NAS', 'NYSE', 'AMEX', 'ARCA', 'NYSEARCA']);
function compatible(left, right) {
  if (left.ticker !== right.ticker || left.assetType !== right.assetType ||
      left.currency !== right.currency) return false;
  if (left.market === right.market) return true;
  return ((left.market === 'US' || left.market === 'USA') && US_MARKETS.has(right.market)) ||
    ((right.market === 'US' || right.market === 'USA') && US_MARKETS.has(left.market));
}

export function evaluateRecurringBuy(plan, transactions, options) {
  if (typeof plan.enabled !== 'boolean' || typeof options.dataComplete !== 'boolean' ||
      typeof options.isTradingDay !== 'boolean' || typeof options.cutoffPassed !== 'boolean' ||
      !/^\d{4}-\d{2}-\d{2}$/.test(options.targetDate) ||
      new Date(`${options.targetDate}T00:00:00Z`).toISOString().slice(0, 10) !== options.targetDate) {
    throw new TypeError('missing or invalid reconciliation state');
  }
  const expected = decimal(plan.dailyAmount);
  const zero = decimal('0');
  if (compare(expected, zero) <= 0 || !Array.isArray(plan.scheduledWeekdays) ||
      !['KRW', 'USD'].includes(plan.currency)) throw new TypeError('invalid plan');
  const target = options.targetDate;
  const weekday = (new Date(`${target}T00:00:00Z`).getUTCDay() + 6) % 7;
  const finish = (status, actual, ids, final) => ({
    status, actual, remaining: compare(expected, actual) > 0 ? subtract(expected, actual) : zero,
    matchedTransactionIds: ids, isFinal: final,
  });
  if (!plan.enabled) return finish('disabled', zero, [], true);
  if ((plan.startedAt && target < plan.startedAt) ||
      !plan.scheduledWeekdays.includes(weekday) || !options.isTradingDay) {
    return finish('not_due', zero, [], true);
  }
  let actual = zero;
  const ids = [];
  for (const row of transactions) {
    if (row.side !== 'BUY' || row.tradeDate !== target ||
        !compatible(row.instrument, plan.instrument) ||
        (plan.broker != null && row.broker !== plan.broker) ||
        (plan.accountId != null && row.accountId !== plan.accountId)) continue;
    if (row.currency !== plan.currency) throw new TypeError('transaction currency mismatch');
    const gross = decimal(row.grossAmount);
    if (compare(gross, zero) < 0) throw new TypeError('negative transaction');
    actual = add(actual, gross);
    ids.push(row.transactionId);
  }
  ids.sort();
  if (!options.dataComplete) return finish('unverifiable', actual, ids, false);
  const tolerancePct = decimal(options.tolerancePct ?? '1');
  const absolute = decimal(options.absoluteTolerance ?? '0');
  if (compare(tolerancePct, zero) < 0 || compare(absolute, zero) < 0) {
    throw new TypeError('negative tolerance');
  }
  const tolerance = multiply(expected, tolerancePct);
  const percentTolerance = { coefficient: tolerance.coefficient, scale: tolerance.scale + 2 };
  const effectiveTolerance = compare(percentTolerance, absolute) > 0 ? percentTolerance : absolute;
  if (compare(actual, subtract(expected, effectiveTolerance)) >= 0) {
    return finish('filled', actual, ids, true);
  }
  if (compare(actual, zero) > 0) return finish('partially_filled', actual, ids, options.cutoffPassed);
  return finish(options.cutoffPassed ? 'missing' : 'pending', zero, [], options.cutoffPassed);
}
