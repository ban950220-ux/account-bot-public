// D1 cache for normalized public observations. The caller owns the provider.
// A stale result is explicit; it must never be silently used as fresh data.
const ID = /^[A-Za-z][A-Za-z0-9_.:-]{0,47}$/;
const KEY = /^[A-Za-z0-9_.:/-]{1,160}$/;

function validateKey(provider, namespace, cacheKey) {
  if (!ID.test(provider) || !ID.test(namespace) || !KEY.test(cacheKey)) {
    throw new TypeError('invalid public market cache key');
  }
}

function instant(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T/.test(value) ||
      !/(Z|[+-]\d{2}:\d{2})$/.test(value)) throw new TypeError('invalid UTC timestamp');
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) throw new TypeError('invalid UTC timestamp');
  return date.toISOString();
}

function publicValue(value) {
  if (!value || Array.isArray(value) || typeof value !== 'object') {
    throw new TypeError('normalized public observation required');
  }
  const encoded = JSON.stringify(value);
  if (!encoded || encoded.length > 32768 ||
      /"(?:account|accountId|accountSeq|token|secret|password|holdings|positions|transactions)"\s*:/i.test(encoded)) {
    throw new TypeError('private or oversized market cache value');
  }
  return encoded;
}

function putStatement(db, { provider, namespace, cacheKey, value,
  fetchedAt, expiresAt, staleUntil = null }) {
  validateKey(provider, namespace, cacheKey);
  const fetched = instant(fetchedAt);
  const expires = instant(expiresAt);
  const stale = staleUntil === null ? null : instant(staleUntil);
  if (expires <= fetched || (stale !== null && stale < expires)) {
    throw new TypeError('invalid cache validity window');
  }
  const encoded = publicValue(value);
  return db.prepare(`INSERT INTO cloud_market_cache
    (provider, namespace, cache_key, normalized_json, fetched_at, expires_at, stale_until)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (provider, namespace, cache_key) DO UPDATE SET
      normalized_json = excluded.normalized_json, fetched_at = excluded.fetched_at,
      expires_at = excluded.expires_at, stale_until = excluded.stale_until
    WHERE excluded.fetched_at >= cloud_market_cache.fetched_at`).bind(
    provider, namespace, cacheKey, encoded, fetched, expires, stale);
}

function getStatement(db, { provider, namespace, cacheKey }) {
  validateKey(provider, namespace, cacheKey);
  return db.prepare(`SELECT normalized_json, fetched_at, expires_at, stale_until
    FROM cloud_market_cache WHERE provider = ? AND namespace = ? AND cache_key = ?`).bind(
    provider, namespace, cacheKey);
}

function observation(row, { now, allowStale = false }) {
  const current = instant(now);
  if (!row) return null;
  const stale = current > row.expires_at;
  if (stale && (!allowStale || row.stale_until === null || current > row.stale_until)) return null;
  return { value: JSON.parse(row.normalized_json), fetchedAt: row.fetched_at,
    expiresAt: row.expires_at, isStale: stale };
}

export async function putMarketCache(db, entry) {
  await putStatement(db, entry).run();
}

export async function putMarketCaches(db, entries) {
  if (entries.length) await db.batch(entries.map(entry => putStatement(db, entry)));
}

export async function getMarketCache(db, options) {
  instant(options.now);
  return observation(await getStatement(db, options).first(), options);
}

export async function getMarketCaches(db, options) {
  if (!options.length) return [];
  // Validate all inputs before submitting the batch. Each statement has three bindings.
  for (const item of options) instant(item.now);
  const results = await db.batch(options.map(item => getStatement(db, item)));
  return results.map((result, index) => observation(result.results[0], options[index]));
}
