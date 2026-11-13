// D1 optimistic store for the owner's allocation policy. Append-only revisions.
import { normalizePolicy } from './allocation-policy.mjs';
import { mutationDeliveryStatements } from './outbox.mjs';

const MUTATION = /^[A-Za-z0-9._:-]{1,128}$/;

function changes(result) {
  if (!Number.isSafeInteger(result?.meta?.changes)) throw new TypeError('invalid D1 result');
  return result.meta.changes;
}

export async function currentPolicy(db) {
  let row;
  try {
    row = await db.prepare('SELECT version, policy_json FROM cloud_policy WHERE singleton = 1').first();
  } catch (error) {
    // The Worker may be deployed before the additive tables exist. That means
    // "feature not installed", which reads as no policy; other errors propagate.
    if (/no such table: cloud_policy/i.test(String(error?.message))) {
      return { version: 0, policy: normalizePolicy({}), installed: false };
    }
    throw error;
  }
  if (!row) return { version: 0, policy: normalizePolicy({}), installed: true };
  return { version: row.version, policy: normalizePolicy(JSON.parse(row.policy_json)),
    installed: true };
}

export async function savePolicy(db, { policy, expectedVersion, mutationKey, effectiveAt,
  delivery = null }) {
  if (!MUTATION.test(mutationKey || '') || !Number.isSafeInteger(expectedVersion) ||
      expectedVersion < 0 || typeof effectiveAt !== 'string' || !effectiveAt) {
    throw new TypeError('invalid policy revision');
  }
  const json = JSON.stringify(normalizePolicy(policy));
  const deliveryStatements = mutationDeliveryStatements(db, delivery,
    { kind: 'policy', values: [mutationKey] });
  const statements = [];
  if (delivery) statements.push(deliveryStatements[0]);
  if (expectedVersion === 0) statements.push(db.prepare(`INSERT INTO cloud_policy
    (singleton, version, policy_json) VALUES (1, 0, '{}') ON CONFLICT(singleton) DO NOTHING`).bind());
  statements.push(db.prepare(`UPDATE cloud_policy SET version = version + 1,
    policy_json = ?, last_mutation_key = ? WHERE singleton = 1 AND version = ?
      AND NOT EXISTS (SELECT 1 FROM cloud_policy_revisions WHERE mutation_key = ?)`)
    .bind(json, mutationKey, expectedVersion, mutationKey));
  statements.push(db.prepare(`INSERT INTO cloud_policy_revisions
    (revision_no, policy_json, effective_at, mutation_key)
    SELECT version, policy_json, ?, ? FROM cloud_policy
    WHERE singleton = 1 AND version = ? AND last_mutation_key = ?
      AND NOT EXISTS (SELECT 1 FROM cloud_policy_revisions WHERE mutation_key = ?)`)
    .bind(effectiveAt, mutationKey, expectedVersion + 1, mutationKey, mutationKey));
  if (delivery) statements.push(deliveryStatements[1]);
  const results = await db.batch(statements);
  const updated = changes(results.at(delivery ? -3 : -2));
  const inserted = changes(results.at(delivery ? -2 : -1));
  if (updated !== inserted || ![0, 1].includes(updated)) {
    throw new Error('policy revision transaction mismatch');
  }
  if (delivery && (changes(results[0]) !== updated || changes(results.at(-1)) !== updated)) {
    throw new Error('policy delivery transaction mismatch');
  }
  return updated ? 'CHANGED' : 'CONFLICT';
}
