// D1's Free limit counts binding calls; a batch counts once, including failures.
export function createD1QueryBudget(db, limit = 50) {
  if (!db || !Number.isSafeInteger(limit) || limit <= 0) throw new TypeError('D1 budget required');
  let used = 0;
  function assertAvailable(count) {
    if (!Number.isSafeInteger(count) || count < 0) throw new TypeError('invalid D1 reservation');
    if (used + count > limit) throw new Error('D1 query budget unavailable before report delivery');
  }
  function charge() { assertAvailable(1); used++; }
  const originals = new WeakMap();
  function wrap(statement) {
    const wrapped = new Proxy(statement, { get(target, key) {
      if (key === 'bind') return (...values) => wrap(target.bind(...values));
      if (['first', 'all', 'run', 'raw'].includes(key)) return async (...args) => {
        charge(); return target[key](...args);
      };
      const value = target[key];
      return typeof value === 'function' ? value.bind(target) : value;
    } });
    originals.set(wrapped, statement);
    return wrapped;
  }
  return { assertAvailable, get used() { return used; }, db: {
    prepare: sql => wrap(db.prepare(sql)),
    batch: async statements => {
      charge(); return db.batch(statements.map(statement => originals.get(statement) || statement));
    },
  } };
}
