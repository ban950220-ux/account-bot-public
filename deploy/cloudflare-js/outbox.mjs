// D1 binding protocol only. Test with a disposable local SQLite implementation.
function changes(result) {
  const count = result?.meta?.changes;
  if (!Number.isSafeInteger(count) || count < 0) throw new TypeError('invalid D1 change count');
  return count;
}

function stage(db, key, destination, payload, now, table, column, claimKey, nonce) {
  return db.prepare(`INSERT INTO cloud_outbox (event_key, destination_key, payload_json, created_at)
    SELECT ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM ${table} WHERE ${column} = ? AND claim_nonce = ?)`)
    .bind(key, destination, payload, now, claimKey, nonce);
}

export function mutationDeliveryStatements(db, delivery, evidence) {
  if (!delivery) return [];
  const { updateId, destination, reply, now } = delivery;
  if (!Number.isSafeInteger(updateId) || updateId <= 0 ||
      !/^[1-9]\d*$/.test(destination) || typeof reply !== 'string' ||
      !reply || reply.length > 3900 || typeof now !== 'string' || !now) {
    throw new TypeError('invalid mutation delivery');
  }
  const selectors = {
    plan: [`cloud_plan_revisions`, `plan_key = ? AND mutation_key = ?`],
    group: [`cloud_mutation_checks`, `mutation_key = ?`],
    thesis: [`cloud_thesis_revisions`, `instrument_key = ? AND mutation_key = ?`],
    policy: [`cloud_policy_revisions`, `mutation_key = ?`],
  };
  const selected = selectors[evidence?.kind];
  if (!selected || !Array.isArray(evidence.values)) throw new TypeError('invalid mutation evidence');
  const nonce = crypto.randomUUID();
  return [db.prepare(`INSERT INTO cloud_update_claims (update_id, received_at, claim_nonce)
    VALUES (?, ?, ?)`).bind(updateId, now, nonce),
  db.prepare(`INSERT INTO cloud_outbox
    (event_key, destination_key, payload_json, created_at) VALUES (?, ?,
      CASE WHEN EXISTS (SELECT 1 FROM ${selected[0]} WHERE ${selected[1]}) AND
        EXISTS (SELECT 1 FROM cloud_update_claims WHERE update_id = ? AND claim_nonce = ?)
      THEN ? ELSE NULL END, ?)`).bind(`update:${updateId}:0`, destination,
    ...evidence.values, updateId, nonce, JSON.stringify({ text: reply }), now)];
}

export async function enqueueUpdate(db, updateId, destination, payload, now) {
  if (!Number.isSafeInteger(updateId) || updateId <= 0 || !destination || !payload || !now) {
    throw new TypeError('invalid update claim');
  }
  const nonce = crypto.randomUUID();
  const results = await db.batch([
    db.prepare(`INSERT INTO cloud_update_claims (update_id, received_at, claim_nonce)
      VALUES (?, ?, ?) ON CONFLICT(update_id) DO NOTHING`).bind(updateId, now, nonce),
    stage(db, `update:${updateId}:0`, destination, payload, now,
      'cloud_update_claims', 'update_id', updateId, nonce),
  ]);
  if (results.length !== 2 || changes(results[0]) !== changes(results[1]) ||
      ![0, 1].includes(changes(results[0]))) throw new Error('claim and outbox disagree');
  return changes(results[0]) === 1;
}

export async function enqueueUpdateChunks(db, updateId, destination, chunks, now) {
  if (!Number.isSafeInteger(updateId) || updateId <= 0 ||
      !/^[1-9]\d*$/.test(destination) || !Array.isArray(chunks) ||
      !chunks.length || chunks.length > 32 ||
      chunks.some(chunk => typeof chunk !== 'string' || !chunk || chunk.length > 3900) || !now) {
    throw new TypeError('invalid update chunks');
  }
  const nonce = crypto.randomUUID();
  const statements = [db.prepare(`INSERT INTO cloud_update_claims
    (update_id, received_at, claim_nonce) VALUES (?, ?, ?)
    ON CONFLICT(update_id) DO NOTHING`).bind(updateId, now, nonce)];
  chunks.forEach((chunk, index) => statements.push(stage(db, `update:${updateId}:${index}`,
    destination, JSON.stringify({ text: chunk }), now,
    'cloud_update_claims', 'update_id', updateId, nonce)));
  const results = await db.batch(statements);
  const count = changes(results[0]);
  if (![0, 1].includes(count) || results.some(result => changes(result) !== count)) {
    throw new Error('update chunks and claim disagree');
  }
  return count === 1;
}

export async function enqueueReport(db, periodKey, deliveries, now) {
  if (!periodKey || !now || !deliveries.length ||
      deliveries.some(([destination, payload]) => !destination || !payload) ||
      new Set(deliveries.map(([destination]) => destination)).size !== deliveries.length) {
    throw new TypeError('invalid report claim');
  }
  const nonce = crypto.randomUUID();
  const statements = [db.prepare(`INSERT INTO cloud_report_claims (period_key, claimed_at, claim_nonce)
    VALUES (?, ?, ?) ON CONFLICT(period_key) DO NOTHING`).bind(periodKey, now, nonce)];
  deliveries.forEach(([destination, payload], index) => statements.push(
    stage(db, `report:${periodKey}:${index}`, destination, payload, now,
      'cloud_report_claims', 'period_key', periodKey, nonce),
  ));
  const results = await db.batch(statements);
  if (results.length !== statements.length || ![0, 1].includes(changes(results[0])) ||
      results.some(result => changes(result) !== changes(results[0]))) {
    throw new Error('report claim and outbox disagree');
  }
  return changes(results[0]) === 1;
}

export async function enqueueReportChunks(db, periodKey, destination, chunks, now) {
  if (!/^[A-Za-z0-9._:-]{1,128}$/.test(periodKey) ||
      !/^[1-9]\d*$/.test(destination) || !Array.isArray(chunks) ||
      !chunks.length || chunks.length > 32 ||
      chunks.some(chunk => typeof chunk !== 'string' || !chunk || chunk.length > 3900) || !now) {
    throw new TypeError('invalid report chunks');
  }
  const nonce = crypto.randomUUID();
  const statements = [db.prepare(`INSERT INTO cloud_report_claims
    (period_key, claimed_at, claim_nonce) VALUES (?, ?, ?)
    ON CONFLICT(period_key) DO NOTHING`).bind(periodKey, now, nonce)];
  chunks.forEach((chunk, index) => statements.push(stage(db, `report:${periodKey}:${index}`,
    destination, JSON.stringify({ text: chunk }), now,
    'cloud_report_claims', 'period_key', periodKey, nonce)));
  const results = await db.batch(statements);
  const count = changes(results[0]);
  if (![0, 1].includes(count) || results.some(result => changes(result) !== count)) {
    throw new Error('report chunks and claim disagree');
  }
  return count === 1;
}

export async function beginSend(db, key) {
  if (!key) throw new TypeError('empty event key');
  const result = await db.prepare(`UPDATE cloud_outbox SET status = 'SENDING'
    WHERE event_key = ? AND status = 'PENDING'`).bind(key).run();
  return changes(result) === 1;
}

export async function finishSend(db, key, sent, now) {
  if (!key || !now) throw new TypeError('missing send result');
  const result = await db.prepare(`UPDATE cloud_outbox SET status = ?, finished_at = ?
    WHERE event_key = ? AND status = 'SENDING'`)
    .bind(sent ? 'SENT' : 'UNKNOWN', now, key).run();
  return changes(result) === 1;
}
