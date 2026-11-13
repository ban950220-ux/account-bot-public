// One budget for read-only provider HTTP and Telegram within a scheduled event.
export function createExternalRequestBudget(limit = 50) {
  if (!Number.isSafeInteger(limit) || limit <= 0) throw new TypeError('external request budget required');
  let used = 0;
  let exceeded = false;
  function assertAvailable(count) {
    if (!Number.isSafeInteger(count) || count < 0) throw new TypeError('invalid external request reservation');
    if (exceeded || used + count > limit) {
      throw new Error('external request budget unavailable before report delivery');
    }
  }
  return { assertAvailable, get used() { return used; }, wrap(fetcher) {
    if (typeof fetcher !== 'function') throw new TypeError('external transport required');
    return async (input, options = {}) => {
      try { assertAvailable(1); }
      catch (error) { exceeded = true; throw error; }
      used++;
      // Every counted call can issue at most one HTTP request. Providers already
      // use manual redirects; Telegram redirects also fail closed.
      return fetcher(input, { ...options, redirect: 'manual' });
    };
  } };
}
