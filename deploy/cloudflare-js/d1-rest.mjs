// D1 over the Cloudflare REST API, shaped like the Worker binding, for the
// Actions precompute job. It may read anything, but may write only derived data:
// precomputed replies and the public market/signal cache. Any other statement is
// refused before it is sent, so the job can never change plans, policy,
// cutover, claims or delivery state.
const WRITABLE = new Set(['cloud_precomputed', 'cloud_market_cache']);
const WRITE_WORDS = /\b(INSERT|UPDATE|DELETE|REPLACE|UPSERT|CREATE|DROP|ALTER|ATTACH|DETACH|PRAGMA|VACUUM|REINDEX|ANALYZE|SAVEPOINT|RELEASE|BEGIN|COMMIT|ROLLBACK)\b/i;

function stripLiterals(sql) {
  return sql.replace(/'(?:[^']|'')*'/g, "''").replace(/--[^\n]*/g, ' ').replace(/\/\*[\s\S]*?\*\//g, ' ');
}

export function statementKind(sql) {
  if (typeof sql !== 'string' || !sql.trim()) throw new TypeError('empty SQL');
  const text = stripLiterals(sql).trim();
  if (text.replace(/;\s*$/, '').includes(';')) throw new TypeError('one statement per call');
  if (/^(SELECT|WITH)\b/i.test(text) && !WRITE_WORDS.test(text)) return 'read';
  const insert = /^INSERT\s+INTO\s+([a-z_]+)\b/i.exec(text);
  if (insert && WRITABLE.has(insert[1].toLowerCase())) {
    const rest = text.slice(insert[0].length)
      .replace(/ON\s+CONFLICT\s*\([^)]*\)\s*DO\s+UPDATE\s+SET/gi, ' ');
    if (!WRITE_WORDS.test(rest)) return 'write';
  }
  throw new TypeError('statement not allowed for the precompute job');
}

function param(value) {
  if (value === undefined) throw new TypeError('undefined D1 parameter');
  if (typeof value === 'boolean') return value ? 1 : 0;
  if (value === null || typeof value === 'number' || typeof value === 'string') return value;
  throw new TypeError('unsupported D1 parameter');
}

export function createD1Rest({ accountId, databaseId, token, fetcher = fetch,
  host = 'https://api.cloudflare.com' }) {
  if (!/^[0-9a-f]{32}$/.test(accountId || '') || !/^[0-9a-f-]{36}$/.test(databaseId || '') ||
      typeof token !== 'string' || !token || typeof fetcher !== 'function') {
    throw new TypeError('D1 REST binding unavailable');
  }
  const url = `${host}/client/v4/accounts/${accountId}/d1/database/${databaseId}/query`;
  let queries = 0;
  async function query(sql, params) {
    queries += 1;
    const response = await fetcher(url, { method: 'POST',
      headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
      body: JSON.stringify({ sql, params }) });
    let payload = null;
    try { payload = await response.json(); } catch { /* reported below */ }
    const result = payload?.result?.[0];
    if (!response.ok || payload?.success !== true || result?.success === false) {
      // Never echo SQL text or parameters: they can carry plan data.
      const code = payload?.errors?.[0]?.code;
      throw new Error(`D1 REST query failed (HTTP ${response.status}${code ? `, code ${code}` : ''})`);
    }
    return { results: result?.results ?? [], success: true, meta: result?.meta ?? {} };
  }
  function statement(sql, values) {
    statementKind(sql);
    const params = values.map(param);
    return {
      bind: (...next) => statement(sql, next),
      all: () => query(sql, params),
      run: () => query(sql, params),
      first: async column => {
        const row = (await query(sql, params)).results[0] ?? null;
        return column === undefined || row === null ? row : row[column] ?? null;
      },
      raw: async () => (await query(sql, params)).results.map(row => Object.values(row)),
      execute: () => query(sql, params),
    };
  }
  return {
    prepare: sql => statement(sql, []),
    // Not atomic: only cache writes reach here, and each row stands alone.
    async batch(statements) {
      const out = [];
      for (const item of statements) out.push(await item.execute());
      return out;
    },
    get queryCount() { return queries; },
  };
}
