// deploy/cloudflare-js/finance.mjs
var DECIMAL = /^([+-]?)(\d+)(?:\.(\d+))?$/;
function decimal(value) {
  if (typeof value !== "string" || value.length > 100) throw new TypeError("decimal string required");
  const match2 = DECIMAL.exec(value);
  if (!match2) throw new TypeError("invalid decimal");
  const scale = (match2[3] || "").length;
  const digits = BigInt(match2[2] + (match2[3] || ""));
  return { coefficient: match2[1] === "-" ? -digits : digits, scale };
}
function align(left, right) {
  const scale = Math.max(left.scale, right.scale);
  return [
    left.coefficient * 10n ** BigInt(scale - left.scale),
    right.coefficient * 10n ** BigInt(scale - right.scale),
    scale
  ];
}
function add(left, right) {
  const [a, b, scale] = align(left, right);
  return { coefficient: a + b, scale };
}
function subtract(left, right) {
  const [a, b, scale] = align(left, right);
  return { coefficient: a - b, scale };
}
function multiply(left, right) {
  return {
    coefficient: left.coefficient * right.coefficient,
    scale: left.scale + right.scale
  };
}
function round28(value) {
  const absolute = value.coefficient < 0n ? -value.coefficient : value.coefficient;
  const places = absolute.toString().length - 28;
  if (places <= 0) return value;
  const divisor = 10n ** BigInt(places);
  let quotient = absolute / divisor;
  const remainder = absolute % divisor;
  if (remainder * 2n > divisor || remainder * 2n === divisor && quotient % 2n === 1n) {
    quotient += 1n;
  }
  return {
    coefficient: (value.coefficient < 0n ? -quotient : quotient) * divisor,
    scale: value.scale
  };
}
function divide28(left, right) {
  if (right.coefficient === 0n) throw new RangeError("divide by zero");
  if (left.coefficient === 0n) return decimal("0");
  const positiveLeft = left.coefficient < 0n ? -left.coefficient : left.coefficient;
  const positiveRight = right.coefficient < 0n ? -right.coefficient : right.coefficient;
  const numerator = positiveLeft * 10n ** BigInt(right.scale);
  const denominator = positiveRight * 10n ** BigInt(left.scale);
  let exponent = numerator.toString().length - denominator.toString().length;
  const belowPower = (power) => power >= 0 ? numerator < denominator * 10n ** BigInt(power) : numerator * 10n ** BigInt(-power) < denominator;
  if (belowPower(exponent)) exponent -= 1;
  const targetScale = 27 - exponent;
  const scaledNumerator = targetScale >= 0 ? numerator * 10n ** BigInt(targetScale) : numerator;
  const scaledDenominator = targetScale >= 0 ? denominator : denominator * 10n ** BigInt(-targetScale);
  let quotient = scaledNumerator / scaledDenominator;
  const remainder = scaledNumerator % scaledDenominator;
  if (remainder * 2n > scaledDenominator || remainder * 2n === scaledDenominator && quotient % 2n === 1n) quotient += 1n;
  if (left.coefficient < 0n !== right.coefficient < 0n) quotient = -quotient;
  return targetScale >= 0 ? { coefficient: quotient, scale: targetScale } : { coefficient: quotient * 10n ** BigInt(-targetScale), scale: 0 };
}
function compare(left, right) {
  const [a, b] = align(left, right);
  return a < b ? -1 : a > b ? 1 : 0;
}
function exact(value) {
  const negative = value.coefficient < 0n ? "-" : "";
  const absolute = value.coefficient < 0n ? -value.coefficient : value.coefficient;
  const digits = absolute.toString().padStart(value.scale + 1, "0");
  return negative + (value.scale ? `${digits.slice(0, -value.scale)}.${digits.slice(-value.scale)}` : digits);
}
function display(value, places) {
  if (!Number.isInteger(places) || places < 0 || places > 20) throw new RangeError("places");
  const shift = value.scale - places;
  let rounded2 = value.coefficient;
  if (shift > 0) {
    const divisor = 10n ** BigInt(shift);
    const negative2 = rounded2 < 0n;
    const absolute = negative2 ? -rounded2 : rounded2;
    let quotient = absolute / divisor;
    if (absolute % divisor * 2n >= divisor) quotient += 1n;
    rounded2 = negative2 ? -quotient : quotient;
  } else if (shift < 0) {
    rounded2 *= 10n ** BigInt(-shift);
  }
  const negative = rounded2 < 0n ? "-" : "";
  const digits = (rounded2 < 0n ? -rounded2 : rounded2).toString().padStart(places + 1, "0");
  const whole = digits.slice(0, digits.length - places).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return negative + whole + (places ? `.${digits.slice(-places)}` : "");
}
function weeklyBudget(plans, fxRate = null) {
  let total = decimal("0");
  if (fxRate !== null && compare(decimal(fxRate), total) <= 0) {
    throw new TypeError("invalid FX rate");
  }
  for (const plan of plans) {
    if (typeof plan.enabled !== "boolean") throw new TypeError("plan enabled state required");
    if (!plan.enabled) continue;
    if (!Array.isArray(plan.scheduledWeekdays) || plan.scheduledWeekdays.some((day) => !Number.isInteger(day) || day < 0 || day > 6)) {
      throw new TypeError("invalid scheduled weekdays");
    }
    const amount2 = multiply(decimal(plan.dailyAmount), decimal(String(new Set(plan.scheduledWeekdays).size)));
    if (compare(decimal(plan.dailyAmount), decimal("0")) <= 0) {
      throw new TypeError("invalid daily amount");
    }
    if (plan.currency === "KRW") total = add(total, amount2);
    else if (plan.currency === "USD") {
      if (fxRate === null) return null;
      total = add(total, multiply(amount2, decimal(fxRate)));
    } else throw new TypeError("unsupported currency");
  }
  return total;
}

// deploy/cloudflare-js/storage.mjs
var FINGERPRINT = /^[a-z][a-z0-9_]{0,30}_[0-9a-f]{64}$/;
var IDENTIFIER = /^[A-Z0-9][A-Z0-9._:-]{0,159}$/;
var CURRENCY = /^[A-Z]{3}$/;
var MASKED_LABEL = /^\*{3,}(?:[A-Za-z_-]{1,16}|[0-9]{1,4})$/;
function required(value, field) {
  if (typeof value !== "string" || !value.trim()) throw new TypeError(`${field} required`);
  return value;
}
function fingerprint(value) {
  if (!FINGERPRINT.test(value)) throw new TypeError("opaque account fingerprint required");
  return value;
}
function amount(value, field, minimum = null) {
  if (value === null || value === void 0) return null;
  const parsed = decimal(value);
  if (minimum !== null && compare(parsed, decimal(minimum)) < 0) {
    throw new TypeError(`${field} below minimum`);
  }
  return value;
}
function currency(value) {
  if (typeof value !== "string" || !CURRENCY.test(value)) {
    throw new TypeError("invalid currency code");
  }
  return value;
}
function instrument(value) {
  for (const field of ["market", "ticker", "assetType"]) {
    if (!IDENTIFIER.test(required(value[field], field))) throw new TypeError("invalid instrument");
  }
  currency(value.currency);
  return value;
}
async function importAccount(db, row) {
  fingerprint(row.fingerprint);
  required(row.broker, "broker");
  currency(row.baseCurrency);
  if (row.maskedLabel !== null && row.maskedLabel !== void 0 && (typeof row.maskedLabel !== "string" || !MASKED_LABEL.test(row.maskedLabel))) {
    throw new TypeError("invalid masked label");
  }
  const result2 = await db.prepare(`INSERT INTO cloud_accounts
    (account_fingerprint, broker, masked_label, base_currency) VALUES (?, ?, ?, ?)`).bind(row.fingerprint, row.broker, row.maskedLabel ?? null, row.baseCurrency).run();
  return result2.meta.changes === 1;
}
async function recordTransactionOnce(db, row) {
  fingerprint(row.accountFingerprint);
  if (required(row.sourceKey, "sourceKey").length > 160) {
    throw new TypeError("sourceKey too long");
  }
  required(row.source, "source");
  currency(row.currency);
  if (row.instrument !== null) instrument(row.instrument);
  if (row.instrument !== null && row.instrument.currency !== row.currency) {
    throw new TypeError("instrument and transaction currency disagree");
  }
  amount(required(row.quantity, "quantity"), "quantity", "0");
  amount(row.unitPrice, "unitPrice", "0");
  for (const field of ["grossAmount", "fee", "tax"]) amount(row[field], field);
  amount(row.netAmount, "netAmount");
  const result2 = await db.prepare(`INSERT INTO cloud_transactions
    (account_fingerprint, source_key, source, market, ticker, asset_type,
     currency, transaction_type, status, traded_at, settled_at, quantity,
     unit_price, gross_amount, fee, tax, net_amount)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(account_fingerprint, source_key) DO NOTHING`).bind(
    row.accountFingerprint,
    row.sourceKey,
    row.source,
    row.instrument?.market ?? null,
    row.instrument?.ticker ?? null,
    row.instrument?.assetType ?? null,
    row.currency,
    required(row.transactionType, "transactionType"),
    required(row.status, "status"),
    required(row.tradedAt, "tradedAt"),
    row.settledAt,
    row.quantity,
    row.unitPrice,
    row.grossAmount,
    row.fee,
    row.tax,
    row.netAmount
  ).run();
  return result2.meta.changes === 1;
}

// deploy/cloudflare-js/outbox.mjs
function changes(result2) {
  const count = result2?.meta?.changes;
  if (!Number.isSafeInteger(count) || count < 0) throw new TypeError("invalid D1 change count");
  return count;
}
function stage(db, key2, destination, payload, now, table, column, claimKey, nonce) {
  return db.prepare(`INSERT INTO cloud_outbox (event_key, destination_key, payload_json, created_at)
    SELECT ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM ${table} WHERE ${column} = ? AND claim_nonce = ?)`).bind(key2, destination, payload, now, claimKey, nonce);
}
function mutationDeliveryStatements(db, delivery, evidence) {
  if (!delivery) return [];
  const { updateId, destination, reply, now } = delivery;
  if (!Number.isSafeInteger(updateId) || updateId <= 0 || !/^[1-9]\d*$/.test(destination) || typeof reply !== "string" || !reply || reply.length > 3900 || typeof now !== "string" || !now) {
    throw new TypeError("invalid mutation delivery");
  }
  const selectors = {
    plan: [`cloud_plan_revisions`, `plan_key = ? AND mutation_key = ?`],
    group: [`cloud_mutation_checks`, `mutation_key = ?`],
    thesis: [`cloud_thesis_revisions`, `instrument_key = ? AND mutation_key = ?`]
  };
  const selected = selectors[evidence?.kind];
  if (!selected || !Array.isArray(evidence.values)) throw new TypeError("invalid mutation evidence");
  const nonce = crypto.randomUUID();
  return [
    db.prepare(`INSERT INTO cloud_update_claims (update_id, received_at, claim_nonce)
    VALUES (?, ?, ?)`).bind(updateId, now, nonce),
    db.prepare(`INSERT INTO cloud_outbox
    (event_key, destination_key, payload_json, created_at) VALUES (?, ?,
      CASE WHEN EXISTS (SELECT 1 FROM ${selected[0]} WHERE ${selected[1]}) AND
        EXISTS (SELECT 1 FROM cloud_update_claims WHERE update_id = ? AND claim_nonce = ?)
      THEN ? ELSE NULL END, ?)`).bind(
      `update:${updateId}:0`,
      destination,
      ...evidence.values,
      updateId,
      nonce,
      JSON.stringify({ text: reply }),
      now
    )
  ];
}
async function enqueueUpdate(db, updateId, destination, payload, now) {
  if (!Number.isSafeInteger(updateId) || updateId <= 0 || !destination || !payload || !now) {
    throw new TypeError("invalid update claim");
  }
  const nonce = crypto.randomUUID();
  const results = await db.batch([
    db.prepare(`INSERT INTO cloud_update_claims (update_id, received_at, claim_nonce)
      VALUES (?, ?, ?) ON CONFLICT(update_id) DO NOTHING`).bind(updateId, now, nonce),
    stage(
      db,
      `update:${updateId}:0`,
      destination,
      payload,
      now,
      "cloud_update_claims",
      "update_id",
      updateId,
      nonce
    )
  ]);
  if (results.length !== 2 || changes(results[0]) !== changes(results[1]) || ![0, 1].includes(changes(results[0]))) throw new Error("claim and outbox disagree");
  return changes(results[0]) === 1;
}
async function beginSend(db, key2) {
  if (!key2) throw new TypeError("empty event key");
  const result2 = await db.prepare(`UPDATE cloud_outbox SET status = 'SENDING'
    WHERE event_key = ? AND status = 'PENDING'`).bind(key2).run();
  return changes(result2) === 1;
}
async function finishSend(db, key2, sent, now) {
  if (!key2 || !now) throw new TypeError("missing send result");
  const result2 = await db.prepare(`UPDATE cloud_outbox SET status = ?, finished_at = ?
    WHERE event_key = ? AND status = 'SENDING'`).bind(sent ? "SENT" : "UNKNOWN", now, key2).run();
  return changes(result2) === 1;
}

// deploy/cloudflare-js/cloud-write.mjs
var FINGERPRINT2 = /^[a-z][a-z0-9_]{0,30}_[0-9a-f]{64}$/;
var SYMBOL = /^[A-Z0-9][A-Z0-9._:-]{0,159}$/;
var MUTATION = /^[A-Za-z0-9._:-]{1,128}$/;
var encode = new TextEncoder();
async function hash(value) {
  const bytes = new Uint8Array(await crypto.subtle.digest("SHA-256", encode.encode(value)));
  return [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}
function resultChanges(value) {
  if (!Number.isSafeInteger(value?.meta?.changes)) throw new TypeError("invalid D1 result");
  return value.meta.changes;
}
function required2(value, name) {
  if (typeof value !== "string" || !value.trim()) throw new TypeError(`${name} required`);
  return value;
}
function normalizeState(state) {
  if (!state || !state.instrument || typeof state.enabled !== "boolean" || !SYMBOL.test(state.instrument.market) || !SYMBOL.test(state.instrument.ticker) || !SYMBOL.test(state.instrument.assetType) || !/^[A-Z]{3}$/.test(state.instrument.currency) || !/^[A-Z]{3}$/.test(state.amountCurrency) || state.accountFingerprint !== null && !FINGERPRINT2.test(state.accountFingerprint) || state.memo !== null && (typeof state.memo !== "string" || state.memo.length > 2e3)) {
    throw new TypeError("invalid plan state");
  }
  required2(state.source, "source");
  required2(state.timezoneName, "timezoneName");
  required2(state.startedAt, "startedAt");
  if (compare(decimal(state.dailyAmount), decimal("0.00000001")) < 0 || state.targetWeight !== null && (compare(decimal(state.targetWeight), decimal("0")) < 0 || compare(decimal(state.targetWeight), decimal("1")) > 0)) {
    throw new TypeError("invalid plan amount or weight");
  }
  return {
    ...state,
    dailyAmount: exact(decimal(state.dailyAmount)),
    targetWeight: state.targetWeight === null ? null : exact(decimal(state.targetWeight))
  };
}
function fromRow(row) {
  return {
    instrument: {
      market: row.market,
      ticker: row.ticker,
      assetType: row.asset_type,
      currency: row.instrument_currency
    },
    accountFingerprint: row.account_fingerprint,
    source: row.source,
    amountCurrency: row.amount_currency,
    dailyAmount: row.daily_amount,
    targetWeight: row.target_weight,
    enabled: row.enabled === 1,
    timezoneName: row.timezone_name,
    startedAt: row.started_at,
    memo: row.memo
  };
}
function statePayload(state) {
  return JSON.stringify([
    state.instrument.market,
    state.instrument.ticker,
    state.instrument.assetType,
    state.instrument.currency,
    state.accountFingerprint,
    state.source,
    state.amountCurrency,
    state.dailyAmount,
    state.targetWeight,
    state.enabled,
    state.timezoneName,
    state.startedAt,
    state.memo
  ]);
}
async function listCurrentPlans(db, enabledOnly = false) {
  const query = enabledOnly ? `SELECT * FROM cloud_plans WHERE enabled = 1 ORDER BY plan_key` : `SELECT * FROM cloud_plans ORDER BY plan_key`;
  const { results } = await db.prepare(query).all();
  return results.map((row) => ({
    planKey: row.plan_key,
    version: row.version,
    ...fromRow(row)
  }));
}
async function createPlanOnce(db, planKey, mutationKey, initialState, effectiveAt, delivery = null) {
  if (!MUTATION.test(required2(planKey, "planKey")) || !MUTATION.test(mutationKey)) {
    throw new TypeError("invalid plan creation key");
  }
  required2(effectiveAt, "effectiveAt");
  const state = normalizeState(initialState);
  const changeHash = await hash(statePayload(state));
  const deliveryStatements = mutationDeliveryStatements(
    db,
    delivery,
    { kind: "plan", values: [planKey, mutationKey] }
  );
  const results = await db.batch([
    ...delivery ? [deliveryStatements[0]] : [],
    db.prepare(`INSERT INTO cloud_plans
      (plan_key, account_fingerprint, source, market, ticker, asset_type,
       instrument_currency, amount_currency, daily_amount, target_weight,
       enabled, timezone_name, started_at, memo, version, last_mutation_key)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
      ON CONFLICT(plan_key) DO NOTHING`).bind(
      planKey,
      state.accountFingerprint,
      state.source,
      state.instrument.market,
      state.instrument.ticker,
      state.instrument.assetType,
      state.instrument.currency,
      state.amountCurrency,
      state.dailyAmount,
      state.targetWeight,
      state.enabled ? 1 : 0,
      state.timezoneName,
      state.startedAt,
      state.memo,
      mutationKey
    ),
    db.prepare(`INSERT INTO cloud_plan_revisions
      (plan_key, revision_no, change_hash, source, account_fingerprint,
       market, ticker, asset_type, instrument_currency, enabled, daily_amount,
       amount_currency, target_weight, timezone_name, started_at, effective_from,
       memo, mutation_key)
      SELECT plan_key, 1, ?, source, account_fingerprint, market, ticker,
       asset_type, instrument_currency, enabled, daily_amount, amount_currency,
       target_weight, timezone_name, started_at, ?, memo, ? FROM cloud_plans
      WHERE plan_key = ? AND version = 1 AND last_mutation_key = ?
        AND NOT EXISTS (SELECT 1 FROM cloud_plan_revisions WHERE plan_key = ?)`).bind(changeHash, effectiveAt, mutationKey, planKey, mutationKey, planKey),
    ...delivery ? [deliveryStatements[1]] : []
  ]);
  const offset = delivery ? 1 : 0;
  const created = resultChanges(results[offset]);
  const revision = resultChanges(results[offset + 1]);
  if (created !== revision || ![0, 1].includes(created)) {
    throw new Error("plan creation transaction mismatch");
  }
  if (delivery && (resultChanges(results[0]) !== created || resultChanges(results.at(-1)) !== created)) {
    throw new Error("plan delivery transaction mismatch");
  }
  if (created) return "CREATED";
  const existing = await db.prepare(`SELECT * FROM cloud_plans WHERE plan_key = ?`).bind(planKey).first();
  return existing?.last_mutation_key === mutationKey && statePayload(normalizeState(fromRow(existing))) === statePayload(state) ? "REPLAY" : "CONFLICT";
}
async function applyPlanChange(db, planKey, expectedVersion, mutationKey, nextState, effectiveAt, delivery = null) {
  if (!MUTATION.test(required2(planKey, "planKey")) || !MUTATION.test(mutationKey) || !Number.isSafeInteger(expectedVersion) || expectedVersion < 1) {
    throw new TypeError("invalid plan mutation");
  }
  required2(effectiveAt, "effectiveAt");
  const current = await db.prepare(`SELECT * FROM cloud_plans WHERE plan_key = ?`).bind(planKey).first();
  if (!current) return "MISSING";
  if (current.version !== expectedVersion) return "CONFLICT";
  const state = normalizeState(nextState);
  if (state.accountFingerprint !== current.account_fingerprint || state.source !== current.source || state.instrument.market !== current.market || state.instrument.ticker !== current.ticker || state.instrument.assetType !== current.asset_type || state.instrument.currency !== current.instrument_currency) {
    throw new TypeError("plan edit cannot change account, source or instrument");
  }
  if (statePayload(state) === statePayload(normalizeState(fromRow(current)))) return "UNCHANGED";
  const changeHash = await hash(statePayload(state));
  const deliveryStatements = mutationDeliveryStatements(
    db,
    delivery,
    { kind: "plan", values: [planKey, mutationKey] }
  );
  const results = await db.batch([
    ...delivery ? [deliveryStatements[0]] : [],
    db.prepare(`UPDATE cloud_plans SET account_fingerprint = ?, source = ?, market = ?,
      ticker = ?, asset_type = ?, instrument_currency = ?, amount_currency = ?,
      daily_amount = ?, target_weight = ?, enabled = ?, timezone_name = ?,
      started_at = ?, memo = ?, version = version + 1, last_mutation_key = ?
      WHERE plan_key = ? AND version = ? AND NOT EXISTS (
        SELECT 1 FROM cloud_plan_revisions WHERE plan_key = ? AND mutation_key = ?
      )`).bind(
      state.accountFingerprint,
      state.source,
      state.instrument.market,
      state.instrument.ticker,
      state.instrument.assetType,
      state.instrument.currency,
      state.amountCurrency,
      state.dailyAmount,
      state.targetWeight,
      state.enabled ? 1 : 0,
      state.timezoneName,
      state.startedAt,
      state.memo,
      mutationKey,
      planKey,
      expectedVersion,
      planKey,
      mutationKey
    ),
    db.prepare(`INSERT INTO cloud_plan_revisions
      (plan_key, revision_no, change_hash, source, account_fingerprint,
       market, ticker, asset_type, instrument_currency, enabled, daily_amount,
       amount_currency, target_weight, timezone_name, started_at, effective_from,
       memo, mutation_key)
      SELECT plan_key, version, ?, source, account_fingerprint, market, ticker,
       asset_type, instrument_currency, enabled, daily_amount, amount_currency,
       target_weight, timezone_name, started_at, ?, memo, ? FROM cloud_plans
      WHERE plan_key = ? AND version = ? AND last_mutation_key = ?
        AND NOT EXISTS (SELECT 1 FROM cloud_plan_revisions
          WHERE plan_key = ? AND mutation_key = ?)`).bind(
      changeHash,
      effectiveAt,
      mutationKey,
      planKey,
      expectedVersion + 1,
      mutationKey,
      planKey,
      mutationKey
    ),
    ...delivery ? [deliveryStatements[1]] : []
  ]);
  const offset = delivery ? 1 : 0;
  const first = resultChanges(results[offset]);
  const second = resultChanges(results[offset + 1]);
  if (first !== second || ![0, 1].includes(first)) throw new Error("plan revision transaction mismatch");
  if (delivery && (resultChanges(results[0]) !== first || resultChanges(results.at(-1)) !== first)) {
    throw new Error("plan delivery transaction mismatch");
  }
  return first ? "CHANGED" : "CONFLICT";
}
async function applyGroupChanges(db, changes2, mutationKey, effectiveAt, delivery = null) {
  if (!MUTATION.test(mutationKey) || !Array.isArray(changes2) || !changes2.length || changes2.length > 100 || new Set(changes2.map((item) => item.planKey)).size !== changes2.length) {
    throw new TypeError("invalid bounded group mutation");
  }
  required2(effectiveAt, "effectiveAt");
  const prior = await groupMutationPlans(db, mutationKey);
  if (prior.length) return "REPLAY";
  const work = [];
  for (const item of changes2) {
    if (!MUTATION.test(item.planKey) || !Number.isSafeInteger(item.expectedVersion) || item.expectedVersion < 1) throw new TypeError("invalid group member");
    const current = await db.prepare(`SELECT * FROM cloud_plans WHERE plan_key = ?`).bind(item.planKey).first();
    if (!current || current.version !== item.expectedVersion) return "CONFLICT";
    const next = normalizeState(item.nextState);
    if (next.accountFingerprint !== current.account_fingerprint || next.source !== current.source || next.instrument.market !== current.market || next.instrument.ticker !== current.ticker || next.instrument.assetType !== current.asset_type || next.instrument.currency !== current.instrument_currency) {
      throw new TypeError("group edit crossed account, source or instrument");
    }
    if (statePayload(next) !== statePayload(normalizeState(fromRow(current)))) {
      work.push({
        ...item,
        next,
        changeHash: await hash(statePayload(next)),
        memberKey: await hash(`${mutationKey}:${item.planKey}`)
      });
    }
  }
  if (!work.length) return "UNCHANGED";
  const statements = [];
  const deliveryStatements = mutationDeliveryStatements(
    db,
    delivery,
    { kind: "group", values: [mutationKey] }
  );
  if (delivery) statements.push(deliveryStatements[0]);
  for (const item of work) {
    statements.push(db.prepare(`INSERT INTO cloud_mutation_checks
      (mutation_key, plan_key, valid) VALUES (?, ?,
       (SELECT CASE WHEN version = ? THEN 1 ELSE 0 END
        FROM cloud_plans WHERE plan_key = ?))`).bind(mutationKey, item.planKey, item.expectedVersion, item.planKey));
    const state = item.next;
    statements.push(db.prepare(`UPDATE cloud_plans SET daily_amount = ?, amount_currency = ?,
      target_weight = ?, enabled = ?, timezone_name = ?, started_at = ?, memo = ?,
      version = version + 1, last_mutation_key = ?
      WHERE plan_key = ? AND version = ?`).bind(
      state.dailyAmount,
      state.amountCurrency,
      state.targetWeight,
      state.enabled ? 1 : 0,
      state.timezoneName,
      state.startedAt,
      state.memo,
      item.memberKey,
      item.planKey,
      item.expectedVersion
    ));
    statements.push(db.prepare(`INSERT INTO cloud_plan_revisions
      (plan_key, revision_no, change_hash, source, account_fingerprint,
       market, ticker, asset_type, instrument_currency, enabled, daily_amount,
       amount_currency, target_weight, timezone_name, started_at, effective_from,
       memo, mutation_key)
      SELECT plan_key, version, ?, source, account_fingerprint, market, ticker,
       asset_type, instrument_currency, enabled, daily_amount, amount_currency,
       target_weight, timezone_name, started_at, ?, memo, ? FROM cloud_plans
      WHERE plan_key = ? AND version = ? AND last_mutation_key = ?`).bind(
      item.changeHash,
      effectiveAt,
      item.memberKey,
      item.planKey,
      item.expectedVersion + 1,
      item.memberKey
    ));
  }
  if (delivery) statements.push(deliveryStatements[1]);
  const results = await db.batch(statements);
  if (results.length !== statements.length || results.some((result2) => resultChanges(result2) !== 1)) {
    throw new Error("group mutation transaction mismatch");
  }
  return "CHANGED";
}
async function groupMutationPlans(db, mutationKey) {
  if (!MUTATION.test(mutationKey)) throw new TypeError("invalid group mutation key");
  const { results } = await db.prepare(`SELECT plan_key FROM cloud_mutation_checks
    WHERE mutation_key = ? ORDER BY plan_key`).bind(mutationKey).all();
  return results.map((row) => row.plan_key);
}
async function appendThesis(db, {
  market,
  ticker,
  currency: currency2,
  text,
  revisionKey,
  mutationKey,
  expectedRevision,
  effectiveAt,
  delivery = null
}) {
  if (![market, ticker].every((value) => SYMBOL.test(value)) || !/^[A-Z]{3}$/.test(currency2) || !MUTATION.test(revisionKey) || !MUTATION.test(mutationKey) || !Number.isSafeInteger(expectedRevision) || expectedRevision < 0 || typeof text !== "string" || !text.trim() || text.length > 2e4) {
    throw new TypeError("invalid thesis revision");
  }
  required2(effectiveAt, "effectiveAt");
  const identity = `${market}:${ticker}`;
  const contentHash = await hash(text.trim());
  const statements = [];
  const deliveryStatements = mutationDeliveryStatements(
    db,
    delivery,
    { kind: "thesis", values: [identity, mutationKey] }
  );
  if (delivery) statements.push(deliveryStatements[0]);
  if (expectedRevision === 0) statements.push(db.prepare(`INSERT INTO cloud_theses
    (instrument_key, market, ticker, currency, current_revision_no)
    VALUES (?, ?, ?, ?, 0) ON CONFLICT(instrument_key) DO NOTHING`).bind(identity, market, ticker, currency2));
  statements.push(db.prepare(`UPDATE cloud_theses SET
    current_revision_no = current_revision_no + 1, last_mutation_key = ?
    WHERE instrument_key = ? AND current_revision_no = ?
      AND NOT EXISTS (SELECT 1 FROM cloud_thesis_revisions
        WHERE instrument_key = ? AND (revision_key = ? OR mutation_key = ?))`).bind(mutationKey, identity, expectedRevision, identity, revisionKey, mutationKey));
  statements.push(db.prepare(`INSERT INTO cloud_thesis_revisions
    (instrument_key, revision_no, revision_key, content_hash, thesis_text,
     effective_at, mutation_key)
    SELECT instrument_key, current_revision_no, ?, ?, ?, ?, ?
    FROM cloud_theses WHERE instrument_key = ? AND current_revision_no = ?
      AND last_mutation_key = ? AND NOT EXISTS (
        SELECT 1 FROM cloud_thesis_revisions
        WHERE instrument_key = ? AND mutation_key = ?)`).bind(
    revisionKey,
    contentHash,
    text.trim(),
    effectiveAt,
    mutationKey,
    identity,
    expectedRevision + 1,
    mutationKey,
    identity,
    mutationKey
  ));
  if (delivery) statements.push(deliveryStatements[1]);
  const results = await db.batch(statements);
  const updated = resultChanges(results.at(delivery ? -3 : -2));
  const inserted = resultChanges(results.at(delivery ? -2 : -1));
  if (updated !== inserted || ![0, 1].includes(updated)) {
    throw new Error("thesis revision transaction mismatch");
  }
  if (delivery && (resultChanges(results[0]) !== updated || resultChanges(results.at(-1)) !== updated)) {
    throw new Error("thesis delivery transaction mismatch");
  }
  return updated ? "CHANGED" : "CONFLICT";
}
async function currentThesis(db, market, ticker) {
  if (!SYMBOL.test(market) || !SYMBOL.test(ticker)) throw new TypeError("invalid thesis identity");
  return db.prepare(`SELECT r.thesis_text, r.revision_no, r.revision_key
    FROM cloud_theses AS t JOIN cloud_thesis_revisions AS r
      ON r.instrument_key = t.instrument_key AND r.revision_no = t.current_revision_no
    WHERE t.instrument_key = ?`).bind(`${market}:${ticker}`).first();
}

// deploy/cloudflare-js/synthetic-harness.mjs
var RUN_KEY = "synthetic-v2";
var ACCOUNT = `fixture_${"c".repeat(64)}`;
var DDL = [
  `CREATE TABLE IF NOT EXISTS cloud_test_runs (
    run_key TEXT PRIMARY KEY, status TEXT NOT NULL, completed_at TEXT)`,
  `CREATE TABLE IF NOT EXISTS cloud_accounts (
    account_fingerprint TEXT PRIMARY KEY, broker TEXT NOT NULL,
    masked_label TEXT, base_currency TEXT NOT NULL)`,
  `CREATE TABLE IF NOT EXISTS cloud_transactions (
    account_fingerprint TEXT NOT NULL REFERENCES cloud_accounts(account_fingerprint),
    source_key TEXT NOT NULL, source TEXT NOT NULL, market TEXT, ticker TEXT,
    asset_type TEXT, currency TEXT NOT NULL, transaction_type TEXT NOT NULL,
    status TEXT NOT NULL, traded_at TEXT NOT NULL, settled_at TEXT, quantity TEXT NOT NULL,
    unit_price TEXT, gross_amount TEXT, fee TEXT, tax TEXT, net_amount TEXT,
    PRIMARY KEY (account_fingerprint, source_key))`,
  `CREATE TABLE IF NOT EXISTS cloud_update_claims (
    update_id INTEGER PRIMARY KEY, received_at TEXT NOT NULL, claim_nonce TEXT NOT NULL)`,
  `CREATE TABLE IF NOT EXISTS cloud_report_claims (
    period_key TEXT PRIMARY KEY, claimed_at TEXT NOT NULL, claim_nonce TEXT NOT NULL)`,
  `CREATE TABLE IF NOT EXISTS cloud_outbox (
    event_key TEXT PRIMARY KEY, destination_key TEXT NOT NULL, payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING'
      CHECK (status IN ('PENDING', 'SENDING', 'SENT', 'UNKNOWN')),
    created_at TEXT NOT NULL, finished_at TEXT)`,
  `CREATE TABLE IF NOT EXISTS cloud_plans (
    plan_key TEXT PRIMARY KEY, account_fingerprint TEXT REFERENCES cloud_accounts(account_fingerprint),
    source TEXT NOT NULL, market TEXT NOT NULL, ticker TEXT NOT NULL,
    asset_type TEXT NOT NULL, instrument_currency TEXT NOT NULL,
    amount_currency TEXT NOT NULL, daily_amount TEXT NOT NULL, target_weight TEXT,
    enabled INTEGER NOT NULL, timezone_name TEXT NOT NULL, started_at TEXT NOT NULL,
    memo TEXT, version INTEGER NOT NULL, last_mutation_key TEXT)`,
  `CREATE TABLE IF NOT EXISTS cloud_plan_revisions (
    plan_key TEXT NOT NULL REFERENCES cloud_plans(plan_key), revision_no INTEGER NOT NULL,
    change_hash TEXT NOT NULL, source TEXT NOT NULL, account_fingerprint TEXT,
    market TEXT NOT NULL, ticker TEXT NOT NULL, asset_type TEXT NOT NULL,
    instrument_currency TEXT NOT NULL, enabled INTEGER NOT NULL, daily_amount TEXT NOT NULL,
    amount_currency TEXT NOT NULL, target_weight TEXT, timezone_name TEXT NOT NULL,
    started_at TEXT NOT NULL, effective_from TEXT NOT NULL, memo TEXT,
    mutation_key TEXT, PRIMARY KEY (plan_key, revision_no),
    UNIQUE (plan_key, mutation_key))`,
  `CREATE TABLE IF NOT EXISTS cloud_mutation_checks (
    mutation_key TEXT NOT NULL, plan_key TEXT NOT NULL,
    valid INTEGER NOT NULL CHECK (valid = 1),
    PRIMARY KEY (mutation_key, plan_key))`,
  `CREATE TABLE IF NOT EXISTS cloud_theses (
    instrument_key TEXT PRIMARY KEY, market TEXT NOT NULL, ticker TEXT NOT NULL,
    currency TEXT NOT NULL, current_revision_no INTEGER NOT NULL, last_mutation_key TEXT)`,
  `CREATE TABLE IF NOT EXISTS cloud_thesis_revisions (
    instrument_key TEXT NOT NULL REFERENCES cloud_theses(instrument_key),
    revision_no INTEGER NOT NULL, revision_key TEXT NOT NULL, content_hash TEXT NOT NULL,
    thesis_text TEXT NOT NULL, effective_at TEXT NOT NULL, mutation_key TEXT NOT NULL,
    PRIMARY KEY (instrument_key, revision_no), UNIQUE (instrument_key, revision_key),
    UNIQUE (instrument_key, mutation_key))`
];
async function initSyntheticTables(db) {
  for (const statement of DDL) await db.prepare(statement).run();
}
async function runOnce(db) {
  await initSyntheticTables(db);
  const claimed = await db.prepare(`INSERT INTO cloud_test_runs (run_key, status)
    VALUES (?, 'RUNNING') ON CONFLICT(run_key) DO NOTHING`).bind(RUN_KEY).run();
  if (claimed.meta.changes !== 1) return;
  try {
    await importAccount(db, {
      fingerprint: ACCOUNT,
      broker: "synthetic",
      maskedLabel: "***fixture",
      baseCurrency: "KRW"
    });
    const row = {
      accountFingerprint: ACCOUNT,
      sourceKey: "fill-fixed",
      source: "synthetic",
      instrument: null,
      currency: "KRW",
      transactionType: "CASH",
      status: "FILLED",
      tradedAt: "2026-02-02T00:00:00Z",
      settledAt: null,
      quantity: "0",
      unitPrice: null,
      grossAmount: "10.25",
      fee: null,
      tax: null,
      netAmount: "10.25"
    };
    const ledgerFirst = await recordTransactionOnce(db, row);
    const ledgerReplay = await recordTransactionOnce(db, row);
    const claims = await Promise.all([
      enqueueUpdate(db, 731001, "synthetic", '{"fixture":true}', "synthetic-t0"),
      enqueueUpdate(db, 731001, "synthetic", '{"fixture":true}', "synthetic-t0")
    ]);
    const sendStarted = await beginSend(db, "update:731001:0");
    const uncertain = await finishSend(db, "update:731001:0", false, "synthetic-t1");
    const replay = await enqueueUpdate(
      db,
      731001,
      "synthetic",
      '{"fixture":true}',
      "synthetic-t2"
    );
    const state = {
      instrument: {
        market: "NASDAQ",
        ticker: "FIXTURE",
        assetType: "ETF",
        currency: "USD"
      },
      accountFingerprint: ACCOUNT,
      source: "TELEGRAM",
      amountCurrency: "KRW",
      dailyAmount: "1000",
      targetWeight: "0.1",
      enabled: true,
      timezoneName: "Asia/Seoul",
      startedAt: "2026-02-01T00:00:00Z",
      memo: null
    };
    const planFirst = await createPlanOnce(
      db,
      "manual:NASDAQ:FIXTURE",
      "fixture-plan-create",
      state,
      "2026-02-01T00:00:00Z"
    );
    const planReplay = await createPlanOnce(
      db,
      "manual:NASDAQ:FIXTURE",
      "fixture-plan-create",
      state,
      "2026-02-01T00:00:00Z"
    );
    const planChanged = await applyPlanChange(
      db,
      "manual:NASDAQ:FIXTURE",
      1,
      "fixture-plan-change",
      { ...state, dailyAmount: "3000" },
      "2026-02-02T00:00:00Z"
    );
    const planStale = await applyPlanChange(
      db,
      "manual:NASDAQ:FIXTURE",
      1,
      "fixture-plan-stale",
      { ...state, dailyAmount: "3000" },
      "2026-02-02T00:00:00Z"
    );
    const second = {
      ...state,
      instrument: { ...state.instrument, ticker: "FIXTURE2" },
      dailyAmount: "4000"
    };
    const secondCreated = await createPlanOnce(
      db,
      "manual:NASDAQ:FIXTURE2",
      "fixture-plan-create-2",
      second,
      "2026-02-01T00:00:00Z"
    );
    const group = [
      {
        planKey: "manual:NASDAQ:FIXTURE",
        expectedVersion: 2,
        nextState: { ...state, dailyAmount: "2000" }
      },
      {
        planKey: "manual:NASDAQ:FIXTURE2",
        expectedVersion: 1,
        nextState: { ...second, dailyAmount: "3000" }
      }
    ];
    const groupChanged = await applyGroupChanges(
      db,
      group,
      "fixture-group",
      "2026-02-02T00:00:00Z"
    );
    const groupReplay = await applyGroupChanges(
      db,
      group,
      "fixture-group",
      "2026-02-02T00:00:00Z"
    );
    const thesis = {
      market: "NASDAQ",
      ticker: "FIXTURE",
      currency: "USD",
      text: "synthetic-only thesis",
      revisionKey: "fixture-revision",
      mutationKey: "fixture-thesis-create",
      expectedRevision: 0,
      effectiveAt: "2026-02-01T00:00:00Z"
    };
    const thesisFirst = await appendThesis(db, thesis);
    const thesisReplay = await appendThesis(db, thesis);
    const counts = await db.prepare(`SELECT
      (SELECT COUNT(*) FROM cloud_transactions) AS ledger_count,
      (SELECT COUNT(*) FROM cloud_outbox WHERE status = 'UNKNOWN') AS unknown_count,
      (SELECT COUNT(*) FROM cloud_update_claims) AS update_count,
      (SELECT COUNT(*) FROM cloud_plan_revisions) AS plan_revisions,
      (SELECT COUNT(*) FROM cloud_mutation_checks) AS group_checks,
      (SELECT COUNT(*) FROM cloud_thesis_revisions) AS thesis_revisions`).first();
    const passed = ledgerFirst && !ledgerReplay && claims.filter(Boolean).length === 1 && sendStarted && uncertain && !replay && counts.ledger_count === 1 && counts.unknown_count === 1 && counts.update_count === 1 && planFirst === "CREATED" && planReplay === "REPLAY" && planChanged === "CHANGED" && planStale === "CONFLICT" && secondCreated === "CREATED" && groupChanged === "CHANGED" && groupReplay === "REPLAY" && counts.group_checks === 2 && thesisFirst === "CHANGED" && thesisReplay === "CONFLICT" && counts.plan_revisions === 5 && counts.thesis_revisions === 1;
    await db.prepare(`UPDATE cloud_test_runs SET status = ?, completed_at = ?
      WHERE run_key = ? AND status = 'RUNNING'`).bind(
      passed ? "PASS" : "FAIL",
      (/* @__PURE__ */ new Date()).toISOString(),
      RUN_KEY
    ).run();
  } catch {
    await db.prepare(`UPDATE cloud_test_runs SET status = 'FAIL', completed_at = ?
      WHERE run_key = ? AND status = 'RUNNING'`).bind((/* @__PURE__ */ new Date()).toISOString(), RUN_KEY).run();
  }
}
var synthetic_harness_default = {
  async fetch(request, env) {
    const path = new URL(request.url).pathname;
    if (request.method !== "GET" || path !== "/health") {
      return new Response("not found", { status: 404 });
    }
    if (!env.DB) return Response.json({ status: "UNBOUND" }, { status: 503 });
    try {
      const run = await env.DB.prepare(`SELECT status FROM cloud_test_runs
        WHERE run_key = ?`).bind(RUN_KEY).first();
      return Response.json(
        { status: run?.status || "NOT_RUN" },
        { headers: { "Cache-Control": "no-store" } }
      );
    } catch {
      return Response.json(
        { status: "NOT_RUN" },
        { headers: { "Cache-Control": "no-store" } }
      );
    }
  },
  async scheduled(_event, env) {
    if (env.TEST_WRITES_ENABLED !== "1" || !env.DB) return;
    await runOnce(env.DB);
  }
};

// deploy/cloudflare-js/aggregation.mjs
var US_MARKETS = /* @__PURE__ */ new Set(["US", "USA", "NASDAQ", "NAS", "NYSE", "AMEX", "ARCA", "NYSEARCA"]);
var US_GENERIC = /* @__PURE__ */ new Set(["US", "USA"]);
var USABLE = /* @__PURE__ */ new Set(["ok", "partial", "stale"]);
var STATUSES = /* @__PURE__ */ new Set([...USABLE, "not_configured", "auth_failed", "rate_limited", "unavailable"]);
var ASSET_TYPES = /* @__PURE__ */ new Set(["EQUITY", "ETF", "FUND", "BOND", "CASH", "OTHER"]);
var zero = () => decimal("0");
var instrumentKey = (item) => `${item.market}:${item.ticker}:${item.assetType}:${item.currency}`;
function nonempty(value) {
  return typeof value === "string" && value.trim().length > 0;
}
function validateInstrument(item) {
  if (item === null || typeof item !== "object" || !nonempty(item.market) || !nonempty(item.ticker) || !ASSET_TYPES.has(item.assetType) || !["KRW", "USD"].includes(item.currency) || [item.market, item.ticker, item.assetType].some((value) => typeof value !== "string" || value.trim() !== value || value !== value.toUpperCase())) {
    throw new TypeError("invalid normalized instrument");
  }
}
function resultMap(results) {
  const mapped = /* @__PURE__ */ new Map();
  for (const result2 of results) {
    if (!result2.broker || result2.broker !== result2.broker.toLowerCase() || !STATUSES.has(result2.status) || !Array.isArray(result2.items) || mapped.has(result2.broker)) throw new TypeError("invalid or duplicate broker result");
    mapped.set(result2.broker, result2);
  }
  return mapped;
}
function fxBook(quotes, baseCurrency) {
  const pairs = /* @__PURE__ */ new Map();
  for (const quote of quotes) {
    const key2 = `${quote.base}/${quote.quote}`;
    if (quote.base === quote.quote || pairs.has(key2) || compare(decimal(quote.rate), zero()) <= 0) {
      throw new TypeError("duplicate or invalid FX quote");
    }
    pairs.set(key2, quote);
  }
  return (value, currency2) => {
    if (currency2 === baseCurrency) return [value, false];
    const quote = pairs.get(`${currency2}/${baseCurrency}`);
    if (quote) return [multiply(value, decimal(quote.rate)), quote.isStale === true];
    const inverse = pairs.get(`${baseCurrency}/${currency2}`);
    if (inverse) return [divide28(value, decimal(inverse.rate)), inverse.isStale === true];
    return [null, false];
  };
}
function effectiveValue(position, field, fallback) {
  if (position[field] !== null && position[field] !== void 0) return decimal(position[field]);
  if (position[fallback] !== null && position[fallback] !== void 0) {
    return multiply(decimal(position[fallback]), decimal(position.quantity));
  }
  return null;
}
function aggregateCoverage({
  baseCurrency = "KRW",
  fxQuotes = [],
  positionResults,
  cashResults = [],
  expectedBrokers = null
}) {
  if (!["KRW", "USD"].includes(baseCurrency)) throw new TypeError("unsupported base currency");
  const positions = resultMap(positionResults);
  const cash = resultMap(cashResults);
  const convert = fxBook(fxQuotes, baseCurrency);
  const expected = new Set(expectedBrokers === null ? [] : expectedBrokers.map((broker) => {
    if (!nonempty(broker)) throw new TypeError("empty expected broker");
    return broker.trim().toLowerCase();
  }));
  for (const broker of [...positions.keys(), ...cash.keys()]) expected.add(broker);
  if (expected.has("")) throw new TypeError("empty expected broker");
  const expectedSorted = [...expected].sort();
  const includesCash = cashResults.length > 0;
  const incomplete = /* @__PURE__ */ new Set();
  const included = /* @__PURE__ */ new Set();
  const warnings = [];
  for (const broker of expectedSorted) {
    const result2 = positions.get(broker);
    if (!result2) {
      incomplete.add(broker);
      warnings.push(`${broker}:position_result_missing`);
    } else {
      if (result2.status !== "ok") {
        incomplete.add(broker);
        warnings.push(`${broker}:positions_${result2.status}`);
      }
      if (USABLE.has(result2.status)) included.add(broker);
    }
  }
  if (includesCash) for (const broker of expectedSorted) {
    const result2 = cash.get(broker);
    if (!result2) {
      incomplete.add(broker);
      warnings.push(`${broker}:cash_result_missing`);
    } else {
      if (result2.status !== "ok") {
        incomplete.add(broker);
        warnings.push(`${broker}:cash_${result2.status}`);
      }
      if (USABLE.has(result2.status)) included.add(broker);
    }
  }
  const lots = [];
  for (const result2 of positionResults) {
    if (!USABLE.has(result2.status)) continue;
    for (const position of result2.items) {
      if (position.broker !== result2.broker || !nonempty(position.accountId)) {
        throw new TypeError("invalid position source or account");
      }
      validateInstrument(position.instrument);
      const quantity = decimal(position.quantity);
      if (compare(quantity, zero()) < 0) throw new TypeError("negative position quantity");
      for (const field of ["marketValue", "costBasis", "currentPrice", "averagePrice"]) {
        if (position[field] !== null && position[field] !== void 0 && compare(decimal(position[field]), zero()) < 0) {
          throw new TypeError(`negative ${field}`);
        }
      }
      if (compare(quantity, zero()) > 0) lots.push(position);
    }
  }
  const specific = /* @__PURE__ */ new Map();
  for (const lot of lots) {
    const item = lot.instrument;
    if (!US_MARKETS.has(item.market) || US_GENERIC.has(item.market)) continue;
    const key2 = `${item.ticker}:${item.assetType}:${item.currency}`;
    if (!specific.has(key2)) specific.set(key2, /* @__PURE__ */ new Set());
    specific.get(key2).add(item.market);
  }
  const grouped = /* @__PURE__ */ new Map();
  for (const lot of lots) {
    const item = lot.instrument;
    const choices = specific.get(`${item.ticker}:${item.assetType}:${item.currency}`);
    const market = US_GENERIC.has(item.market) && choices?.size === 1 ? [...choices][0] : item.market;
    const instrument3 = { ...item, market };
    const key2 = instrumentKey(instrument3);
    if (!grouped.has(key2)) grouped.set(key2, { instrument: instrument3, lots: [] });
    grouped.get(key2).lots.push(lot);
  }
  let positionAmount = zero();
  let costAmount = zero();
  let cashAmount = zero();
  let valuationComplete = true;
  let allCostsComplete = true;
  let staleFx = false;
  const aggregated = [];
  for (const key2 of [...grouped.keys()].sort()) {
    const { instrument: instrument3, lots: group } = grouped.get(key2);
    const quantity = group.reduce((sum2, item) => add(sum2, decimal(item.quantity)), zero());
    const marketValues = group.map((item) => effectiveValue(item, "marketValue", "currentPrice"));
    const costs = group.map((item) => effectiveValue(item, "costBasis", "averagePrice"));
    const marketValue = marketValues.some((item) => item === null) ? null : marketValues.reduce(add, zero());
    const cost = costs.some((item) => item === null) ? null : costs.reduce(add, zero());
    if (cost === null) allCostsComplete = false;
    const unrealized = marketValue === null || cost === null ? null : subtract(marketValue, cost);
    const returnPct = unrealized === null || compare(cost, zero()) === 0 ? null : round28(multiply(divide28(unrealized, cost), decimal("100")));
    let marketValueBase = null;
    if (marketValue === null) {
      valuationComplete = false;
      warnings.push(`${key2}:market_value_missing`);
    } else {
      const [converted, stale] = convert(marketValue, instrument3.currency);
      marketValueBase = converted;
      staleFx ||= stale;
      if (converted === null) {
        valuationComplete = false;
        warnings.push(`${key2}:fx_to_${baseCurrency}_missing`);
      } else positionAmount = add(positionAmount, converted);
    }
    let costBasisBase = null;
    if (cost !== null) {
      const [converted, stale] = convert(cost, instrument3.currency);
      costBasisBase = converted;
      staleFx ||= stale;
      if (converted === null) allCostsComplete = false;
      else costAmount = add(costAmount, converted);
    }
    const brokerQuantities = /* @__PURE__ */ new Map();
    for (const lot of group) {
      brokerQuantities.set(lot.broker, add(
        brokerQuantities.get(lot.broker) || zero(),
        decimal(lot.quantity)
      ));
    }
    aggregated.push({
      instrument: instrument3,
      quantity: exact(quantity),
      companyName: group.find((item) => item.companyName)?.companyName || "",
      averagePrice: cost === null ? null : exact(divide28(cost, quantity)),
      currentPrice: marketValue === null ? null : exact(divide28(marketValue, quantity)),
      costBasis: cost === null ? null : exact(cost),
      marketValue: marketValue === null ? null : exact(marketValue),
      unrealizedProfit: unrealized === null ? null : exact(unrealized),
      returnPct: returnPct === null ? null : exact(returnPct),
      costBasisBase: costBasisBase === null ? null : exact(costBasisBase),
      marketValueBase: marketValueBase === null ? null : exact(marketValueBase),
      weightPct: null,
      brokerQuantities: [...brokerQuantities].sort(([a], [b]) => a.localeCompare(b)).map(([broker, value]) => [broker, exact(value)])
    });
  }
  const convertedCash = [];
  if (includesCash) for (const result2 of cashResults) {
    if (!USABLE.has(result2.status)) continue;
    for (const balance of result2.items) {
      if (balance.broker !== result2.broker || !nonempty(balance.accountId) || !["KRW", "USD"].includes(balance.currency)) {
        throw new TypeError("invalid cash source, account or currency");
      }
      const [converted, stale] = convert(decimal(balance.amount), balance.currency);
      staleFx ||= stale;
      if (converted === null) {
        valuationComplete = false;
        warnings.push(`${balance.broker}:cash_fx_${balance.currency}_to_${baseCurrency}_missing`);
      } else cashAmount = add(cashAmount, converted);
      convertedCash.push({
        broker: balance.broker,
        accountId: balance.accountId,
        currency: balance.currency,
        amount: balance.amount,
        amountBase: converted === null ? null : exact(converted)
      });
    }
  }
  if (staleFx) warnings.push("fx_quote_stale");
  const complete = expectedSorted.length > 0 && incomplete.size === 0 && valuationComplete;
  const subtotal = add(positionAmount, cashAmount);
  let cashWeightPct = null;
  if (complete && compare(subtotal, zero()) > 0) {
    for (const position of aggregated) {
      if (position.marketValueBase !== null) {
        position.weightPct = exact(round28(multiply(
          divide28(decimal(position.marketValueBase), subtotal),
          decimal("100")
        )));
      }
    }
    cashWeightPct = exact(round28(multiply(divide28(cashAmount, subtotal), decimal("100"))));
  }
  return {
    baseCurrency,
    expectedBrokers: expectedSorted,
    includedBrokers: [...included].sort(),
    incompleteBrokers: [...incomplete].sort(),
    includesCash,
    isComplete: complete,
    includedPositionValue: exact(positionAmount),
    includedCashValue: exact(cashAmount),
    includedMarketValue: exact(subtotal),
    totalMarketValue: complete ? exact(subtotal) : null,
    includedCostBasis: allCostsComplete ? exact(costAmount) : null,
    totalCostBasis: complete && allCostsComplete ? exact(costAmount) : null,
    cashWeightPct,
    positions: aggregated,
    cashBalances: convertedCash,
    warnings: [...new Set(warnings)]
  };
}

// deploy/cloudflare-js/provider-contracts.mjs
var STATUSES2 = /* @__PURE__ */ new Set([
  "ok",
  "partial",
  "stale",
  "not_configured",
  "auth_failed",
  "rate_limited",
  "unavailable"
]);
function result(source, value) {
  if (!value || value.broker !== source || !STATUSES2.has(value.status) || !Array.isArray(value.items) || value.status === "partial" && !value.safeErrorType && !value.missingAccounts?.length) {
    throw new TypeError("invalid normalized provider result");
  }
  return value;
}
async function safeRead(source, operation) {
  try {
    return result(source, await operation());
  } catch (error) {
    return {
      broker: source,
      status: "unavailable",
      items: [],
      safeErrorType: error instanceof TypeError ? "InvalidProviderResult" : "ProviderUnavailable"
    };
  }
}
async function refreshWithProviders(providers, selectedNames, fxQuotes = []) {
  if (!Array.isArray(selectedNames) || !selectedNames.length || new Set(selectedNames).size !== selectedNames.length) {
    throw new TypeError("explicit unique source scope required");
  }
  const byName = new Map(providers.map((provider) => [provider.name, provider]));
  const rows = await Promise.all(selectedNames.map(async (name) => {
    const provider = byName.get(name);
    if (!provider || !["getPositions", "getCash", "getAccounts"].every(
      (method) => typeof provider[method] === "function"
    )) {
      const missing = { broker: name, status: "not_configured", items: [] };
      return { positions: missing, cash: missing, accounts: missing };
    }
    const [positions, cash, accounts] = await Promise.all([
      safeRead(name, () => provider.getPositions()),
      safeRead(name, () => provider.getCash()),
      safeRead(name, () => provider.getAccounts())
    ]);
    return { positions, cash, accounts };
  }));
  const positionResults = rows.map((row) => row.positions);
  const cashResults = rows.map((row) => row.cash);
  return {
    snapshot: aggregateCoverage({
      baseCurrency: "KRW",
      expectedBrokers: selectedNames,
      fxQuotes,
      positionResults,
      cashResults
    }),
    positionResults,
    cashResults,
    accountResults: rows.map((row) => row.accounts),
    fxQuote: fxQuotes.find((quote) => quote.base === "USD" && quote.quote === "KRW") || null
  };
}
async function readPrice(provider, market, ticker) {
  if (!provider || typeof provider.getPrice !== "function") return null;
  let quote;
  try {
    quote = await provider.getPrice(market, ticker);
  } catch {
    return null;
  }
  try {
    if (!quote || quote.market !== market || quote.ticker !== ticker || !["KRW", "USD"].includes(quote.currency) || typeof quote.asOf !== "string" || typeof quote.provider !== "string" || !quote.provider || typeof quote.stale !== "boolean" || compare(decimal(quote.price), decimal("0")) <= 0) return null;
  } catch {
    return null;
  }
  return quote;
}

// deploy/cloudflare-js/assets-report.mjs
var zero2 = () => decimal("0");
var usable = /* @__PURE__ */ new Set(["ok", "partial", "stale"]);
var koreanMarkets = /* @__PURE__ */ new Set(["KRX", "KOSPI", "KOSDAQ", "NXT"]);
var statusPriority = /* @__PURE__ */ new Map([
  ["ok", 0],
  ["stale", 1],
  ["partial", 2],
  ["not_configured", 3],
  ["rate_limited", 4],
  ["auth_failed", 5],
  ["unavailable", 6]
]);
var statusText = /* @__PURE__ */ new Map([
  ["ok", "\uC815\uC0C1"],
  ["partial", "\uC77C\uBD80\uB9CC \uC870\uD68C"],
  ["stale", "\uB9C8\uC9C0\uB9C9 \uC815\uC0C1\uAC12"],
  ["not_configured", "\uBBF8\uC124\uC815"],
  ["auth_failed", "\uC778\uC99D \uC2E4\uD328"],
  ["rate_limited", "\uD638\uCD9C \uC81C\uD55C"],
  ["unavailable", "\uC870\uD68C \uC2E4\uD328"]
]);
var brokerLabels = /* @__PURE__ */ new Map([
  ["kiwoom", "\uD0A4\uC6C0"],
  ["kiwoom_main", "\uD0A4\uC6C0 \uBA54\uC778"],
  ["kiwoom_accum", "\uD0A4\uC6C0 \uBAA8\uC73C\uAE30"],
  ["toss", "\uD1A0\uC2A4"],
  ["meritz", "\uBA54\uB9AC\uCE20"]
]);
function html(value) {
  return value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#x27;");
}
function money(value, currency2) {
  return currency2 === "KRW" ? `${display(decimal(value), 0)}\uC6D0` : currency2 === "USD" ? `$${display(decimal(value), 2)}` : `${display(decimal(value), 2)} ${html(currency2)}`;
}
function percent(value) {
  if (value === null) return "\uD655\uC778 \uBD88\uAC00";
  return `${compare(value, zero2()) > 0 ? "+" : ""}${display(value, 2)}%`;
}
function localTime(iso) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Seoul",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23"
  }).formatToParts(new Date(iso)).map(({ type, value }) => [type, value]));
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}
function positionValue(row) {
  if (row.marketValue !== null && row.marketValue !== void 0) return decimal(row.marketValue);
  if (row.currentPrice !== null && row.currentPrice !== void 0) {
    return multiply(decimal(row.currentPrice), decimal(row.quantity));
  }
  return null;
}
function brokerTotals(positionResults, cashResults, fxQuote) {
  const totals = /* @__PURE__ */ new Map();
  const valid = /* @__PURE__ */ new Map();
  const convert = (value, currency2) => {
    if (currency2 === "KRW") return value;
    if (currency2 === "USD" && fxQuote?.base === "USD" && fxQuote.quote === "KRW") {
      return multiply(value, decimal(fxQuote.rate));
    }
    return null;
  };
  for (const result2 of [...positionResults, ...cashResults]) {
    if (!totals.has(result2.broker)) totals.set(result2.broker, zero2());
    if (!valid.has(result2.broker)) valid.set(result2.broker, true);
    if (result2.status !== "ok") valid.set(result2.broker, false);
    if (!usable.has(result2.status)) continue;
    for (const row of result2.items) {
      const native = "instrument" in row ? positionValue(row) : decimal(row.amount);
      const currency2 = "instrument" in row ? row.instrument.currency : row.currency;
      const converted = native === null ? null : convert(native, currency2);
      if (converted === null) valid.set(result2.broker, false);
      else totals.set(result2.broker, add(totals.get(result2.broker), converted));
    }
  }
  return new Map([...totals].map(([broker, value]) => [broker, valid.get(broker) ? value : null]));
}
function formatAssets(snapshot, {
  positionResults,
  cashResults = [],
  collectedAt,
  fxQuote = null
}) {
  const total = snapshot.isComplete ? snapshot.totalMarketValue : snapshot.includedMarketValue;
  const scope = snapshot.expectedBrokers?.length === 1 && snapshot.expectedBrokers[0] === "kiwoom_main" ? "\uD0A4\uC6C0 \uBA54\uC778" : "\uC120\uD0DD \uCD9C\uCC98";
  const lines = [
    snapshot.isComplete ? `\u{1F4B0} <b>${scope} \uC804\uCCB4\uC790\uC0B0</b>` : `\u26A0\uFE0F <b>${scope} \uD655\uC778 \uC790\uC0B0 \uBD80\uBD84 \uD569\uACC4</b>`,
    "",
    "\uD3C9\uAC00\uAE08\uC561",
    `<b>${money(total, snapshot.baseCurrency)}</b>`
  ];
  const nativeTotals = /* @__PURE__ */ new Map();
  const accumulate = (currency2, value) => nativeTotals.set(currency2, add(nativeTotals.get(currency2) || zero2(), value));
  for (const result2 of positionResults) if (usable.has(result2.status)) {
    for (const row of result2.items) {
      const value = positionValue(row);
      if (value !== null) accumulate(row.instrument.currency, value);
    }
  }
  for (const result2 of cashResults) if (usable.has(result2.status)) {
    for (const row of result2.items) accumulate(row.currency, decimal(row.amount));
  }
  if (nativeTotals.size) {
    lines.push("", "\uD655\uC778\uB41C \uD604\uC9C0\uD1B5\uD654 \uD569\uACC4");
    for (const currency2 of ["KRW", "USD"]) if (nativeTotals.has(currency2)) {
      lines.push(money(exact(nativeTotals.get(currency2)), currency2));
    }
  }
  const cost = snapshot.isComplete ? snapshot.totalCostBasis : snapshot.includedCostBasis;
  if (cost !== null) {
    const profit = subtract(decimal(total), decimal(cost));
    const costValue = decimal(cost);
    const returnPct = compare(costValue, zero2()) === 0 ? null : round28(multiply(divide28(profit, costValue), decimal("100")));
    lines.push(
      "",
      "\uD22C\uC790\uC6D0\uAE08",
      money(cost, snapshot.baseCurrency),
      "",
      "\uD3C9\uAC00\uC190\uC775",
      money(exact(profit), snapshot.baseCurrency),
      "",
      "\uC218\uC775\uB960",
      percent(returnPct)
    );
  } else lines.push("", "\uD22C\uC790\uC6D0\uAE08\xB7\uC218\uC775\uB960", "\uC77C\uBD80 \uC6D0\uAC00 \uB370\uC774\uD130\uAC00 \uC5C6\uC5B4 \uACC4\uC0B0 \uBCF4\uB958");
  lines.push("", "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501", "", "\u{1F3E6} <b>\uC99D\uAD8C\uC0AC</b>");
  const amounts = brokerTotals(positionResults, cashResults, fxQuote);
  const statuses2 = /* @__PURE__ */ new Map();
  for (const result2 of [...positionResults, ...cashResults]) {
    if (!statuses2.has(result2.broker) || statusPriority.get(result2.status) > statusPriority.get(statuses2.get(result2.broker))) {
      statuses2.set(result2.broker, result2.status);
    }
  }
  for (const broker of snapshot.expectedBrokers) {
    const value = amounts.get(broker);
    const status = statuses2.get(broker) || "unavailable";
    lines.push("", html(brokerLabels.get(broker) || broker), value === null || value === void 0 ? `${statusText.get(status)} \u2014 \uCD1D\uC561\uC5D0\uC11C 0\uC6D0 \uCC98\uB9AC\uD558\uC9C0 \uC54A\uC74C` : money(exact(value), "KRW"));
  }
  if (snapshot.isComplete && compare(decimal(total), zero2()) > 0) {
    let korea = zero2();
    let us = zero2();
    for (const position of snapshot.positions) {
      const value = decimal(position.weightPct);
      if (koreanMarkets.has(position.instrument.market)) korea = add(korea, value);
      else us = add(us, value);
    }
    lines.push(
      "",
      "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501",
      "",
      "\u{1F30E} <b>\uC9C0\uC5ED</b>",
      "",
      `\uBBF8\uAD6D\uC8FC\uC2DD ${percent(us)}`,
      `\uD55C\uAD6D\uC8FC\uC2DD ${percent(korea)}`,
      `\uD604\uAE08 ${percent(decimal(snapshot.cashWeightPct))}`
    );
  } else lines.push("", "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501", "", "\u{1F30E} \uC9C0\uC5ED \uBE44\uC911", "\uBD88\uC644\uC804\uD55C \uBD84\uBAA8\uC774\uBBC0\uB85C \uACC4\uC0B0 \uBCF4\uB958");
  if (fxQuote !== null) lines.push(
    "",
    `USD/KRW ${display(decimal(fxQuote.rate), 2)}` + (fxQuote.isStale ? " \xB7 \uB9C8\uC9C0\uB9C9 \uC815\uC0C1\uAC12" : "")
  );
  lines.push("", html(`\uAE30\uC900 ${localTime(collectedAt)} KST \xB7 ` + (snapshot.isComplete ? "\uC804\uCCB4 \uC870\uD68C" : "\uBD88\uC644\uC804 \uC870\uD68C")));
  return lines.join("\n");
}

// deploy/cloudflare-js/analysis-report.mjs
var score = (value) => value == null ? "\uBCF4\uB958" : display(decimal(value), 2).replaceAll(",", "");
function fixed(value, places) {
  const parsed = decimal(value);
  const shift = parsed.scale - places;
  let rounded2 = parsed.coefficient;
  if (shift > 0) {
    const divisor = 10n ** BigInt(shift);
    const negative2 = rounded2 < 0n;
    const absolute = negative2 ? -rounded2 : rounded2;
    let quotient = absolute / divisor;
    const remainder = absolute % divisor;
    if (remainder * 2n > divisor || remainder * 2n === divisor && quotient % 2n === 1n) {
      quotient += 1n;
    }
    rounded2 = negative2 ? -quotient : quotient;
  } else if (shift < 0) rounded2 *= 10n ** BigInt(-shift);
  const negative = rounded2 < 0n ? "-" : "";
  const digits = (rounded2 < 0n ? -rounded2 : rounded2).toString().padStart(places + 1, "0");
  const whole = digits.slice(0, digits.length - places).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return negative + whole + (places ? `.${digits.slice(-places)}` : "");
}
var ratioPct = (value) => percent(multiply(decimal(value), decimal("100")));
var decisionLabels = /* @__PURE__ */ new Map([
  ["aggressive_buy", "\u{1F7E2} \uC801\uADF9 \uB9E4\uC218"],
  ["continue_buy", "\u{1F7E2} \uACC4\uC18D \uB9E4\uC218"],
  ["maintain", "\u{1F7E1} \uD604\uC7AC \uC218\uC900 \uC720\uC9C0"],
  ["reduce", "\u{1F7E0} \uB9E4\uC218 \uCD95\uC18C"],
  ["review_stop", "\u{1F534} \uB9E4\uC218 \uC911\uB2E8 \uAC80\uD1A0"],
  ["review_required", "\u{1F534} \uC911\uC694 \uACBD\uACE0 \uAC80\uD1A0 \uD544\uC694"],
  ["unavailable", "\u26AA \uD310\uB2E8 \uBCF4\uB958"]
]);
function formatAnalysis(result2) {
  if (!result2?.companyScore?.components || result2.companyScore.components.length < 5 || !result2.portfolioScore || !result2.finalScore || !decisionLabels.has(result2.finalScore.decision)) {
    throw new TypeError("incomplete normalized analysis");
  }
  const lines = [`\u{1F50D} <b>${html(result2.market)}:${html(result2.ticker)} \uD22C\uC790\uBD84\uC11D</b>`];
  if (result2.price) lines.push(
    "",
    "\uD604\uC7AC\uAC00",
    money(result2.price.price, result2.price.currency) + (result2.price.stale ? " \xB7 \uB9C8\uC9C0\uB9C9 \uC815\uC0C1\uAC12" : "")
  );
  else lines.push("", "\uD604\uC7AC\uAC00", "\uD655\uC778 \uBD88\uAC00");
  lines.push(
    "",
    "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501",
    "",
    "\u{1F4C8} <b>Fundamental</b>",
    `${score(result2.companyScore.components[0].score)} / 100`
  );
  if (result2.fundamentals) {
    const f = result2.fundamentals;
    if (f.revenueGrowth != null) lines.push(`Revenue growth ${ratioPct(f.revenueGrowth)}`);
    if (f.operatingMargin != null) lines.push(`Operating margin ${ratioPct(f.operatingMargin)}`);
    if (f.freeCashFlow != null) lines.push(`FCF ${fixed(f.freeCashFlow, 0)}`);
  } else lines.push("\uC815\uB7C9 \uB370\uC774\uD130 \uBD80\uC871");
  lines.push("", "\u{1F4D1} <b>\uCD5C\uADFC \uC2E4\uC801</b>");
  if (result2.earnings) {
    const e = result2.earnings;
    lines.push(html(e.period || "\uCD5C\uADFC \uD655\uC778 \uBD84\uAE30"));
    if (e.revenue != null) lines.push(`Revenue ${fixed(e.revenue, 0)} (provider \uD45C\uC2DC \uB2E8\uC704)`);
    if (e.revenueYoy != null) lines.push(`Revenue YoY ${ratioPct(e.revenueYoy)}`);
    if (e.eps != null) lines.push(`EPS ${fixed(e.eps, 2)}`);
    if (e.epsYoy != null) lines.push(`EPS YoY ${ratioPct(e.epsYoy)}`);
    if (e.grossMargin != null) lines.push(`Gross margin ${ratioPct(e.grossMargin)}`);
    if (e.operatingMargin != null) lines.push(`Operating margin ${ratioPct(e.operatingMargin)}`);
    if (e.guidance) lines.push("Guidance " + html(e.guidance.slice(0, 240)));
  } else lines.push("\uD655\uC778 \uAC00\uB2A5\uD55C \uBD84\uAE30 \uC2E4\uC801 \uB370\uC774\uD130 \uC5C6\uC74C");
  lines.push(
    "",
    "\u{1F4B0} <b>Valuation</b>",
    `${score(result2.companyScore.components[1].score)} / 100`
  );
  if (result2.valuation) {
    const v = result2.valuation;
    if (v.closePrice != null) {
      const date = new Intl.DateTimeFormat("en-CA", {
        timeZone: "Asia/Seoul",
        year: "numeric",
        month: "2-digit",
        day: "2-digit"
      }).format(new Date(v.asOf));
      lines.push(`\uC8FC\uAC04 \uAE30\uC900\uC885\uAC00 ${fixed(v.closePrice, 2)} \xB7 ${date}`);
    }
    if (v.forwardPe != null) lines.push(`Forward PER ${fixed(v.forwardPe, 2)}x`);
    if (v.historicalForwardPeMedian != null) {
      lines.push(`\uACFC\uAC70 Median ${fixed(v.historicalForwardPeMedian, 2)}x`);
    } else lines.push("\uACFC\uAC70 5Y median \uBBF8\uC81C\uACF5 \u2014 \uC808\uB300 PER\uB9CC\uC73C\uB85C \uB2E8\uC815\uD558\uC9C0 \uC54A\uC74C");
    if (v.peg != null) lines.push(`PEG ${fixed(v.peg, 2)}`);
    if (v.enterpriseToEbitda != null) lines.push(`EV/EBITDA ${fixed(v.enterpriseToEbitda, 2)}x`);
    if (v.freeCashFlowYield != null) {
      lines.push(`FCF Yield ${fixed(exact(multiply(
        decimal(v.freeCashFlowYield),
        decimal("100")
      )), 2)}%`);
    }
    if (v.dilutionPct != null) {
      lines.push(`\uC8FC\uC2DD\uC218 \uBCC0\uD654 ${compare(decimal(v.dilutionPct), decimal("0")) >= 0 ? "+" : ""}${fixed(v.dilutionPct, 2)}%`);
    }
  } else lines.push("\uC815\uB7C9 \uB370\uC774\uD130 \uBD80\uC871");
  lines.push(
    "",
    "\u{1F680} <b>Growth</b>",
    `${score(result2.companyScore.components[2].score)} / 100`,
    "",
    "\u{1F3F0} <b>Moat</b>",
    "\uAC80\uC99D \uAC00\uB2A5\uD55C \uACBD\uC7C1\uC6B0\uC704 \uADFC\uAC70 \uBBF8\uC785\uB825 \uC2DC \uC810\uC218 \uBCF4\uB958",
    "",
    "\u26A0\uFE0F <b>Safety</b>",
    `${score(result2.companyScore.components[4].score)} / 100`,
    "",
    "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501",
    "",
    `\uAE30\uC5C5\uC810\uC218 ${score(result2.companyScore.score)} / 100`,
    `Portfolio \uC810\uC218 ${score(result2.portfolioScore.score)} / 100`,
    `\uCD5C\uC885 ${score(result2.finalScore.score)} / 100`,
    `\uC7A5\uAE30\uCD94\uC138 \uC801\uD569\uC131 ${score(result2.recurringSuitability.score)} / 100`,
    "",
    `<b>${decisionLabels.get(result2.finalScore.decision)}</b>`,
    "",
    "\uC774\uC720"
  );
  (result2.reasons || []).forEach((reason, index) => lines.push(`${index + 1}. ${html(reason)}`));
  const news = (result2.news || []).filter((item) => item.fundamentalImpact === "REVIEW_REQUIRED");
  if (news.length) {
    lines.push("", "\u{1F4F0} <b>Fundamental \uC601\uD5A5 \uD655\uC778 \uD544\uC694 \uB274\uC2A4</b>");
    for (const item of news.slice(0, 3)) {
      const title = html(item.title);
      lines.push(item.url.startsWith("https://") ? `\u2022 <a href="${html(item.url)}">${title}</a>` : `\u2022 ${title}`);
    }
    lines.push("\uB274\uC2A4 \uC790\uCCB4\uB294 \uC810\uC218\uC5D0 \uC9C1\uC811 \uBC18\uC601\uD558\uC9C0 \uC54A\uC558\uC2B5\uB2C8\uB2E4.");
  }
  if (result2.warnings?.length) lines.push("", "\uB370\uC774\uD130 \uACBD\uACE0: " + html(result2.warnings.join(", ")));
  return lines.join("\n");
}

// deploy/cloudflare-js/scoring.mjs
var add2 = (left, right) => round28(add(left, right));
var subtract2 = (left, right) => round28(subtract(left, right));
var multiply2 = (left, right) => round28(multiply(left, right));
var z = () => decimal("0");
var hundred = () => decimal("100");
var d = (value) => value === null || value === void 0 ? null : decimal(value);
var rounded = (value) => decimal(display(value, 2).replaceAll(",", ""));
var sum = (values) => values.reduce((total, value) => add2(total, value), z());
var clamp = (value) => compare(value, z()) < 0 ? z() : compare(value, hundred()) > 0 ? hundred() : value;
var linear = (value, low, high) => value === null || value === void 0 ? null : clamp(multiply2(divide28(subtract2(d(value), d(low)), subtract2(d(high), d(low))), hundred()));
var mean = (values) => {
  const available = values.filter((value) => value !== null);
  return available.length ? divide28(sum(available), decimal(String(available.length))) : null;
};
var invert = (value) => value === null ? null : subtract2(hundred(), value);
var nullable = (value) => value === null ? null : exact(value);
function fundamentalComponent(data) {
  if (data === null) return null;
  let debt = null;
  if (data.totalCash != null && data.totalDebt != null) {
    debt = compare(d(data.totalDebt), z()) <= 0 ? hundred() : clamp(multiply2(divide28(d(data.totalCash), d(data.totalDebt)), decimal("50")));
  }
  return nullable(mean([
    linear(data.revenueGrowth, "-0.10", "0.30"),
    linear(data.earningsGrowth, "-0.20", "0.40"),
    linear(data.grossMargin, "0.10", "0.70"),
    linear(data.operatingMargin, "0", "0.35"),
    linear(data.returnOnEquity, "0", "0.30"),
    data.freeCashFlow == null ? null : compare(d(data.freeCashFlow), z()) > 0 ? d("80") : d("20"),
    debt
  ]));
}
function growthComponent(data) {
  if (data === null) return null;
  return nullable(mean([
    linear(data.revenueGrowth, "-0.05", "0.35"),
    linear(data.earningsGrowth, "-0.10", "0.50")
  ]));
}
function valuationComponent(data) {
  if (data === null) return null;
  const metrics = [];
  if (data.forwardPe != null) metrics.push(invert(linear(data.forwardPe, "10", "60")));
  if (data.peg != null) metrics.push(invert(linear(data.peg, "0.5", "3")));
  if (data.enterpriseToEbitda != null) {
    metrics.push(invert(linear(data.enterpriseToEbitda, "5", "35")));
  }
  if (data.freeCashFlowYield != null) {
    metrics.push(linear(data.freeCashFlowYield, "0", "0.08"));
  }
  if (data.forwardPe != null && data.historicalForwardPeMedian != null && compare(d(data.historicalForwardPeMedian), z()) !== 0) {
    metrics.push(invert(linear(nullable(divide28(
      d(data.forwardPe),
      d(data.historicalForwardPeMedian)
    )), "0.7", "1.4")));
  }
  if (data.dilutionPct != null) metrics.push(invert(linear(data.dilutionPct, "0", "10")));
  return nullable(mean(metrics));
}
function safetyComponent(data) {
  if (data === null) return null;
  const metrics = [];
  if (data.freeCashFlow != null) metrics.push(compare(d(data.freeCashFlow), z()) > 0 ? d("85") : d("15"));
  if (data.totalCash != null && data.totalDebt != null) {
    metrics.push(compare(d(data.totalDebt), z()) <= 0 ? hundred() : clamp(multiply2(divide28(d(data.totalCash), d(data.totalDebt)), d("60"))));
  }
  if (data.debtToEquity != null) metrics.push(invert(linear(data.debtToEquity, "20", "250")));
  return nullable(mean(metrics));
}
var companyWeights = [
  ["fundamental", "35"],
  ["valuation", "25"],
  ["growth", "20"],
  ["moat", "10"],
  ["safety", "10"]
];
var portfolioWeights = [
  ["target_alignment", "50"],
  ["single_stock_capacity", "20"],
  ["sector_capacity", "15"],
  ["currency_capacity", "10"],
  ["correlation", "5"]
];
function weightedScore(values, weights, extraWarnings = []) {
  const components = weights.map(([name, weight]) => ({
    name,
    score: values[name] ?? null,
    weight,
    reasons: []
  }));
  for (const component of components) if (component.score !== null && (compare(d(component.score), z()) < 0 || compare(d(component.score), hundred()) > 0)) {
    throw new TypeError("score outside 0..100");
  }
  const covered = components.filter((component) => component.score !== null);
  const coveredWeight = sum(covered.map((component) => d(component.weight)));
  const coverage = rounded(coveredWeight);
  const warnings = [...extraWarnings];
  if (compare(coverage, d("70")) < 0) {
    warnings.push(`score_withheld:coverage_${display(coverage, 2)}_below_70.00`);
    return {
      score: null,
      coveragePct: exact(coverage),
      isEligible: false,
      components,
      warnings
    };
  }
  const weightedSum = sum(covered.map((component) => multiply2(d(component.score), d(component.weight))));
  const score2 = rounded(divide28(weightedSum, coveredWeight));
  if (compare(coveredWeight, hundred()) < 0) {
    warnings.push("score_renormalized_over_available_components");
  }
  return {
    score: exact(score2),
    coveragePct: exact(coverage),
    isEligible: true,
    components,
    warnings
  };
}
function companyScore(values) {
  return weightedScore(values, companyWeights);
}
function portfolioScore(values) {
  const { complete, ...parts } = values;
  const result2 = weightedScore(
    parts,
    portfolioWeights,
    complete ? [] : ["score_withheld:portfolio_snapshot_incomplete"]
  );
  return complete ? result2 : { ...result2, score: null, isEligible: false };
}
var warningTypes = /* @__PURE__ */ new Set([
  "accounting_issue",
  "liquidity_crisis",
  "going_concern",
  "fraud",
  "major_customer_loss",
  "structural_business_deterioration",
  "investment_thesis_breakdown"
]);
var decision = (value) => compare(value, d("85")) >= 0 ? "aggressive_buy" : compare(value, d("70")) >= 0 ? "continue_buy" : compare(value, d("55")) >= 0 ? "maintain" : compare(value, d("40")) >= 0 ? "reduce" : "review_stop";
function valuationDecision(score2, hardWarnings = []) {
  const warnings = [...new Set(hardWarnings)];
  if (warnings.some((item) => !warningTypes.has(item))) throw new TypeError("unsupported hard warning");
  if (score2 !== null && (compare(d(score2), z()) < 0 || compare(d(score2), hundred()) > 0)) {
    throw new TypeError("valuation score outside 0..100");
  }
  if (score2 === null) return {
    score: null,
    companyScore: null,
    portfolioScore: null,
    decision: warnings.length ? "review_required" : "unavailable",
    isActionable: false,
    hardWarnings: warnings,
    warnings: ["valuation_decision_withheld:data_unavailable"]
  };
  const value = exact(rounded(d(score2)));
  return {
    score: value,
    companyScore: value,
    portfolioScore: null,
    decision: warnings.length ? "review_required" : decision(d(value)),
    isActionable: !warnings.length,
    hardWarnings: warnings,
    warnings: warnings.length ? ["valuation_only_decision", "hard_warning_override"] : ["valuation_only_decision"]
  };
}
function buyCapacityScore(currentWeight, targetWeight) {
  const current = d(currentWeight);
  const target = d(targetWeight);
  if (current === null || target === null || compare(current, z()) < 0 || compare(current, hundred()) > 0 || compare(target, z()) < 0 || compare(target, hundred()) > 0) throw new TypeError("invalid exposure");
  if (compare(target, z()) === 0) return "0";
  if (compare(current, target) <= 0) return "100";
  const overshoot = divide28(subtract2(current, target), target);
  return exact(rounded(clamp(multiply2(subtract2(d("1"), overshoot), hundred()))));
}
function recurringSuitability(trend) {
  if (trend === null) return { score: null, reasons: ["\uC7A5\uAE30 \uAC00\uACA9 \uC774\uB825 \uD655\uC778 \uBD88\uAC00"] };
  const metrics = [];
  if (trend.cagr3y != null) metrics.push(linear(trend.cagr3y, "-0.05", "0.15"));
  if (trend.return1y != null) metrics.push(linear(trend.return1y, "-0.20", "0.20"));
  if (trend.maxDrawdown3y != null) {
    metrics.push(linear(trend.maxDrawdown3y, "-0.60", "-0.15"));
  }
  if (trend.annualizedVolatility1y != null) {
    metrics.push(invert(linear(trend.annualizedVolatility1y, "0.15", "0.50")));
  }
  if (trend.above200dAverage != null) {
    metrics.push(trend.above200dAverage ? hundred() : d("30"));
  }
  if (metrics.length < 3) return { score: null, reasons: ["\uC7A5\uAE30\uCD94\uC138 \uC9C0\uD45C \uCEE4\uBC84\uB9AC\uC9C0 \uBD80\uC871"] };
  const reasons = [];
  const signed = (value) => `${compare(d(value), z()) >= 0 ? "+" : ""}${display(d(value), 2)}%`;
  if (trend.cagr3y != null) reasons.push(`3\uB144 CAGR ${signed(nullable(multiply2(d(trend.cagr3y), hundred())))}`);
  if (trend.maxDrawdown3y != null) {
    reasons.push(`\uCD5C\uB300\uB099\uD3ED ${display(multiply2(d(trend.maxDrawdown3y), hundred()), 2)}%`);
  }
  reasons.push(trend.above200dAverage ? "200\uC77C\uC120 \uC0C1\uD68C" : "200\uC77C\uC120 \uD558\uD68C");
  return { score: exact(rounded(mean(metrics))), reasons };
}

// deploy/cloudflare-js/analysis-service.mjs
var US = /* @__PURE__ */ new Set(["US", "USA", "NASDAQ", "NAS", "NYSE", "AMEX", "ARCA", "NYSEARCA"]);
var KEYWORDS = /* @__PURE__ */ new Map([
  ["ACCOUNTING", ["accounting", "restatement", "auditor", "\uD68C\uACC4", "\uAC10\uC0AC \uC758\uACAC"]],
  ["LIQUIDITY", ["bankruptcy", "default", "liquidity crisis", "\uD30C\uC0B0", "\uCC44\uBB34\uBD88\uC774\uD589", "\uC720\uB3D9\uC131"]],
  ["CUSTOMER", ["contract cancelled", "customer loss", "\uACC4\uC57D \uD574\uC9C0", "\uACE0\uAC1D\uC0AC \uC774\uD0C8"]],
  ["GUIDANCE", ["guidance", "forecast cut", "forecast raised", "\uAC00\uC774\uB358\uC2A4", "\uC804\uB9DD \uD558\uD5A5", "\uC804\uB9DD \uC0C1\uD5A5"]],
  ["EARNINGS", ["earnings", "revenue", "profit warning", "\uC2E4\uC801", "\uB9E4\uCD9C", "\uC774\uC775 \uACBD\uACE0"]],
  ["REGULATORY", ["investigation", "antitrust", "sanction", "\uC870\uC0AC", "\uB3C5\uC810", "\uC81C\uC7AC"]],
  ["CAPITAL", ["share offering", "debt offering", "capital raise", "\uC720\uC0C1\uC99D\uC790", "\uD68C\uC0AC\uCC44", "\uC790\uBCF8 \uC870\uB2EC"]]
]);
function compatible(left, market, ticker) {
  if (left.ticker !== ticker) return false;
  return left.market === market || ["US", "USA"].includes(left.market) && US.has(market) || ["US", "USA"].includes(market) && US.has(left.market);
}
function assessNews(items) {
  return items.map((item) => {
    const words = `${item.title} ${item.summary || ""}`.toLocaleLowerCase();
    const hits = [...KEYWORDS.keys()].filter((category) => KEYWORDS.get(category).some((keyword) => words.includes(keyword.toLocaleLowerCase())));
    return {
      ...item,
      fundamentalImpact: hits.length ? "REVIEW_REQUIRED" : "NO_DIRECT_EVIDENCE",
      impactReasons: hits.length ? hits.slice(0, 3).map((category) => `${category} \uAD00\uB828 \uC0AC\uAC74 \uAC00\uB2A5\uC131\u2014\uC6D0\uBB38\uACFC 1\uCC28 \uC790\uB8CC \uD655\uC778 \uD544\uC694`) : ["\uC815\uB7C9 \uD380\uB354\uBA58\uD138 \uC601\uD5A5 \uADFC\uAC70\uAC00 \uC81C\uBAA9/\uC694\uC57D\uC5D0\uC11C \uD655\uC778\uB418\uC9C0 \uC54A\uC74C"]
    };
  });
}
function analyzeNormalized({
  market,
  ticker,
  snapshot,
  targetWeightPct = null,
  price = null,
  fundamentals = null,
  valuation = null,
  earnings = null,
  news = [],
  trend = null,
  errors = {},
  hardWarnings = []
}) {
  if (!snapshot || !Array.isArray(snapshot.positions) || !Array.isArray(news) || typeof market !== "string" || typeof ticker !== "string") {
    throw new TypeError("invalid normalized analysis input");
  }
  const valuationValue = valuationComponent(valuation);
  const company = companyScore({
    fundamental: fundamentalComponent(fundamentals),
    valuation: valuationValue,
    growth: growthComponent(fundamentals),
    moat: null,
    safety: safetyComponent(fundamentals)
  });
  const match2 = snapshot.positions.find((position) => compatible(position.instrument, market, ticker));
  const weightsAvailable = snapshot.isComplete && snapshot.totalMarketValue !== null && compare(decimal(snapshot.totalMarketValue), decimal("0")) > 0;
  const currentWeight = match2 ? match2.weightPct : weightsAvailable ? "0" : null;
  const targetAlignment = currentWeight !== null && targetWeightPct !== null ? buyCapacityScore(currentWeight, targetWeightPct) : null;
  const limit = targetWeightPct !== null && compare(decimal(targetWeightPct), decimal("10")) > 0 ? targetWeightPct : "10";
  const singleCapacity = currentWeight !== null ? buyCapacityScore(currentWeight, limit) : null;
  let currencyExposure = null;
  if (weightsAvailable && match2) {
    currencyExposure = exact(snapshot.positions.filter((position) => position.instrument.currency === match2.instrument.currency).reduce((total, position) => add(total, decimal(position.weightPct || "0")), decimal("0")));
  }
  const currencyCapacity = currencyExposure === null ? null : buyCapacityScore(currencyExposure, "80");
  const portfolio = portfolioScore({
    complete: snapshot.isComplete,
    target_alignment: targetAlignment,
    single_stock_capacity: singleCapacity,
    sector_capacity: null,
    currency_capacity: currencyCapacity,
    correlation: null
  });
  const final = valuationDecision(valuationValue, hardWarnings);
  const reasons = [];
  if (fundamentals?.revenueGrowth != null) reasons.push(
    `\uB9E4\uCD9C \uC131\uC7A5\uB960 ${fixed(exact(multiply(decimal(fundamentals.revenueGrowth), decimal("100"))), 1)}%(${fundamentals.provider}, \uAE30\uC900 ${fundamentals.asOf.slice(0, 10)})`
  );
  if (valuation?.forwardPe != null) reasons.push(
    `Forward PER ${fixed(valuation.forwardPe, 2)}\uBC30\uB97C \uC131\uC7A5\xB7\uD604\uAE08\uD750\uB984 \uC9C0\uD45C\uC640 \uD568\uAED8 \uD3C9\uAC00\uD568`
  );
  if (currentWeight !== null && targetWeightPct !== null) reasons.push(
    `\uD604\uC7AC \uBE44\uC911 ${fixed(currentWeight, 2)}%\uAC00 \uBAA9\uD45C ${fixed(targetWeightPct, 2)}% ` + (compare(decimal(currentWeight), decimal(targetWeightPct)) <= 0 ? "\uC774\uD558" : "\uCD08\uACFC")
  );
  if (!reasons.length) reasons.push("\uD310\uB2E8\uC5D0 \uD544\uC694\uD55C \uC815\uB7C9 \uB370\uC774\uD130\uAC00 \uCDA9\uBD84\uD558\uC9C0 \uC54A\uC74C");
  if (reasons.length < 3) reasons.push(
    `\uAE30\uC5C5 \uC810\uC218 ${company.score === null ? "\uCEE4\uBC84\uB9AC\uC9C0 \uBBF8\uB2EC\uB85C \uBCF4\uB958" : `${fixed(company.score, 2)}\uC810`}(\uC815\uB7C9 \uB370\uC774\uD130 \uCEE4\uBC84\uB9AC\uC9C0 ${fixed(company.coveragePct, 2)}%)`
  );
  if (reasons.length < 3) reasons.push(
    `\uD3EC\uD2B8\uD3F4\uB9AC\uC624 \uC810\uC218 ${portfolio.score === null ? "\uBD88\uC644\uC804 \uB370\uC774\uD130\uB85C \uBCF4\uB958" : `${fixed(portfolio.score, 2)}\uC810`}(\uC2A4\uB0C5\uC0F7 ${snapshot.isComplete ? "\uC644\uC804" : "\uBD80\uBD84"})`
  );
  const warnings = ["price", "fundamental", "valuation", "earnings", "news", "trend"].filter((name) => errors[name]).map((name) => `${name}:${errors[name]}`);
  return {
    market: market.toUpperCase(),
    ticker: ticker.toUpperCase(),
    price,
    fundamentals,
    valuation,
    earnings,
    news: assessNews(news),
    trend,
    recurringSuitability: recurringSuitability(trend),
    companyScore: company,
    portfolioScore: portfolio,
    finalScore: final,
    reasons,
    warnings
  };
}
async function analyzeWithProvider(provider, options) {
  const calls = [
    ["price", "getPrice"],
    ["fundamental", "getFundamentals"],
    ["valuation", "getValuation"],
    ["earnings", "getEarnings"],
    ["news", "getNews"],
    ["trend", "getTrend"]
  ];
  const entries = await Promise.all(calls.map(async ([key2, method]) => {
    if (typeof provider?.[method] !== "function") return [key2, null, "NotSupported"];
    try {
      const value = await provider[method](
        options.market,
        options.ticker,
        ...key2 === "news" ? [10] : []
      );
      return [key2, value, null];
    } catch (error) {
      return [key2, null, error?.name || "ProviderUnavailable"];
    }
  }));
  const rows = Object.fromEntries(entries.map(([key2, value]) => [key2, value]));
  const errors = Object.fromEntries(entries.filter(([, , error]) => error).map(([key2, , error]) => [key2, error]));
  return analyzeNormalized({
    ...options,
    price: rows.price,
    fundamentals: rows.fundamental,
    valuation: rows.valuation,
    earnings: rows.earnings,
    news: rows.news || [],
    trend: rows.trend,
    errors
  });
}

// deploy/cloudflare-js/cloud-gateway.mjs
var KR = /* @__PURE__ */ new Set(["KOSPI", "KOSDAQ", "KRX", "NXT"]);
var US2 = /* @__PURE__ */ new Set(["US", "USA", "NASDAQ", "NAS", "NYSE", "AMEX", "ARCA", "NYSEARCA"]);
var ACCOUNT_SCOPE = /^[a-z][a-z0-9_]{0,30}_[0-9a-f]{64}$/;
var stable = (plan) => `${plan.instrument.market}:${plan.instrument.ticker}:${plan.instrument.assetType}:${plan.instrument.currency}`;
var match = (left, market, ticker) => left.ticker === ticker && (left.market === market || ["US", "USA"].includes(left.market) && US2.has(market) || ["US", "USA"].includes(market) && US2.has(left.market));
function toDomain(row) {
  const memo = row.memo || "";
  const scheduledWeekdays = memo.includes("[schedule=MON]") ? [0] : memo.includes("[schedule=FRI]") ? [4] : [0, 1, 2, 3, 4];
  const broker = memo.includes("[source=USER_CANDIDATE]") ? "candidate" : row.source === "TELEGRAM" ? "kiwoom_accum" : row.source === "MERITZ_SNAPSHOT" ? "meritz" : row.source.toLowerCase();
  return {
    ...row,
    currency: row.amountCurrency,
    broker,
    scheduledWeekdays,
    targetWeightPct: row.targetWeight === null ? null : exact(multiply(decimal(row.targetWeight), decimal("100"))),
    investmentThesis: ""
  };
}
var SyntheticCloudGateway = class {
  constructor({
    db,
    providers,
    marketProvider = null,
    externalRecurringPlans,
    scopeAccountFingerprint,
    meritzSnapshotFingerprint = null,
    now = () => (/* @__PURE__ */ new Date()).toISOString(),
    timezoneName = "Asia/Seoul"
  }) {
    if (!db || !Array.isArray(providers) || typeof externalRecurringPlans !== "function" || !ACCOUNT_SCOPE.test(scopeAccountFingerprint) || meritzSnapshotFingerprint !== null && (!ACCOUNT_SCOPE.test(meritzSnapshotFingerprint) || meritzSnapshotFingerprint === scopeAccountFingerprint)) {
      throw new TypeError("explicit D1, source and account scope required");
    }
    this.db = db;
    this.providers = providers;
    this.marketProvider = marketProvider;
    this.analysisProvider = marketProvider;
    this.externalRecurringPlans = externalRecurringPlans;
    this.scopeAccountFingerprint = scopeAccountFingerprint;
    this.meritzSnapshotFingerprint = meritzSnapshotFingerprint;
    this.now = now;
    this.timezoneName = timezoneName;
    this.lastSnapshot = null;
  }
  async refresh({ allBrokers }) {
    const names = allBrokers ? this.providers.map((provider) => provider.name) : ["kiwoom_main"];
    const refresh = await refreshWithProviders(this.providers, names);
    this.lastSnapshot = refresh.snapshot;
    return { ...refresh, collectedAt: this.now() };
  }
  async listPlans(enabledOnly) {
    const local = (await listCurrentPlans(this.db, enabledOnly)).map(toDomain);
    for (const plan of local) {
      const thesis = await currentThesis(
        this.db,
        plan.instrument.market,
        plan.instrument.ticker
      );
      plan.investmentThesis = thesis?.thesis_text || "";
    }
    const external = await this.externalRecurringPlans(enabledOnly);
    if (!Array.isArray(external)) throw new TypeError("external source scope unavailable");
    const merged = /* @__PURE__ */ new Map();
    for (const plan of external) {
      if (!plan.broker || !plan.instrument || !plan.currency || typeof plan.enabled !== "boolean" || compare(decimal(plan.dailyAmount), decimal("0")) <= 0) {
        throw new TypeError("invalid external recurring source");
      }
      if (!enabledOnly || plan.enabled) merged.set(`${stable(plan)}:${plan.broker}`, plan);
    }
    for (const plan of local) merged.set(`${stable(plan)}:${plan.broker}`, plan);
    return [...merged.values()].sort((a, b) => a.broker.localeCompare(b.broker) || stable(a).localeCompare(stable(b)));
  }
  async createPlan({
    market,
    ticker,
    amount: amount2,
    currency: currency2,
    targetWeightPct,
    memo,
    mutationKey,
    delivery = null
  }) {
    const planKey = `manual:${market}:${ticker}`;
    const existing = (await listCurrentPlans(this.db)).find((row) => row.planKey === planKey);
    if (existing && existing.accountFingerprint !== this.scopeAccountFingerprint) {
      throw new TypeError("plan belongs to a different account scope");
    }
    const start = existing?.startedAt || this.now();
    const state = {
      instrument: existing?.instrument || {
        market,
        ticker,
        assetType: "EQUITY",
        currency: currency2
      },
      accountFingerprint: existing?.accountFingerprint ?? this.scopeAccountFingerprint,
      source: "TELEGRAM",
      amountCurrency: currency2,
      dailyAmount: amount2,
      targetWeight: exact(divide28(decimal(targetWeightPct), decimal("100"))),
      enabled: true,
      timezoneName: this.timezoneName,
      startedAt: start,
      memo: memo || null
    };
    const staged = delivery && { ...delivery, reply: delivery.render(toDomain(state)) };
    const outcome = existing ? await applyPlanChange(
      this.db,
      planKey,
      existing.version,
      mutationKey,
      state,
      this.now(),
      staged
    ) : await createPlanOnce(this.db, planKey, mutationKey, state, this.now(), staged);
    if (!["CHANGED", "UNCHANGED", "CREATED", "REPLAY"].includes(outcome)) {
      throw new Error("plan changed concurrently");
    }
    if (outcome === "UNCHANGED" && staged) await enqueueUpdate(
      this.db,
      staged.updateId,
      staged.destination,
      JSON.stringify({ text: staged.reply }),
      staged.now
    );
    const current = (await listCurrentPlans(this.db)).find((row) => row.planKey === planKey);
    return toDomain(current);
  }
  async changeGroup({ assetType, delta, stop, excluded, mutationKey, delivery = null }) {
    const editable = (plan) => plan.accountFingerprint === this.scopeAccountFingerprint || this.meritzSnapshotFingerprint !== null && plan.accountFingerprint === this.meritzSnapshotFingerprint && plan.source === "MERITZ_SNAPSHOT";
    const replayKeys = await groupMutationPlans(this.db, mutationKey);
    if (replayKeys.length) {
      const keys2 = new Set(replayKeys);
      return (await listCurrentPlans(this.db)).filter((plan) => keys2.has(plan.planKey) && editable(plan)).map(toDomain);
    }
    const current = (await listCurrentPlans(this.db, true)).filter((plan) => editable(plan) && plan.instrument.assetType === assetType && !excluded.includes(plan.instrument.ticker));
    const changes2 = current.map((plan) => {
      let dailyAmount = plan.dailyAmount;
      if (!stop) {
        const minimum = decimal(plan.amountCurrency === "KRW" ? plan.source === "MERITZ_SNAPSHOT" ? "1000" : "2000" : "1");
        const candidate = add(decimal(plan.dailyAmount), decimal(delta));
        dailyAmount = exact(compare(candidate, minimum) < 0 ? minimum : candidate);
      }
      return {
        planKey: plan.planKey,
        expectedVersion: plan.version,
        nextState: { ...plan, dailyAmount, enabled: stop ? false : plan.enabled }
      };
    }).filter((item) => stop || item.nextState.dailyAmount !== current.find((plan) => plan.planKey === item.planKey).dailyAmount);
    if (!changes2.length) {
      if (delivery) await enqueueUpdate(
        this.db,
        delivery.updateId,
        delivery.destination,
        JSON.stringify({ text: delivery.render([]) }),
        delivery.now
      );
      return [];
    }
    const staged = delivery && {
      ...delivery,
      reply: delivery.render(changes2.map((item) => toDomain(item.nextState)))
    };
    const result2 = await applyGroupChanges(this.db, changes2, mutationKey, this.now(), staged);
    if (result2 !== "CHANGED") throw new Error("group changed concurrently");
    const keys = new Set(changes2.map((item) => item.planKey));
    return (await listCurrentPlans(this.db)).filter((plan) => keys.has(plan.planKey)).map(toDomain);
  }
  async setThesis({ market, ticker, thesis, mutationKey, delivery = null }) {
    const plans = await this.listPlans(false);
    const matches = plans.filter((plan) => match(plan.instrument, market, ticker));
    if (matches.length === 1) {
      market = matches[0].instrument.market;
      ticker = matches[0].instrument.ticker;
    }
    const previous = await currentThesis(this.db, market, ticker);
    const contentHash = [...new Uint8Array(await crypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(thesis)
    ))].map((byte) => byte.toString(16).padStart(2, "0")).join("");
    const revisionKey = `telegram:${market}:${ticker}:${contentHash.slice(0, 32)}`;
    const staged = delivery && { ...delivery, reply: delivery.render({ market, ticker }) };
    if (previous?.revision_key === revisionKey) {
      if (staged) await enqueueUpdate(
        this.db,
        staged.updateId,
        staged.destination,
        JSON.stringify({ text: staged.reply }),
        staged.now
      );
      return { market, ticker };
    }
    const status = await appendThesis(this.db, {
      market,
      ticker,
      currency: KR.has(market) ? "KRW" : "USD",
      text: thesis,
      revisionKey,
      mutationKey,
      expectedRevision: previous?.revision_no || 0,
      effectiveAt: this.now(),
      delivery: staged
    });
    if (status !== "CHANGED") {
      const current = await currentThesis(this.db, market, ticker);
      if (current?.revision_key !== revisionKey) throw new Error("thesis changed concurrently");
      if (staged) await enqueueUpdate(
        this.db,
        staged.updateId,
        staged.destination,
        JSON.stringify({ text: staged.reply }),
        staged.now
      );
    }
    return { market, ticker };
  }
  async analyzePlans(plans, snapshot) {
    if (!this.marketProvider) return {};
    const unique = /* @__PURE__ */ new Map();
    for (const plan of plans) if (!unique.has(stable(plan)) || unique.get(stable(plan)).targetWeightPct == null && plan.targetWeightPct != null) {
      unique.set(stable(plan), plan);
    }
    const pairs = await Promise.all([...unique].map(async ([key2, plan]) => {
      try {
        return [key2, await analyzeWithProvider(
          this.marketProvider,
          {
            market: plan.instrument.market,
            ticker: plan.instrument.ticker,
            snapshot,
            targetWeightPct: plan.targetWeightPct
          }
        )];
      } catch {
        return [key2, null];
      }
    }));
    return Object.fromEntries(pairs.filter(([, value]) => value !== null));
  }
  async refreshValuations() {
    if (!this.marketProvider || typeof this.marketProvider.getValuation !== "function") {
      throw new Error("valuation provider unavailable");
    }
    const plans = await this.listPlans(true);
    this.marketProvider.cache?.clear?.("valuation:");
    const unique = new Map(plans.map((plan) => [stable(plan), plan.instrument]));
    let successes = 0;
    let failed = 0;
    const items = [...unique.values()];
    for (let offset = 0; offset < items.length; offset += 4) {
      const results = await Promise.all(items.slice(offset, offset + 4).map(async (item) => {
        try {
          await this.marketProvider.getValuation(item.market, item.ticker);
          return true;
        } catch {
          return false;
        }
      }));
      successes += results.filter(Boolean).length;
      failed += results.filter((value) => !value).length;
    }
    return { successes, failed };
  }
};

// deploy/cloudflare-js/boundary.mjs
var HEADER = "x-telegram-bot-api-secret-token";
var encoder = new TextEncoder();
var decoder = new TextDecoder("utf-8", { fatal: true });
function sameSecret(a, b) {
  const left = encoder.encode(a);
  const right = encoder.encode(b);
  let difference = left.length ^ right.length;
  for (let index = 0; index < Math.max(left.length, right.length); index++) {
    difference |= (left[index] || 0) ^ (right[index] || 0);
  }
  return difference === 0;
}
function positiveId(value) {
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}
function admittedUpdateId(headerEntries, body, expectedSecret, allowedUserIds) {
  if (!expectedSecret || !allowedUserIds.length || body.byteLength > 64 * 1024) return null;
  const secrets = headerEntries.filter(([name]) => name.toLowerCase() === HEADER);
  if (secrets.length !== 1 || !sameSecret(secrets[0][1], expectedSecret)) return null;
  let update;
  try {
    update = JSON.parse(decoder.decode(body));
  } catch {
    return null;
  }
  if (update === null || typeof update !== "object" || Array.isArray(update)) return null;
  const updateId = positiveId(update.update_id);
  if (updateId === null) return null;
  const message = update.message || update.callback_query?.message;
  const user = update.message?.from || update.callback_query?.from;
  const userId = positiveId(user?.id);
  if (userId === null || !allowedUserIds.includes(userId) || message?.chat?.type !== "private" || positiveId(message.chat.id) !== userId) return null;
  return updateId;
}

// deploy/cloudflare-js/static-commands.mjs
var START = `\u{1F916} <b>\uD1B5\uD569 \uACC4\uC88C\uAD00\uB9AC\uBD07</b>

\uC804\uCCB4\uC790\uC0B0 \uBD84\uC11D\uC740 \uD0A4\uC6C0 \uBA54\uC778 \uACC4\uC88C\uB9CC \uC0AC\uC6A9\uD569\uB2C8\uB2E4.
\uD0A4\uC6C0 \uBAA8\uC73C\uAE30\xB7\uBA54\uB9AC\uCE20\uB294 \uBAA8\uC73C\uAE30 \uC810\uAC80\uC5D0\uC11C\uB9CC \uC0AC\uC6A9\uD569\uB2C8\uB2E4.
<b>READ ONLY</b> \u2014 \uB9E4\uC218\xB7\uB9E4\uB3C4\xB7\uC815\uC815\xB7\uCDE8\uC18C \uC8FC\uBB38\uC740 \uC2E4\uD589\uD558\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4.

<code>/help</code>\uC5D0\uC11C \uBA85\uB839\uC5B4\uB97C \uD655\uC778\uD558\uC138\uC694.`;
var HELP = `\u{1F916} <b>\uD1B5\uD569 \uACC4\uC88C\uAD00\uB9AC\uBD07 \uB3C4\uC6C0\uB9D0</b>

<b>\uC6B4\uC601 \uBAA9\uC801</b>
1. \uB9E4\uC77C\uBAA8\uC73C\uAE30\uC5D0\uC11C \uBD88\uD544\uC694\uD55C \uC885\uBAA9 \uAC78\uB7EC\uB0B4\uAE30
2. \uBAA8\uC73C\uAE30 \uC124\uC815\uC744 \uAE30\uC900\uC73C\uB85C \uD558\uB8E8\xB7\uC8FC\uAC04 \uB9E4\uC218\uAE08\uC561 \uD655\uC778
3. \uC9C0\uB098\uCE5C \uC139\uD130 \uC3E0\uB9BC \uBC29\uC9C0
4. \uB9E4\uC77C\uBAA8\uC73C\uAE30 \uC885\uBAA9\xB7\uAE08\uC561 \uBC38\uB7F0\uC2F1
5. \uBAA8\uC73C\uAE30 \uC81C\uC678 \uC628\uC8FC\uB97C \uC815\uAE30 \uBD84\uC11D\uD574 \uAE30\uACC4\uC801\uC778 \uCC28\uC775\uC2E4\uD604 \uD310\uB2E8 \uC9C0\uC6D0

\u{1F331} <b>\uB9E4\uC77C\uBAA8\uC73C\uAE30 \uAD00\uB9AC</b>
/recurring \xB7 /\uBAA8\uC73C\uAE30
\uD0A4\uC6C0\xB7\uBA54\uB9AC\uCE20\xB7\uCD94\uAC00 \uD6C4\uBCF4\uC758 \uC885\uBAA9 \uD3C9\uAC00, \uC8FC\uAC04 \uAE08\uC561, \uC139\uD130 \uBD84\uC0B0, \uADFC\uAC70 \uC788\uB294 \uAE08\uC561 \uC870\uC815\uC548
\uAD8C\uC7A5 \uAE08\uC561\uC740 \uAC1C\uBCC4 \uC885\uBAA9 Valuation\uC744 \uAE30\uC900\uC73C\uB85C \uC0B0\uC815

\u{1F4CA} <b>\uD0A4\uC6C0 \uBA54\uC778\xB7\uC628\uC8FC \uAD00\uB9AC</b>
/assets \xB7 /\uC790\uC0B0 \u2014 \uD0A4\uC6C0 \uBA54\uC778 \uC790\uC0B0\uACFC \uC9C0\uC5ED\xB7\uC9D1\uC911\uB3C4
/portfolio \xB7 /\uD3EC\uD2B8\uD3F4\uB9AC\uC624 \u2014 \uD0A4\uC6C0 \uBA54\uC778 \uBCF4\uC720\uC885\uBAA9\xB7\uBE44\uC911\xB7\uC218\uC775\uB960
/analyze AMD \xB7 /\uBD84\uC11D AMD \u2014 \uD0A4\uC6C0 \uBA54\uC778 \uAE30\uC900 \uC885\uBAA9 \uC815\uB7C9 \uBD84\uC11D
/stock AMD \xB7 /\uC885\uBAA9 AMD \u2014 \uD604\uC7AC\uAC00 \uD655\uC778
/weekly \xB7 /\uC8FC\uAC04\uB9AC\uD3EC\uD2B8 \u2014 \uD0A4\uC6C0 \uBA54\uC778\uACFC \uBAA8\uC73C\uAE30 \uC8FC\uAC04 \uC810\uAC80

\u{1F527} <b>\uC0C1\uD0DC\xB7\uACE0\uAE09 \uBA85\uB839</b>
/recurring_add AMD 2000KRW 8% \u2014 \uBD84\uC11D\uC6A9 \uB85C\uCEEC \uACC4\uD68D \uCD94\uAC00
/recurring_update ETF +1000 \u2014 ETF \uC804\uCCB4 \uAE08\uC561 \uBCC0\uACBD
/\uBAA8\uC73C\uAE30\uBCC0\uACBD \uAC1C\uBCC4\uC8FC NVDA,AMD \uBE7C\uACE0 -1000 \u2014 \uC77C\uBD80 \uC81C\uC678 \uD6C4 \uBCC0\uACBD
/\uBAA8\uC73C\uAE30\uBCC0\uACBD ETF \uC804\uBD80 \uBAA8\uC73C\uAE30\uC911\uB2E8 \u2014 \uD574\uB2F9 \uADF8\uB8F9 \uC911\uB2E8
/rebalance 500000 \xB7 /\uB9AC\uBC38\uB7F0\uC2F1 500000 \u2014 \uBAA9\uD45C\uBE44\uC911 \uAE30\uBC18 \uC2E0\uADDC\uC790\uAE08 \uC758\uACAC
/thesis AMD \uD22C\uC790\uB17C\uB9AC \u2014 \uC885\uBAA9 Thesis \uC800\uC7A5

\uBA54\uB9AC\uCE20\uC640 \uD0A4\uC6C0 \uBAA8\uC73C\uAE30 \uBCF4\uC720\uC790\uC0B0\uC740 \uC804\uCCB4\uACC4\uC88C \uD3C9\uAC00\uC5D0\uC11C \uC81C\uC678\uB429\uB2C8\uB2E4.
\uD55C\uAD6D \uC885\uBAA9\uC740 <code>KOSPI:005930</code>\uCC98\uB7FC \uC2DC\uC7A5\uC744 \uC9C0\uC815\uD569\uB2C8\uB2E4.
<b>READ ONLY</b> \u2014 \uB9E4\uC218\xB7\uB9E4\uB3C4\xB7\uC815\uC815\xB7\uCDE8\uC18C \uC8FC\uBB38\uC744 \uC2E4\uD589\uD558\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4.
\uC139\uD130\xB7\uC628\uC8FC \uC790\uB3D9 \uC120\uBCC4\uC740 \uAC80\uC99D \uAC00\uB2A5\uD55C \uB370\uC774\uD130\uB9CC \uC0AC\uC6A9\uD558\uBA70 \uBD80\uC871\uD558\uBA74 \uD310\uB2E8\uC744 \uBCF4\uB958\uD569\uB2C8\uB2E4.`;

// deploy/cloudflare-js/portfolio-report.mjs
var labels = /* @__PURE__ */ new Map([
  ["kiwoom", "\uD0A4\uC6C0"],
  ["kiwoom_main", "\uD0A4\uC6C0 \uBA54\uC778"],
  ["kiwoom_accum", "\uD0A4\uC6C0 \uBAA8\uC73C\uAE30"],
  ["toss", "\uD1A0\uC2A4"],
  ["meritz", "\uBA54\uB9AC\uCE20"]
]);
var statuses = /* @__PURE__ */ new Map([
  ["ok", "\uC815\uC0C1"],
  ["partial", "\uC77C\uBD80\uB9CC \uC870\uD68C"],
  ["stale", "\uB9C8\uC9C0\uB9C9 \uC815\uC0C1\uAC12"],
  ["not_configured", "\uBBF8\uC124\uC815"],
  ["auth_failed", "\uC778\uC99D \uC2E4\uD328"],
  ["rate_limited", "\uD638\uCD9C \uC81C\uD55C"],
  ["unavailable", "\uC870\uD68C \uC2E4\uD328"]
]);
var SAFE_MASK = /^\*{3,}(?:[A-Za-z_-]{1,16}|[0-9]{1,4})$/;
function normalizedQuantity(value) {
  let result2 = exact(decimal(value));
  if (result2.includes(".")) result2 = result2.replace(/0+$/, "").replace(/\.$/, "");
  return result2;
}
function formatAccounts(accountResults, collectedAt, isComplete) {
  const lines = ["\u{1F3E6} <b>\uC99D\uAD8C\uC0AC\uBCC4 \uACC4\uC88C</b>"];
  for (const result2 of accountResults) {
    lines.push("", `<b>${html(labels.get(result2.broker) || result2.broker)}</b> \xB7 ` + statuses.get(result2.status));
    if (result2.items.length) {
      for (const account of result2.items) {
        const label = SAFE_MASK.test(account.displayName || "") ? account.displayName : "\uB4F1\uB85D \uACC4\uC88C";
        lines.push(`\u2022 ${html(label)}`);
      }
    } else lines.push("\u2022 \uD45C\uC2DC \uAC00\uB2A5\uD55C \uACC4\uC88C \uC5C6\uC74C");
  }
  lines.push(
    "",
    "\uC804\uCCB4 \uACC4\uC88C\uBC88\uD638\uB294 \uC800\uC7A5\xB7\uD45C\uC2DC\uD558\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4.",
    html(`\uAE30\uC900 ${localTime(collectedAt)} KST \xB7 ${isComplete ? "\uC804\uCCB4 \uC870\uD68C" : "\uBD88\uC644\uC804 \uC870\uD68C"}`)
  );
  return lines.join("\n");
}
function formatPortfolio(snapshot, limit = 30) {
  if (!Number.isSafeInteger(limit) || limit < 0) throw new TypeError("invalid limit");
  const scope = snapshot.expectedBrokers?.length === 1 && snapshot.expectedBrokers[0] === "kiwoom_main" ? "\uD0A4\uC6C0 \uBA54\uC778" : "\uC120\uD0DD \uCD9C\uCC98";
  const title = snapshot.isComplete ? `\u{1F4CA} <b>${scope} \uD3EC\uD2B8\uD3F4\uB9AC\uC624</b>` : `\u26A0\uFE0F <b>${scope} \uD3EC\uD2B8\uD3F4\uB9AC\uC624 \uBD80\uBD84 \uC870\uD68C</b>`;
  const rankingValue = (item) => item.marketValueBase === null || compare(decimal(item.marketValueBase), decimal("0")) === 0 ? decimal("-1") : decimal(item.marketValueBase);
  const positions = [...snapshot.positions].sort((a, b) => compare(rankingValue(b), rankingValue(a)));
  const lines = [title];
  if (!positions.length) lines.push("", "\uC870\uD68C\uB41C \uBCF4\uC720 \uC885\uBAA9\uC774 \uC5C6\uC2B5\uB2C8\uB2E4.");
  positions.slice(0, limit).forEach((position, index) => {
    const name = html(position.companyName || position.instrument.ticker);
    const identity = html(`${position.instrument.market}:${position.instrument.ticker}`);
    lines.push("", `<b>${index + 1}. ${name} (${identity})</b>`);
    lines.push(position.marketValueBase === null ? "\uD3C9\uAC00\uC561 \uD655\uC778 \uBD88\uAC00" : `\uD3C9\uAC00\uC561 ${money(position.marketValueBase, snapshot.baseCurrency)}`);
    lines.push(`\uBE44\uC911 ${percent(position.weightPct === null ? null : decimal(position.weightPct))}`);
    lines.push(`\uC218\uC775\uB960 ${percent(position.returnPct === null ? null : decimal(position.returnPct))}`);
    if (position.brokerQuantities.length > 1) {
      const detail = position.brokerQuantities.map(([broker, quantity]) => `${html(broker)} ${normalizedQuantity(quantity)}\uC8FC`).join(" \xB7 ");
      lines.push(`\uD569\uC0B0 ${normalizedQuantity(position.quantity)}\uC8FC (${detail})`);
    }
  });
  if (positions.length > limit) lines.push("", `\uC678 ${positions.length - limit}\uAC1C \uC885\uBAA9`);
  if (snapshot.isComplete && snapshot.totalMarketValue !== null && compare(decimal(snapshot.totalMarketValue), decimal("0")) > 0 && positions.length && positions[0].weightPct !== null && compare(decimal(positions[0].weightPct), decimal("15")) >= 0) {
    const top = positions[0];
    lines.push(
      "",
      "\u26A0\uFE0F <b>\uC9D1\uC911\uB3C4</b>",
      `${html(`${top.instrument.market}:${top.instrument.ticker}`)} \uBE44\uC911\uC774 ${percent(decimal(top.weightPct))}\uC785\uB2C8\uB2E4.`
    );
  }
  if (!snapshot.isComplete) {
    lines.push("", "\uBE44\uC911\xB7\uC9D1\uC911\uB3C4\uB294 \uB204\uB77D \uC99D\uAD8C\uC0AC\uB97C 0\uC6D0\uC73C\uB85C \uB450\uC9C0 \uC54A\uAE30 \uC704\uD574 \uBCF4\uB958\uB429\uB2C8\uB2E4.");
  }
  lines.push("", "\uC139\uD130\xB7\uAD6D\uAC00 \uBD84\uB958\uB294 \uC2DC\uC7A5\uB370\uC774\uD130\uAC00 \uD655\uC778\uB41C \uC885\uBAA9\uB9CC \uC8FC\uAC04 \uBD84\uC11D\uC5D0 \uBC18\uC601\uB429\uB2C8\uB2E4.");
  return lines.join("\n");
}

// deploy/cloudflare-js/recurring-report.mjs
var zero3 = () => decimal("0");
var exposureGroups = [
  ["\uBBF8\uAD6D \uB300\uD615 \uAE30\uC220\xB7\uC131\uC7A5 ETF", "QQQ QLD MAGS VGT"],
  ["\uBBF8\uAD6D \uAD11\uBC94\uC704 \uC9C0\uC218", "VOO SSO SPMO IWM VTWO VUG"],
  ["\uBC18\uB3C4\uCCB4", "USD SOXQ SOXX ASX TSM KLAC LRCX ASML MPWR STM MRVL NVDA AMD AVGO MU ALGM TXN ARM QCOM"],
  ["\uAE08\uC735", "XLF CME"],
  ["\uACBD\uAE30\uC18C\uBE44\uC7AC", "AMZN TSLA"],
  ["\uC0B0\uC5C5\uC7AC", "PAVE CAT RRX CGNX JCI TKR XYL TT ETN ROK"],
  ["\uC0B0\uC5C5\uC7AC\xB7\uAE30\uC220", "NOVT"],
  ["\uB370\uC774\uD130\uC800\uC7A5\xB7\uAE30\uC220", "STX WDC SNDK"],
  ["\uAD11\uD1B5\uC2E0\xB7\uAE30\uC220", "COHR LITE GLW"],
  ["\uCEE4\uBBA4\uB2C8\uCF00\uC774\uC158", "GOOG"],
  ["\uC591\uC790\uCEF4\uD4E8\uD305\xB7\uAE30\uC220", "QNT"],
  ["\uCCAD\uC815\uC5D0\uB108\uC9C0", "BE FLNC"],
  ["\uC18C\uC7AC", "ECL"],
  ["\uD5EC\uC2A4\uCF00\uC5B4", "XLV LLY UNH"],
  ["\uC18C\uD504\uD2B8\uC6E8\uC5B4", "PLTR"],
  ["\uC804\uB825\uC778\uD504\uB77C", "GEV VRT"],
  ["\uC635\uC158\uC778\uCEF4\xB7\uBC30\uB2F9 ETF", "QDVO QDTE QYLD QQQI GPIQ JEPQ SCHD DIVO VYM DGRO"],
  ["\uAE08", "GLDM"]
];
var exposures = new Map(exposureGroups.flatMap(([label, tickers]) => tickers.split(" ").map((ticker) => [ticker, label])));
var sourceLabels = /* @__PURE__ */ new Map([
  ["kiwoom_accum", "\uD0A4\uC6C0 \uBAA8\uC73C\uAE30"],
  ["meritz", "\uBA54\uB9AC\uCE20 \uBAA8\uC73C\uAE30"],
  ["candidate", "\uCD94\uAC00 \uD6C4\uBCF4 7\uC885\uBAA9"],
  ["other", "\uAE30\uD0C0"]
]);
var planSourceLabels = /* @__PURE__ */ new Map([
  ["kiwoom_accum", "\uD0A4\uC6C0 \uBAA8\uC73C\uAE30"],
  ["meritz", "\uBA54\uB9AC\uCE20 \uBAA8\uC73C\uAE30"],
  ["candidate", "\uC0AC\uC6A9\uC790 \uCD94\uAC00 \uD6C4\uBCF4 \xB7 \uD0A4\uC6C0 \uC124\uC815 \uBBF8\uD655\uC778"]
]);
var weekdayNames = ["\uC6D4", "\uD654", "\uC218", "\uBAA9", "\uAE08", "\uD1A0", "\uC77C"];
var stableId = (plan) => `${plan.instrument.market}:${plan.instrument.ticker}`;
var fullId = (plan) => `${stableId(plan)}:${plan.instrument.assetType}:${plan.instrument.currency}`;
var weekly = (plan) => multiply(
  decimal(plan.dailyAmount),
  decimal(String(plan.scheduledWeekdays.length))
);
var exposure = (plan, analysis) => exposures.get(plan.instrument.ticker) || analysis?.fundamentals?.sector || "\uBD84\uB958 \uD655\uC778 \uBD88\uAC00";
var percent2 = (numerator, denominator, places) => `${display(multiply(divide28(numerator, denominator), decimal("100")), places)}`;
function schedule(plan) {
  const days = plan.scheduledWeekdays;
  if (days.join(",") === "0,1,2,3,4") return "\uD3C9\uC77C \uB9E4\uC77C";
  if (days.join(",") === "0") return "\uB9E4\uC8FC \uC6D4\uC694\uC77C";
  if (days.join(",") === "4") return "\uB9E4\uC8FC \uAE08\uC694\uC77C";
  return "\uB9E4\uC8FC " + days.map((day) => weekdayNames[day]).join("\xB7");
}
function valuationScore(analysis) {
  return analysis?.companyScore?.components?.find((component) => component.name === "valuation")?.score ?? null;
}
function recommended(plan, analysis) {
  const current = decimal(plan.dailyAmount);
  if (!plan.enabled) return {
    amount: current,
    action: "\uBE44\uD65C\uC131 \uC720\uC9C0",
    reasons: ["\uBE44\uD65C\uC131 \uACC4\uD68D\uC740 \uAE08\uC561 \uC870\uC815 \uB300\uC0C1\uC5D0\uC11C \uC81C\uC678"]
  };
  const step = decimal(plan.currency === "KRW" ? "1000" : "1");
  const minimum = decimal(plan.currency === "KRW" && plan.broker !== "meritz" ? "2000" : plan.currency === "KRW" ? "1000" : "1");
  const valuation = valuationScore(analysis);
  const factor = valuation === null ? "1" : compare(decimal(valuation), decimal("35")) < 0 ? "0.8" : compare(decimal(valuation), decimal("70")) >= 0 ? "1.1" : "1";
  const reasons = valuation === null ? ["Valuation \uB370\uC774\uD130 \uBD80\uC871\uC73C\uB85C \uD604\uC7AC \uAE08\uC561 \uC720\uC9C0"] : compare(decimal(valuation), decimal("35")) < 0 ? [`Valuation ${fixed(valuation, 1)}\uC810\uC73C\uB85C \uAC00\uACA9 \uBD80\uB2F4`] : compare(decimal(valuation), decimal("70")) >= 0 ? [`Valuation ${fixed(valuation, 1)}\uC810\uC73C\uB85C \uAC00\uACA9 \uB9E4\uB825 \uC591\uD638`] : [`Valuation ${fixed(valuation, 1)}\uC810\uC73C\uB85C \uD604\uC7AC \uAE08\uC561 \uC720\uC9C0`];
  const rounded2 = decimal(display(divide28(
    multiply(current, decimal(factor)),
    step
  ), 0).replaceAll(",", ""));
  const proposed = multiply(rounded2, step);
  const amount2 = compare(proposed, minimum) < 0 ? minimum : proposed;
  return {
    amount: amount2,
    action: compare(amount2, current) > 0 ? "\uC99D\uC561" : compare(amount2, current) < 0 ? "\uAC10\uC561" : "\uC720\uC9C0",
    reasons
  };
}
function formatRecurring(plans, analyses = {}) {
  if (!plans.length) return "\u{1F331} <b>\uB9E4\uC77C\uBAA8\uC73C\uAE30</b>\n\n\uB4F1\uB85D\uB41C \uC885\uBAA9\uC774 \uC5C6\uC2B5\uB2C8\uB2E4.\n<code>/recurring_add AMD 20USD 8%</code>\uB85C \uB85C\uCEEC \uACC4\uD68D\uC744 \uB4F1\uB85D\uD560 \uC218 \uC788\uC2B5\uB2C8\uB2E4.";
  const totals = /* @__PURE__ */ new Map();
  const counts = /* @__PURE__ */ new Map();
  const byExposure = /* @__PURE__ */ new Map();
  const bySource = /* @__PURE__ */ new Map();
  for (const plan of plans) {
    if (!plan.enabled) continue;
    const currency2 = plan.currency;
    const amount2 = weekly(plan);
    totals.set(currency2, add(totals.get(currency2) || zero3(), amount2));
    counts.set(currency2, (counts.get(currency2) || 0) + 1);
    if (!byExposure.has(currency2)) byExposure.set(currency2, /* @__PURE__ */ new Map());
    if (!bySource.has(currency2)) bySource.set(currency2, /* @__PURE__ */ new Map());
    const group = exposure(plan, analyses[fullId(plan)]);
    byExposure.get(currency2).set(group, add(byExposure.get(currency2).get(group) || zero3(), amount2));
    const source = plan.broker || "other";
    bySource.get(currency2).set(source, add(bySource.get(currency2).get(source) || zero3(), amount2));
  }
  const lines = ["\u{1F331} <b>\uB9E4\uC77C\uBAA8\uC73C\uAE30</b>"];
  const sorted = [...plans].sort((left, right) => ({ ETF: 0, EQUITY: 1 }[left.instrument.assetType] ?? 2) - ({ ETF: 0, EQUITY: 1 }[right.instrument.assetType] ?? 2) || (left.broker || "").localeCompare(right.broker || "") || left.instrument.ticker.localeCompare(right.instrument.ticker));
  let lastType = null;
  for (const plan of sorted) if (plan.instrument.assetType !== lastType) {
    lastType = plan.instrument.assetType;
    lines.push("", `\u{1F4C2} <b>${{ ETF: "ETF", EQUITY: "\uAC1C\uBCC4\uC8FC" }[lastType] || "\uAE30\uD0C0"}</b>`);
  }
  const recommendations = plans.map((plan) => recommended(plan, analyses[fullId(plan)]));
  for (const [index, plan] of plans.entries()) {
    lines.push("", `<b>${html(stableId(plan))}</b>`);
    if (plan.broker) lines.push(`\uCD9C\uCC98 ${html(planSourceLabels.get(plan.broker) || plan.broker)}`);
    lines.push(`${schedule(plan)} ${money(plan.dailyAmount, plan.currency)}`);
    const total = totals.get(plan.currency) || zero3();
    const weight = plan.enabled && compare(total, zero3()) > 0 ? `${percent2(weekly(plan), total, 2)}%` : "\uD655\uC778 \uBD88\uAC00";
    const count = counts.get(plan.currency) || 0;
    const equal = count ? ` \xB7 \uADE0\uB4F1 \uAE30\uC900 ${percent2(decimal("1"), decimal(String(count)), 2)}%` : "";
    lines.push(`\uBAA8\uC73C\uAE30 \uB0B4 \uBE44\uC911 ${weight}${equal} \xB7 \uC8FC\uAC04 \uD658\uC0B0`);
    const analysis = analyses[fullId(plan)];
    if (analysis && (!analysis.companyScore?.components || !analysis.recurringSuitability || !analysis.finalScore)) {
      throw new TypeError("incomplete recurring analysis");
    }
    lines.push(`\uB178\uCD9C\uAD70 ${exposure(plan, analysis)}`);
    if (analysis) {
      const valuation = valuationScore(analysis);
      const score2 = (value) => value == null ? "\uBCF4\uB958" : display(decimal(value), 2).replaceAll(",", "");
      lines.push(
        `\uAE30\uC5C5\uC810\uC218 ${score2(analysis.companyScore.score)} / 100`,
        `Valuation ${score2(valuation)} / 100`,
        `\uC7A5\uAE30\uCD94\uC138 \uC801\uD569\uC131 ${score2(analysis.recurringSuitability.score)} / 100`
      );
      if (analysis.recurringSuitability.reasons?.length) {
        lines.push("\uCD94\uC138 \uADFC\uAC70: " + html(analysis.recurringSuitability.reasons.join(" \xB7 ")));
      }
      lines.push(`\uBAA8\uC73C\uAE30 \uBC30\uBD84 \uC758\uACAC: ${valuation === null ? "\uD310\uB2E8 \uBCF4\uB958 \u2014 Valuation \uB370\uC774\uD130 \uBD80\uC871" : compare(decimal(valuation), decimal("35")) < 0 ? "\uAC10\uC561 \uAC80\uD1A0 \u2014 Valuation \uAC00\uACA9 \uBD80\uB2F4" : compare(decimal(valuation), decimal("70")) >= 0 ? "\uC99D\uC561 \uAC80\uD1A0 \u2014 Valuation \uAC00\uACA9 \uB9E4\uB825 \uC591\uD638" : "\uC720\uC9C0 \u2014 Valuation \uC911\uB9BD \uAD6C\uAC04"}`);
    } else lines.push(
      "\uC885\uBAA9 \uC801\uD569\uC131: \uBD84\uC11D \uB370\uC774\uD130 \uBD80\uC871",
      "\uBAA8\uC73C\uAE30 \uBC30\uBD84 \uC758\uACAC: \uD310\uB2E8 \uBCF4\uB958 \u2014 Valuation \uB370\uC774\uD130 \uBD80\uC871"
    );
    const rec = recommendations[index];
    lines.push(`\uAD8C\uC7A5 1\uD68C \uB9E4\uC218\uAE08\uC561: ${money(plan.dailyAmount, plan.currency)} \u2192 ${money(exact(rec.amount), plan.currency)} (${rec.action})`);
    lines.push("\uC870\uC815 \uADFC\uAC70: " + rec.reasons.map(html).join(" \xB7 "));
    if (plan.investmentThesis) lines.push(`Thesis: ${html(plan.investmentThesis.slice(0, 180))}`);
  }
  if (byExposure.size) {
    lines.push("", "\u{1F4CC} <b>\uCD5C\uC885 \uC694\uC57D \xB7 \uC8FC\uAC04 \uD658\uC0B0</b>");
    for (const currency2 of [...byExposure.keys()].sort()) {
      const total = totals.get(currency2);
      lines.push("", `<b>\uC8FC\uAC04 \uD569\uACC4 ${money(exact(total), currency2)}</b>`);
      for (const [source, amount2] of [...bySource.get(currency2)].sort((a, b) => -compare(a[1], b[1]))) {
        lines.push(`\u2022 ${html(sourceLabels.get(source) || source)} ${money(exact(amount2), currency2)}`);
      }
      lines.push("\uC139\uD130\xB7\uB178\uCD9C\uAD70 \uBE44\uC911");
      for (const [group, amount2] of [...byExposure.get(currency2)].sort((a, b) => -compare(a[1], b[1]))) {
        const pct2 = percent2(amount2, total, 1);
        lines.push(`\u2022 ${html(group)} ${pct2}%${compare(decimal(pct2), decimal("25")) >= 0 ? " \u26A0\uFE0F" : ""}`);
      }
    }
    lines.push("", "\u{1F4A1} <b>\uAD8C\uC7A5 \uAE08\uC561 \uC801\uC6A9 \uC2DC</b>");
    for (const currency2 of [...totals.keys()].sort()) {
      const prior = totals.get(currency2);
      const future = plans.reduce((sum2, plan, index) => plan.enabled && plan.currency === currency2 ? add(
        sum2,
        multiply(
          recommendations[index].amount,
          decimal(String(plan.scheduledWeekdays.length))
        )
      ) : sum2, zero3());
      const change = compare(prior, zero3()) > 0 ? multiply(divide28(future, prior), decimal("100")) : zero3();
      const difference = compare(prior, zero3()) > 0 ? exact(add(change, decimal("-100"))) : "0";
      const signed = compare(decimal(difference), zero3()) >= 0 ? "+" : "";
      lines.push(`\u2022 ${currency2} \uC8FC\uAC04 ${money(exact(prior), currency2)} \u2192 ${money(exact(future), currency2)} (${signed}${display(decimal(difference), 1)}%)`);
    }
  }
  lines.push(
    "",
    "\uBA54\uB9AC\uCE20\uB294 \uC624\uB298 \uACC4\uD68D\uB300\uB85C \uC2E4\uD589\uB410\uB2E4\uACE0 \uAC00\uC815\uD55C \uC2DC\uBBAC\uB808\uC774\uC158\uC774\uBA70 \uC2E4\uC81C \uCCB4\uACB0 \uD655\uC778\uC774 \uC544\uB2D9\uB2C8\uB2E4.",
    "\uAD8C\uC7A5 \uAE08\uC561\uC740 \uC790\uB3D9 \uC801\uC6A9\uB418\uC9C0 \uC54A\uC73C\uBA70, \uAC1C\uBCC4 \uC885\uBAA9 Valuation\uC744 \uAE30\uC900\uC73C\uB85C \uC0B0\uC815\uD569\uB2C8\uB2E4.",
    "",
    "\u{1F9FE} <b>\uBAA8\uC73C\uAE30 \uAE08\uC561 \uC870\uC815 \uC885\uBAA9 \uC694\uC57D</b>"
  );
  const reduced = recommendations.map((rec, index) => [rec, plans[index]]).filter(([rec]) => rec.action === "\uAC10\uC561");
  const increased = recommendations.map((rec, index) => [rec, plans[index]]).filter(([rec]) => rec.action === "\uC99D\uC561");
  if (!reduced.length && !increased.length) lines.push("\uD604\uC7AC \uACC4\uC0B0\uC0C1 \uAE08\uC561\uC744 \uBCC0\uACBD\uD560 \uC885\uBAA9\uC774 \uC5C6\uC2B5\uB2C8\uB2E4.");
  if (reduced.length) {
    lines.push("", "\u{1F53B} <b>\uC904\uC5EC\uC57C \uD560 \uC885\uBAA9</b>");
    for (const [rec, plan] of reduced) lines.push(
      `\u2022 <b>${html(stableId(plan))}</b> ${money(plan.dailyAmount, plan.currency)} \u2192 ` + money(exact(rec.amount), plan.currency),
      "  \uC774\uC720: " + rec.reasons.map(html).join(" \xB7 ")
    );
  }
  if (increased.length) {
    lines.push("", "\u{1F53A} <b>\uB298\uB9B4 \uC218 \uC788\uB294 \uC885\uBAA9</b>");
    for (const [rec, plan] of increased) lines.push(
      `\u2022 <b>${html(stableId(plan))}</b> ${money(plan.dailyAmount, plan.currency)} \u2192 ` + money(exact(rec.amount), plan.currency),
      "  \uADFC\uAC70: " + rec.reasons.map(html).join(" \xB7 ")
    );
  }
  return lines.join("\n");
}

// deploy/cloudflare-js/rebalance.mjs
var zero4 = () => decimal("0");
var hundred2 = () => decimal("100");
var key = (item) => `${item.market}:${item.ticker}:${item.assetType}:${item.currency}`;
var displayId = (item) => `${item.market}:${item.ticker}`;
var usMarkets = /* @__PURE__ */ new Set(["US", "USA", "NASDAQ", "NAS", "NYSE", "AMEX", "ARCA", "NYSEARCA"]);
function compatible2(left, right) {
  if (left.ticker !== right.ticker || left.assetType !== right.assetType || left.currency !== right.currency) return false;
  return left.market === right.market || ["US", "USA"].includes(left.market) && usMarkets.has(right.market) || ["US", "USA"].includes(right.market) && usMarkets.has(left.market);
}
function pct(value, denominator) {
  return compare(denominator, zero4()) > 0 ? round28(multiply(divide28(value, denominator), hundred2())) : null;
}
function suggestNewMoneyRebalance(snapshot, targets, newMoney) {
  const incoming = decimal(newMoney);
  if (compare(incoming, zero4()) < 0) throw new TypeError("negative new money");
  const targetInput = /* @__PURE__ */ new Map();
  let targetSum = zero4();
  for (const target of targets) {
    const identity = key(target.instrument);
    const weight = decimal(target.targetWeightPct);
    if (targetInput.has(identity) || compare(weight, zero4()) < 0 || compare(weight, hundred2()) > 0) throw new TypeError("invalid target");
    targetInput.set(identity, { instrument: target.instrument, weight });
    targetSum = add(targetSum, weight);
  }
  if (compare(targetSum, hundred2()) > 0) throw new TypeError("target sum above 100");
  const withheld = (reason) => ({
    isEligible: false,
    baseCurrency: snapshot.baseCurrency,
    newMoney,
    allocatedNewMoney: "0",
    unallocatedCash: newMoney,
    projectedTotalValue: null,
    items: [],
    usesSales: false,
    warnings: [reason]
  });
  if (!snapshot.isComplete || snapshot.totalMarketValue === null) {
    return withheld("rebalance_withheld:portfolio_snapshot_incomplete");
  }
  const current = /* @__PURE__ */ new Map();
  for (const position of snapshot.positions) {
    if (position.marketValueBase === null) {
      return withheld("rebalance_withheld:position_valuation_missing");
    }
    current.set(key(position.instrument), {
      instrument: position.instrument,
      value: decimal(position.marketValueBase)
    });
  }
  const canonical = /* @__PURE__ */ new Map();
  const canonicalInstruments = /* @__PURE__ */ new Map();
  const aliasWarnings = [];
  for (const target of targetInput.values()) {
    let identity = key(target.instrument);
    if (!current.has(identity)) {
      const matches = [...current.values()].filter((row) => compatible2(row.instrument, target.instrument));
      if (matches.length > 1) return withheld("rebalance_withheld:ambiguous_instrument_alias");
      if (matches.length === 1) identity = key(matches[0].instrument);
    }
    if (canonical.has(identity)) throw new TypeError("duplicate compatible target");
    canonical.set(identity, target.weight);
    canonicalInstruments.set(identity, current.get(identity)?.instrument || target.instrument);
    if (identity !== key(target.instrument)) {
      aliasWarnings.push(`instrument_alias_resolved:${displayId(target.instrument)}->` + displayId(current.get(identity).instrument));
    }
  }
  const currentTotal = decimal(snapshot.totalMarketValue);
  const projectedTotal = add(currentTotal, incoming);
  const all = new Map(current);
  for (const [identity, instrument3] of canonicalInstruments) {
    if (!all.has(identity)) all.set(identity, { instrument: instrument3, value: zero4() });
  }
  const deficits = /* @__PURE__ */ new Map();
  let totalDeficit = zero4();
  for (const [identity, row] of all) {
    const targetValue = divide28(multiply(projectedTotal, canonical.get(identity) || zero4()), hundred2());
    const gap = subtract(targetValue, row.value);
    const deficit = compare(gap, zero4()) > 0 ? gap : zero4();
    deficits.set(identity, deficit);
    totalDeficit = add(totalDeficit, deficit);
  }
  const available = compare(incoming, totalDeficit) < 0 ? incoming : totalDeficit;
  const allocations = new Map([...all.keys()].map((identity) => [identity, zero4()]));
  if (compare(totalDeficit, zero4()) > 0 && compare(available, zero4()) > 0) {
    if (compare(totalDeficit, available) <= 0) {
      for (const [identity, deficit] of deficits) allocations.set(identity, deficit);
    } else {
      for (const [identity, deficit] of deficits) {
        allocations.set(identity, divide28(multiply(available, deficit), totalDeficit));
      }
      const allocated2 = [...allocations.values()].reduce(add, zero4());
      const residual = subtract(available, allocated2);
      if (compare(residual, zero4()) !== 0) {
        const largest = [...deficits.keys()].sort((a, b) => compare(deficits.get(b), deficits.get(a)) || a.localeCompare(b))[0];
        allocations.set(largest, add(allocations.get(largest), residual));
      }
    }
  }
  const allocated = [...allocations.values()].reduce(add, zero4());
  const items = [...all.keys()].sort().map((identity) => {
    const row = all.get(identity);
    const targetWeight = canonical.get(identity) || zero4();
    const suggestion = allocations.get(identity);
    const currentWeight = pct(row.value, currentTotal);
    const projectedWeight = pct(add(row.value, suggestion), projectedTotal);
    const remainingGap = projectedWeight === null ? null : subtract(targetWeight, projectedWeight);
    const action = compare(suggestion, zero4()) > 0 ? "add_with_new_money" : currentWeight !== null && compare(currentWeight, targetWeight) > 0 ? "overweight_no_new_money" : "hold";
    return {
      instrument: row.instrument,
      currentValue: exact(row.value),
      currentWeightPct: currentWeight === null ? null : exact(currentWeight),
      targetWeightPct: exact(targetWeight),
      suggestedNewMoney: exact(suggestion),
      projectedWeightPct: projectedWeight === null ? null : exact(projectedWeight),
      remainingGapPct: remainingGap === null ? null : exact(remainingGap),
      action
    };
  });
  return {
    isEligible: true,
    baseCurrency: snapshot.baseCurrency,
    newMoney,
    allocatedNewMoney: exact(allocated),
    unallocatedCash: exact(subtract(incoming, allocated)),
    projectedTotalValue: exact(projectedTotal),
    items,
    usesSales: false,
    warnings: ["new_money_first:no_sales_suggested", ...aliasWarnings]
  };
}
function formatRebalance(plan) {
  const lines = ["\u2696\uFE0F <b>Portfolio Rebalancing</b>"];
  if (!plan.isEligible) return [
    ...lines,
    "",
    "\uBD88\uC644\uC804\uD55C \uD3EC\uD2B8\uD3F4\uB9AC\uC624 \uBD84\uBAA8\uC774\uBBC0\uB85C \uC81C\uC548 \uBCF4\uB958",
    "\uB204\uB77D \uACC4\uC88C\uB97C 0\uC6D0\uC73C\uB85C \uAC04\uC8FC\uD558\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4."
  ].join("\n");
  for (const item of plan.items) {
    const action = item.action === "add_with_new_money" ? `\uC2E0\uADDC \uC790\uAE08 ${money(item.suggestedNewMoney, plan.baseCurrency)} \uBC30\uBD84` : item.action === "overweight_no_new_money" ? "\uACFC\uB2E4 \u2014 \uC2E0\uADDC \uC790\uAE08 \uBC30\uBD84 \uC911\uB2E8" : "\uC720\uC9C0";
    lines.push(
      "",
      `<b>${html(displayId(item.instrument))}</b>`,
      `\uD604\uC7AC ${percent(item.currentWeightPct === null ? null : decimal(item.currentWeightPct))} \xB7 \uBAA9\uD45C ${percent(decimal(item.targetWeightPct))}`,
      action
    );
  }
  lines.push(
    "",
    "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501",
    "",
    `\uBC30\uBD84 \uD569\uACC4 ${money(plan.allocatedNewMoney, plan.baseCurrency)}`,
    `\uD604\uAE08 \uC720\uC9C0 ${money(plan.unallocatedCash, plan.baseCurrency)}`,
    "",
    "\uB9E4\uB3C4\uB97C \uC81C\uC548\uD558\uC9C0 \uC54A\uACE0 \uC2E0\uADDC \uC790\uAE08 \uBC30\uBD84\uC744 \uC6B0\uC120\uD588\uC2B5\uB2C8\uB2E4."
  );
  return lines.join("\n");
}

// deploy/cloudflare-js/dynamic-commands.mjs
function instrumentParts(raw, positions = []) {
  const token = raw.trim().toUpperCase();
  if (token.includes(":")) {
    const index = token.indexOf(":");
    const market = token.slice(0, index);
    const ticker = token.slice(index + 1);
    if (market && ticker) return [market, ticker];
  }
  const markets = new Set(positions.filter((position) => position.instrument.ticker === token).map((position) => position.instrument.market));
  if (markets.size === 1) return [[...markets][0], token];
  return /^\d+$/.test(token) ? ["KOSPI", token.padStart(6, "0")] : ["NASDAQ", token];
}
async function stockReply(raw, provider, positions = []) {
  if (!raw.trim()) return "\uC0AC\uC6A9\uBC95: <code>/stock AMD</code>";
  if (!provider) return "\uC2DC\uC7A5\uB370\uC774\uD130 provider\uAC00 \uC124\uC815\uB418\uC9C0 \uC54A\uC558\uC2B5\uB2C8\uB2E4.";
  const [market, ticker] = instrumentParts(raw, positions);
  const quote = await readPrice(provider, market, ticker);
  if (quote === null) return "\uD604\uC7AC\uAC00\uB97C \uD655\uC778\uD560 \uC218 \uC5C6\uC2B5\uB2C8\uB2E4. \uC7A0\uC2DC \uD6C4 \uB2E4\uC2DC \uC2DC\uB3C4\uD574 \uC8FC\uC138\uC694.";
  const at = new Date(quote.asOf);
  if (Number.isNaN(at.getTime())) return "\uD604\uC7AC\uAC00\uB97C \uD655\uC778\uD560 \uC218 \uC5C6\uC2B5\uB2C8\uB2E4. \uC7A0\uC2DC \uD6C4 \uB2E4\uC2DC \uC2DC\uB3C4\uD574 \uC8FC\uC138\uC694.";
  const timestamp = at.toISOString().slice(0, 16).replace("T", " ");
  return `\u{1F4C8} <b>${html(market)}:${html(ticker)}</b>

\uD604\uC7AC\uAC00 ${money(quote.price, quote.currency)}` + (quote.stale ? " \xB7 \uB9C8\uC9C0\uB9C9 \uC815\uC0C1\uAC12" : "") + `
\uAE30\uC900 ${timestamp} UTC \xB7 ${html(quote.provider)}`;
}
function rebalanceReply(rawAmount, snapshot, plans) {
  const raw = rawAmount.trim().replaceAll(",", "");
  let amount2;
  try {
    amount2 = raw === "" ? decimal("0") : decimal(raw);
  } catch {
    return "\uC0AC\uC6A9\uBC95: <code>/rebalance 500000</code> (\uC6D0\uD654 \uC2E0\uADDC \uC790\uAE08)";
  }
  if (compare(amount2, decimal("0")) < 0) return "\uC2E0\uADDC \uC790\uAE08\uC740 0 \uC774\uC0C1\uC774\uC5B4\uC57C \uD569\uB2C8\uB2E4.";
  const targets = plans.filter((plan) => plan.enabled && plan.targetWeightPct != null).map((plan) => ({ instrument: plan.instrument, targetWeightPct: plan.targetWeightPct }));
  if (!targets.length) return "\uBAA9\uD45C \uBE44\uC911\uC774 \uC788\uB294 \uC801\uB9BD \uACC4\uD68D\uC744 \uBA3C\uC800 \uB4F1\uB85D\uD574 \uC8FC\uC138\uC694.";
  return formatRebalance(suggestNewMoneyRebalance(snapshot, targets, raw || "0"));
}

// deploy/cloudflare-js/periodic-report.mjs
var EMPTY_RECURRING = "\u{1F331} <b>\uB9E4\uC77C\uBAA8\uC73C\uAE30</b>\n\n\uB4F1\uB85D\uB41C \uC885\uBAA9\uC774 \uC5C6\uC2B5\uB2C8\uB2E4.\n<code>/recurring_add AMD 20USD 8%</code>\uB85C \uB85C\uCEEC \uACC4\uD68D\uC744 \uB4F1\uB85D\uD560 \uC218 \uC788\uC2B5\uB2C8\uB2E4.";
function buildPeriodicReport(type, snapshot, refresh, plans = [], analyses = {}) {
  const assets = formatAssets(snapshot, refresh);
  if (type === "daily") return [
    "\u{1F4CA} <b>Daily Portfolio Report</b>",
    "",
    assets,
    "",
    "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501",
    "",
    plans.length ? formatRecurring(plans, analyses) : EMPTY_RECURRING,
    "",
    "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501",
    "",
    "\u26A0\uFE0F <b>\uC8FC\uC694 \uBCC0\uD654</b>",
    "",
    "\uC2E4\uC801\xB7\uB274\uC2A4 \uBCC0\uD654\uB294 \uD655\uC778\uB41C \uB370\uC774\uD130\uB9CC \uD45C\uC2DC\uD558\uBA70, \uB2E8\uAE30 \uAC00\uACA9 \uC6C0\uC9C1\uC784\uC740 Thesis \uBCC0\uD654\uB85C \uAC04\uC8FC\uD558\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4.",
    "",
    snapshot.isComplete ? "\uD2B9\uBCC4 \uB300\uC751 \uC5EC\uBD80\uB294 \uC704 \uC801\uB9BD \uC885\uBAA9\uBCC4 \uADFC\uAC70\uB97C \uD655\uC778\uD558\uC138\uC694." : "\uC624\uB298\uC740 \uACC4\uC88C \uB370\uC774\uD130\uAC00 \uBD88\uC644\uC804\uD574 Portfolio \uD310\uB2E8\uC744 \uBCF4\uB958\uD569\uB2C8\uB2E4."
  ].join("\n");
  if (type === "weekly") {
    const lines = [
      "\u{1F4C5} <b>Weekly Portfolio Report</b>",
      "",
      assets,
      "",
      "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501",
      "",
      formatPortfolio(snapshot),
      "",
      "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501",
      "",
      "\u{1F4C8} <b>\uC131\uACFC</b>",
      "1W/YTD \uC218\uC775\uB960\uC740 \uC644\uC804\uD55C \uACFC\uAC70 snapshot\uACFC \uC785\uCD9C\uAE08 \uC870\uC815 \uC774\uB825\uC774 \uC313\uC778 \uB4A4 \uC81C\uACF5\uD569\uB2C8\uB2E4.",
      "",
      "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501",
      "",
      "\u{1F9ED} <b>Thesis\xB7Valuation</b>"
    ];
    if (!plans.length) lines.push("", "\uB4F1\uB85D\uB41C \uC801\uB9BD \uC885\uBAA9 \uC5C6\uC74C");
    const decisions = /* @__PURE__ */ new Map([
      ["aggressive_buy", "\uC801\uADF9 \uC99D\uC561"],
      ["continue_buy", "\uC99D\uC561"],
      ["maintain", "\uC720\uC9C0"],
      ["reduce", "\uAC10\uC561"],
      ["review_stop", "\uC911\uB2E8 \uAC80\uD1A0"],
      ["review_required", "\uC911\uC694 \uACBD\uACE0 \uC7AC\uAC80\uD1A0"],
      ["unavailable", "Valuation \uB370\uC774\uD130 \uBD80\uC871\uC73C\uB85C \uD310\uB2E8 \uBCF4\uB958"]
    ]);
    for (const plan of plans) {
      const identity = `${plan.instrument.market}:${plan.instrument.ticker}`;
      const full = `${identity}:${plan.instrument.assetType}:${plan.instrument.currency}`;
      const analysis = analyses[full];
      if (!analysis) {
        lines.push("", `<b>${html(identity)}</b>`, "\uBD84\uC11D \uB370\uC774\uD130 \uBD80\uC871");
        continue;
      }
      if (analysis.companyScore.components.length < 5 || !decisions.has(analysis.finalScore.decision)) {
        throw new TypeError("incomplete weekly analysis");
      }
      const valuation = analysis.companyScore.components.find((component) => component.name === "valuation")?.score ?? null;
      const safety = analysis.companyScore.components[4].score;
      lines.push(
        "",
        `<b>${html(identity)}</b>`,
        `\uC8FC\uC2DD\uBD07 Valuation ${valuation === null ? "\uBCF4\uB958" : valuation}`,
        `\uACB0\uB860 ${decisions.get(analysis.finalScore.decision)}`,
        `\uC7A5\uAE30\uCD94\uC138 \uC801\uD569\uC131 ${analysis.recurringSuitability.score ?? "\uBCF4\uB958"}`
      );
      if (plan.investmentThesis) lines.push(`Thesis: ${html(plan.investmentThesis.slice(0, 240))}`);
      const v = analysis.valuation;
      if (v?.forwardPe != null) lines.push(`\uAE08\uC694\uC77C \uC885\uAC00 \uAE30\uC900 Forward PER ${fixed(v.forwardPe, 2)}x` + (v.historicalForwardPeMedian != null ? ` \xB7 \uACFC\uAC70 median ${fixed(v.historicalForwardPeMedian, 2)}x` : " \xB7 \uACFC\uAC70 median \uBBF8\uC81C\uACF5"));
      if (analysis.earnings) {
        const e = analysis.earnings;
        lines.push(`\uCD5C\uADFC \uC2E4\uC801 ${html(e.period || "\uD655\uC778 \uBD84\uAE30")}` + (e.revenueYoy != null ? ` \xB7 Revenue YoY ${compare(decimal(e.revenueYoy), decimal("0")) >= 0 ? "+" : ""}${fixed(exact(multiply(decimal(e.revenueYoy), decimal("100"))), 2)}%` : ""));
      }
      const news = (analysis.news || []).filter((item) => item.fundamentalImpact === "REVIEW_REQUIRED");
      if (news.length) lines.push(`Fundamental \uC601\uD5A5 \uC6D0\uBB38 \uD655\uC778 \uD544\uC694 \uB274\uC2A4 ${news.length}\uAC74: ` + html(news[0].title.slice(0, 160)));
      lines.push(`Safety ${safety ?? "\uBCF4\uB958"}`);
    }
    lines.push("", "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501", "", "\u2696\uFE0F <b>\uB2E4\uC74C \uC8FC \uBC30\uBD84</b>");
    const budget = plans.length ? weeklyBudget(plans, refresh.fxQuote?.rate ?? null) : null;
    if (budget === null) lines.push("\uC608\uC0C1 \uC2E0\uADDC \uD22C\uC790\uAE08\uC774 \uC124\uC815\uB418\uC9C0 \uC54A\uC544 \uBC30\uBD84\uC548\uC740 \uBCF4\uB958\uD569\uB2C8\uB2E4.");
    else if (!snapshot.isComplete) lines.push("\uACC4\uC88C \uB370\uC774\uD130\uAC00 \uBD88\uC644\uC804\uD574 \uC2E0\uADDC \uC790\uAE08 \uBC30\uBD84\uC548\uC744 \uACC4\uC0B0\uD558\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4.");
    else {
      if (snapshot.baseCurrency !== "KRW") {
        throw new Error("weekly KRW budget requires a KRW base-currency snapshot");
      }
      lines.push(`\uC608\uC0C1 \uC2E0\uADDC \uD22C\uC790\uAE08 ${display(budget, 0)}\uC6D0`);
      const targets = plans.filter((plan) => plan.targetWeightPct != null).map((plan) => ({ instrument: plan.instrument, targetWeightPct: plan.targetWeightPct }));
      try {
        const rebalance = suggestNewMoneyRebalance(snapshot, targets, exact(budget));
        for (const item of rebalance.items) if (compare(decimal(item.suggestedNewMoney), decimal("0")) > 0) {
          lines.push(`${html(`${item.instrument.market}:${item.instrument.ticker}`)} \u2192 ` + money(item.suggestedNewMoney, "KRW"));
        }
        if (compare(decimal(rebalance.unallocatedCash), decimal("0")) > 0) {
          lines.push("\uD604\uAE08 \uC720\uC9C0 \u2192 " + money(rebalance.unallocatedCash, "KRW"));
        }
      } catch (error) {
        if (!(error instanceof TypeError)) throw error;
        lines.push("\uBAA9\uD45C \uBE44\uC911 \uD569\uACC4\uAC00 \uC62C\uBC14\uB974\uC9C0 \uC54A\uC544 \uBC30\uBD84 \uACC4\uC0B0\uC744 \uBCF4\uB958\uD569\uB2C8\uB2E4.");
      }
    }
    lines.push("", "\uB9AC\uBC38\uB7F0\uC2F1\uC740 \uB9E4\uB3C4 \uB300\uC2E0 \uC2E0\uADDC \uC790\uAE08 \uBC30\uBD84 \uBCC0\uACBD\uC744 \uC6B0\uC120\uD569\uB2C8\uB2E4.");
    return lines.join("\n");
  }
  throw new TypeError("unsupported report type");
}

// deploy/cloudflare-js/command-router.mjs
var ALIASES = /* @__PURE__ */ new Map([
  ["start", "start"],
  ["help", "help"],
  ["\uB3C4\uC6C0\uB9D0", "help"],
  ["assets", "assets"],
  ["\uC790\uC0B0", "assets"],
  ["portfolio", "portfolio"],
  ["\uD3EC\uD2B8\uD3F4\uB9AC\uC624", "portfolio"],
  ["recurring", "recurring"],
  ["\uBAA8\uC73C\uAE30", "recurring"],
  ["recurring_add", "recurring_add"],
  ["\uBAA8\uC73C\uAE30\uCD94\uAC00", "recurring_add"],
  ["recurring_update", "recurring_update"],
  ["\uBAA8\uC73C\uAE30\uBCC0\uACBD", "recurring_update"],
  ["stock", "stock"],
  ["\uC885\uBAA9", "stock"],
  ["analyze", "analyze"],
  ["\uBD84\uC11D", "analyze"],
  ["thesis", "thesis"],
  ["rebalance", "rebalance"],
  ["\uB9AC\uBC38\uB7F0\uC2F1", "rebalance"],
  ["weekly", "weekly"],
  ["\uC8FC\uAC04\uB9AC\uD3EC\uD2B8", "weekly"]
]);
var KR_MARKETS = /* @__PURE__ */ new Set(["KOSPI", "KOSDAQ", "KRX", "NXT"]);
var FAILURE = "\uC694\uCCAD\uC744 \uCC98\uB9AC\uD558\uC9C0 \uBABB\uD588\uC2B5\uB2C8\uB2E4. \uC7A0\uC2DC \uD6C4 \uB2E4\uC2DC \uC2DC\uB3C4\uD574 \uC8FC\uC138\uC694.";
var USAGE_UPDATE = "\uC0AC\uC6A9\uBC95:\n<code>/\uBAA8\uC73C\uAE30\uBCC0\uACBD ETF \uC804\uBD80 +1000</code>\n<code>/\uBAA8\uC73C\uAE30\uBCC0\uACBD \uAC1C\uBCC4\uC8FC NVDA,AMD \uBE7C\uACE0 -1000</code>\n<code>/\uBAA8\uC73C\uAE30\uBCC0\uACBD ETF \uC804\uBD80 \uBAA8\uC73C\uAE30\uC911\uB2E8</code>";
function parse(text, botUsername) {
  if (typeof text !== "string") return null;
  const match2 = /^\/([^\s@]+)(?:@([A-Za-z0-9_]+))?(?:\s+([\s\S]*))?$/.exec(text.trim());
  if (!match2 || match2[2] && match2[2].toLowerCase() !== botUsername?.toLowerCase()) {
    return null;
  }
  return {
    command: ALIASES.get(match2[1]) || null,
    raw: (match2[3] || "").trim(),
    args: (match2[3] || "").trim().split(/\s+/).filter(Boolean)
  };
}
function requireGateway(gateway, method) {
  if (typeof gateway?.[method] !== "function") throw new Error(`gateway ${method} unavailable`);
}
function mutationAllowed(signal) {
  if (signal?.aborted) throw new DOMException("cancelled before mutation", "AbortError");
}
async function planAnalyses(gateway, plans, snapshot) {
  if (!gateway.analysisProvider || !plans.length) return {};
  requireGateway(gateway, "analyzePlans");
  return gateway.analyzePlans(plans, snapshot);
}
async function replyFor(parsed, gateway, updateId, signal, delivery) {
  const { command, raw, args } = parsed;
  if (command === "start") return START;
  if (command === "help") return HELP;
  if (["assets", "portfolio", "accounts"].includes(command)) {
    requireGateway(gateway, "refresh");
    const refresh = await gateway.refresh({ force: true, allBrokers: command === "accounts" });
    if (command === "assets") return formatAssets(refresh.snapshot, refresh);
    if (command === "portfolio") return formatPortfolio(refresh.snapshot);
    return formatAccounts(
      refresh.accountResults,
      refresh.collectedAt,
      refresh.snapshot.isComplete
    );
  }
  if (command === "stock") return stockReply(
    args[0] || "",
    gateway.marketProvider,
    gateway.lastSnapshot?.positions || []
  );
  if (command === "recurring") {
    requireGateway(gateway, "refresh");
    requireGateway(gateway, "listPlans");
    const [refresh, plans] = await Promise.all([gateway.refresh({
      force: false,
      allBrokers: false
    }), gateway.listPlans(true)]);
    const analyses = await planAnalyses(gateway, plans, refresh.snapshot);
    return formatRecurring(plans, analyses);
  }
  if (command === "rebalance") {
    requireGateway(gateway, "refresh");
    requireGateway(gateway, "listPlans");
    const [refresh, plans] = await Promise.all([gateway.refresh({
      force: false,
      allBrokers: false
    }), gateway.listPlans(true)]);
    return rebalanceReply(args[0] || "", refresh.snapshot, plans);
  }
  if (command === "weekly") {
    requireGateway(gateway, "refresh");
    requireGateway(gateway, "listPlans");
    const [refresh, plans] = await Promise.all([gateway.refresh({
      force: true,
      allBrokers: false
    }), gateway.listPlans(true)]);
    const analyses = await planAnalyses(gateway, plans, refresh.snapshot);
    return buildPeriodicReport("weekly", refresh.snapshot, refresh, plans, analyses);
  }
  if (command === "analyze") {
    if (!args.length) return "\uC0AC\uC6A9\uBC95: <code>/analyze AMD</code>";
    if (!gateway.analysisProvider) return "\uC2DC\uC7A5\uB370\uC774\uD130 provider\uAC00 \uC124\uC815\uB418\uC9C0 \uC54A\uC544 \uBD84\uC11D\uC744 \uBCF4\uB958\uD569\uB2C8\uB2E4.";
    requireGateway(gateway, "refresh");
    requireGateway(gateway, "listPlans");
    const [market, ticker] = instrumentParts(args[0], gateway.lastSnapshot?.positions || []);
    const [refresh, plans] = await Promise.all([gateway.refresh({
      force: false,
      allBrokers: false
    }), gateway.listPlans(false)]);
    const target = plans.find((plan) => plan.instrument.ticker === ticker && (plan.instrument.market === market || plan.instrument.market === "US"));
    return formatAnalysis(await analyzeWithProvider(
      gateway.analysisProvider,
      {
        market,
        ticker,
        snapshot: refresh.snapshot,
        targetWeightPct: target?.targetWeightPct ?? null
      }
    ));
  }
  if (command === "recurring_add") {
    if (args.length < 3) return "\uC0AC\uC6A9\uBC95: <code>/recurring_add AMD 20USD 8%</code>";
    const [market, ticker] = instrumentParts(args[0], gateway.lastSnapshot?.positions || []);
    const amountMatch = /^(\d+(?:\.\d+)?)(USD|KRW)$/i.exec(args[1]);
    const weightMatch = /^(\d+(?:\.\d+)?)%$/.exec(args[2]);
    if (!amountMatch || !weightMatch) {
      return "\uAE08\uC561\uC740 20USD/10000KRW, \uBAA9\uD45C\uB294 8% \uD615\uC2DD\uC774\uC5B4\uC57C \uD569\uB2C8\uB2E4.";
    }
    const currency2 = amountMatch[2].toUpperCase();
    if (compare(decimal(amountMatch[1]), decimal("0")) <= 0 || compare(decimal(weightMatch[1]), decimal("100")) > 0) {
      return "\uAE08\uC561\uC740 0\uBCF4\uB2E4 \uCEE4\uC57C \uD558\uACE0 \uBAA9\uD45C \uBE44\uC911\uC740 0~100%\uC5EC\uC57C \uD569\uB2C8\uB2E4.";
    }
    if (currency2 === "KRW" && !KR_MARKETS.has(market)) {
      return "\uD574\uC678 \uC885\uBAA9 \uC801\uB9BD\uAE08\uC740 USD\uB85C \uC785\uB825\uD574 \uC8FC\uC138\uC694.";
    }
    if (currency2 === "USD" && KR_MARKETS.has(market)) {
      return "\uD55C\uAD6D \uC885\uBAA9 \uC801\uB9BD\uAE08\uC740 KRW\uB85C \uC785\uB825\uD574 \uC8FC\uC138\uC694.";
    }
    requireGateway(gateway, "createPlan");
    mutationAllowed(signal);
    if (delivery) delivery.mutationAttempted = true;
    const render = (plan2) => `\u2705 <b>${html(`${plan2.instrument.market}:${plan2.instrument.ticker}`)}</b> \uB85C\uCEEC \uC801\uB9BD \uACC4\uD68D\uC744 \uC800\uC7A5\uD588\uC2B5\uB2C8\uB2E4.
\uB9E4\uC77C ${money(plan2.dailyAmount, plan2.currency)} \xB7 \uBAA9\uD45C ${plan2.targetWeightPct}%

\uC8FC\uBB38\uC740 \uC2E4\uD589\uB418\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4.`;
    const plan = await gateway.createPlan({
      market,
      ticker,
      amount: amountMatch[1],
      currency: currency2,
      targetWeightPct: weightMatch[1],
      memo: args.slice(3).join(" "),
      mutationKey: `update:${updateId}`,
      delivery: delivery && { ...delivery, render }
    });
    return render(plan);
  }
  if (command === "recurring_update") {
    if (args.length < 2) return USAGE_UPDATE;
    const group = args[0].toUpperCase();
    const assetType = group === "ETF" ? "ETF" : ["\uAC1C\uBCC4\uC8FC", "\uC8FC\uC2DD", "EQUITY"].includes(group) ? "EQUITY" : null;
    if (!assetType) return "\uCCAB \uD56D\uBAA9\uC740 ETF \uB610\uB294 \uAC1C\uBCC4\uC8FC\uC5EC\uC57C \uD569\uB2C8\uB2E4.\n" + USAGE_UPDATE;
    const text = args.slice(1).join(" ");
    const stop = /(?:모으기)?(?:중단|정지)$/.test(text);
    const delta = stop ? null : /([+-]\d[\d,]*)(?:원)?$/.exec(text)?.[1]?.replaceAll(",", "");
    if (!stop && delta == null) return USAGE_UPDATE;
    if (!stop && (compare(decimal(delta), decimal("0")) === 0 || decimal(delta).coefficient % 1000n !== 0n)) {
      return "\uBCC0\uACBD \uAE08\uC561\uC740 1,000\uC6D0 \uB2E8\uC704\uC758 0\uC774 \uC544\uB2CC \uAC12\uC774\uC5B4\uC57C \uD569\uB2C8\uB2E4.";
    }
    const excluded = text.includes("\uBE7C\uACE0") ? text.split("\uBE7C\uACE0")[0].replaceAll("\uC804\uBD80", "").split(/[\s,]+/).filter(Boolean).map((value) => value.toUpperCase()) : [];
    requireGateway(gateway, "changeGroup");
    mutationAllowed(signal);
    if (delivery) delivery.mutationAttempted = true;
    const render = (changed2) => {
      if (!changed2.length) return "\uC870\uAC74\uC5D0 \uB9DE\uB294 \uBCC0\uACBD \uB300\uC0C1\uC774 \uC5C6\uC2B5\uB2C8\uB2E4.";
      const action = stop ? "\uBAA8\uC73C\uAE30 \uC911\uB2E8" : `${compare(decimal(delta), decimal("0")) > 0 ? "+" : ""}${display(decimal(delta), 0)}\uC6D0 \uBCC0\uACBD`;
      const details = [...changed2].sort((a, b) => a.instrument.ticker.localeCompare(b.instrument.ticker)).map((plan) => `${plan.instrument.ticker}(${money(plan.dailyAmount, plan.currency)})`).join(", ");
      return `\u2705 <b>${assetType === "ETF" ? "ETF" : "\uAC1C\uBCC4\uC8FC"} ${changed2.length}\uC885\uBAA9</b> ${action}
${html(details)}

\uBD07\uC758 \uBAA8\uC73C\uAE30 \uACC4\uD68D\uB9CC \uBCC0\uACBD\uD588\uC73C\uBA70 \uC99D\uAD8C\uC0AC \uC8FC\uBB38\uC740 \uC2E4\uD589\uD558\uC9C0 \uC54A\uC558\uC2B5\uB2C8\uB2E4.`;
    };
    const changed = await gateway.changeGroup({
      assetType,
      delta,
      stop,
      excluded,
      mutationKey: `update:${updateId}`,
      delivery: delivery && { ...delivery, render }
    });
    return render(changed);
  }
  if (command === "thesis") {
    if (args.length < 2) return "\uC0AC\uC6A9\uBC95: <code>/thesis AMD \uD22C\uC790\uB17C\uB9AC...</code>";
    const [market, ticker] = instrumentParts(args[0], gateway.lastSnapshot?.positions || []);
    const thesis = args.slice(1).join(" ").trim();
    if (thesis.length > 4e3) return "Thesis\uB294 4,000\uC790 \uC774\uB0B4\uB85C \uC785\uB825\uD574 \uC8FC\uC138\uC694.";
    requireGateway(gateway, "setThesis");
    mutationAllowed(signal);
    if (delivery) delivery.mutationAttempted = true;
    const render = (saved2) => `\u{1F4DD} <b>${html(saved2.market)}:${html(saved2.ticker)}</b> Thesis revision\uC744 \uC800\uC7A5\uD588\uC2B5\uB2C8\uB2E4.`;
    const saved = await gateway.setThesis({
      market,
      ticker,
      thesis,
      mutationKey: `update:${updateId}`,
      delivery: delivery && { ...delivery, render }
    });
    return render(saved);
  }
  return null;
}
async function routeUpdate({
  headerEntries,
  body,
  secret,
  allowedUserIds,
  botUsername = null,
  gateway,
  signal,
  atomicDelivery = false,
  now = () => (/* @__PURE__ */ new Date()).toISOString()
}) {
  const updateId = admittedUpdateId(headerEntries, body, secret, allowedUserIds);
  if (updateId === null) return { status: "DENIED", updateId: null, reply: null };
  const update = JSON.parse(new TextDecoder().decode(body));
  const parsed = parse(update.message?.text || "", botUsername);
  if (!parsed?.command) return { status: "IGNORED", updateId, reply: null };
  const delivery = atomicDelivery ? {
    updateId,
    destination: String(update.message.chat.id),
    now: now()
  } : null;
  try {
    const reply = await replyFor(parsed, gateway, updateId, signal, delivery);
    return {
      status: reply === null ? "IGNORED" : "READY",
      updateId,
      reply,
      ...atomicDelivery ? { mutationAttempted: Boolean(delivery.mutationAttempted) } : {}
    };
  } catch (error) {
    if (error?.name === "AbortError") return { status: "CANCELLED", updateId, reply: null };
    return { status: "ERROR", updateId, reply: FAILURE };
  }
}

// deploy/cloudflare-js/synthetic-combined-harness.mjs
var CASES = { small: 1, representative: 5, large: 40 };
var FIXED_AT = "2026-02-02T01:00:00Z";
var SECRET = "synthetic-test-only";
var accountFor = (name) => `fixture_v2_${name}_${"d".repeat(64)}`;
var mutationTicker = (name) => name === "small" ? "999991" : "999995";
var HEADER2 = [["X-Telegram-Bot-Api-Secret-Token", SECRET]];
var keyFor = (name) => `commands-v2:${name}`;
var diagnosticsDDL = `CREATE TABLE IF NOT EXISTS cloud_test_diagnostics (
  run_key TEXT PRIMARY KEY, stage TEXT NOT NULL, code TEXT NOT NULL)`;
var symbol = (index) => `9${String(index).padStart(5, "0")}`;
var instrument2 = (index) => ({
  market: "KOSPI",
  ticker: symbol(index),
  assetType: "EQUITY",
  currency: "KRW"
});
function fixtures(size) {
  const source = {
    name: "kiwoom_main",
    getPositions: async () => ({
      broker: "kiwoom_main",
      status: "ok",
      items: Array.from({ length: size }, (_, index) => ({
        broker: "kiwoom_main",
        accountId: "fixture-account",
        instrument: instrument2(index),
        quantity: "1",
        costBasis: "1000",
        marketValue: "1000"
      }))
    }),
    getCash: async () => ({ broker: "kiwoom_main", status: "ok", items: [] }),
    getAccounts: async () => ({ broker: "kiwoom_main", status: "ok", items: [] })
  };
  const market = {
    getPrice: async (marketName, ticker) => ({
      market: marketName,
      ticker,
      currency: "KRW",
      price: "1000",
      asOf: FIXED_AT,
      provider: "fixture",
      stale: false
    }),
    getFundamentals: async () => ({
      asOf: FIXED_AT,
      provider: "fixture",
      revenueGrowth: "0.12",
      earningsGrowth: "0.20",
      grossMargin: "0.4",
      operatingMargin: "0.2",
      freeCashFlow: "100",
      returnOnEquity: "0.18",
      totalCash: "200",
      totalDebt: "300",
      debtToEquity: "75"
    }),
    getValuation: async () => ({
      asOf: FIXED_AT,
      forwardPe: "24",
      peg: "1.6",
      enterpriseToEbitda: "18",
      freeCashFlowYield: "0.04",
      historicalForwardPeMedian: "22",
      dilutionPct: "2"
    }),
    getEarnings: async () => ({ period: "Q1", revenue: "1000", revenueYoy: "0.1" }),
    getNews: async () => [],
    getTrend: async () => ({
      return1y: "0.15",
      cagr3y: "0.1",
      maxDrawdown3y: "-0.3",
      annualizedVolatility1y: "0.25",
      above200dAverage: true
    })
  };
  const externalRecurringPlans = async () => Array.from({ length: size }, (_, index) => ({
    instrument: instrument2(index),
    broker: "meritz",
    currency: "KRW",
    dailyAmount: "1000",
    targetWeightPct: "1",
    enabled: true,
    scheduledWeekdays: [0, 1, 2, 3, 4],
    memo: "",
    investmentThesis: ""
  }));
  return { source, market, externalRecurringPlans };
}
function updateBody(id, command) {
  return new TextEncoder().encode(JSON.stringify({
    update_id: id,
    message: { from: { id: 42 }, chat: { id: 42, type: "private" }, text: command }
  }));
}
async function commandCase(db, name) {
  await initSyntheticTables(db);
  await db.prepare(diagnosticsDDL).run();
  const key2 = keyFor(name);
  const claim = await db.prepare(`INSERT INTO cloud_test_runs (run_key, status)
    VALUES (?, 'RUNNING') ON CONFLICT(run_key) DO NOTHING`).bind(key2).run();
  if (claim.meta.changes !== 1) return;
  let stage2 = "account_import";
  try {
    const size = CASES[name];
    const account = accountFor(name);
    await importAccount(db, {
      fingerprint: account,
      broker: "synthetic",
      baseCurrency: "KRW"
    });
    const { source, market, externalRecurringPlans } = fixtures(size);
    const gateway = new SyntheticCloudGateway({
      db,
      providers: [source],
      marketProvider: market,
      externalRecurringPlans,
      scopeAccountFingerprint: account,
      now: () => FIXED_AT
    });
    const edited = mutationTicker(name);
    const commands = name === "large" ? ["/recurring", "/analyze KOSPI:900000", "/weekly"] : [
      "/start",
      "/help",
      "/assets",
      "/portfolio",
      `/recurring_add KOSPI:${edited} 2000KRW 8%`,
      "/recurring_update \uAC1C\uBCC4\uC8FC \uC804\uBD80 +1000",
      "/stock KOSPI:900000",
      `/analyze KOSPI:${edited}`,
      `/thesis KOSPI:${edited} synthetic thesis`,
      "/recurring",
      "/rebalance 1000",
      "/weekly"
    ];
    const base = name === "small" ? 91e4 : name === "representative" ? 92e4 : 93e4;
    let passed = true;
    let failure = null;
    for (const [index, command] of commands.entries()) {
      stage2 = `command_${index}`;
      const result2 = await routeUpdate({
        headerEntries: HEADER2,
        body: updateBody(base + index, command),
        secret: SECRET,
        allowedUserIds: [42],
        gateway
      });
      if (result2.status !== "READY" || !result2.reply) {
        passed = false;
        failure ??= { stage: stage2, code: "COMMAND_NOT_READY" };
      }
      if (index === 4 && name !== "large" && result2.status === "READY") {
        stage2 = "outbox_4";
        const first = await enqueueUpdate(
          db,
          base + index,
          "synthetic-only",
          JSON.stringify({ reply: result2.reply }),
          FIXED_AT
        );
        const replay = await enqueueUpdate(
          db,
          base + index,
          "synthetic-only",
          JSON.stringify({ reply: result2.reply }),
          FIXED_AT
        );
        if (!first || replay) {
          passed = false;
          failure ??= { stage: stage2, code: "OUTBOX_ASSERTION" };
        }
      }
    }
    stage2 = "finalize";
    if (failure) await db.prepare(`INSERT INTO cloud_test_diagnostics
      (run_key, stage, code) VALUES (?, ?, ?)`).bind(key2, failure.stage, failure.code).run();
    await db.prepare(`UPDATE cloud_test_runs SET status = ?, completed_at = ?
      WHERE run_key = ? AND status = 'RUNNING'`).bind(
      passed ? "PASS" : "FAIL",
      (/* @__PURE__ */ new Date()).toISOString(),
      key2
    ).run();
  } catch (error) {
    const code = error?.code === "ERR_SQLITE_ERROR" ? "SQL_ERROR" : "EXCEPTION";
    await db.prepare(`INSERT INTO cloud_test_diagnostics (run_key, stage, code)
      VALUES (?, ?, ?) ON CONFLICT(run_key) DO NOTHING`).bind(key2, stage2, code).run();
    await db.prepare(`UPDATE cloud_test_runs SET status = 'FAIL', completed_at = ?
      WHERE run_key = ? AND status = 'RUNNING'`).bind((/* @__PURE__ */ new Date()).toISOString(), key2).run();
  }
}
var synthetic_combined_harness_default = {
  async fetch(request, env) {
    if (!Object.hasOwn(CASES, env.TEST_CASE)) return synthetic_harness_default.fetch(request, env);
    if (request.method !== "GET" || new URL(request.url).pathname !== "/health") {
      return new Response("not found", { status: 404 });
    }
    if (!env.DB) return Response.json({ status: "UNBOUND" }, { status: 503 });
    try {
      const row = await env.DB.prepare(`SELECT status FROM cloud_test_runs
        WHERE run_key = ?`).bind(keyFor(env.TEST_CASE)).first();
      const diagnostic = row?.status === "FAIL" ? await env.DB.prepare(
        `SELECT stage, code FROM cloud_test_diagnostics WHERE run_key = ?`
      ).bind(keyFor(env.TEST_CASE)).first() : null;
      return Response.json(
        {
          status: row?.status || "NOT_RUN",
          ...diagnostic ? { diagnostic } : {}
        },
        { headers: { "Cache-Control": "no-store" } }
      );
    } catch {
      return Response.json(
        { status: "NOT_RUN" },
        { headers: { "Cache-Control": "no-store" } }
      );
    }
  },
  async scheduled(event, env) {
    if (env.TEST_WRITES_ENABLED !== "1" || !env.DB) return;
    if (Object.hasOwn(CASES, env.TEST_CASE)) await commandCase(env.DB, env.TEST_CASE);
    else if (env.TEST_CASE === void 0 || env.TEST_CASE === "core") {
      await synthetic_harness_default.scheduled(event, env);
    }
  }
};
export {
  synthetic_combined_harness_default as default
};
