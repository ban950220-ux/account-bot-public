// One-attempt Telegram transport. A timed out or ambiguous send is UNKNOWN.
export function splitTelegramMessage(text, limit = 3900) {
  if (typeof text !== 'string' || !text || !Number.isSafeInteger(limit) ||
      limit < 256 || limit > 4096) throw new TypeError('invalid Telegram message');
  if (text.length <= limit) return [text];
  const chunks = [];
  let current = '';
  const safePrefix = line => {
    let length = 0;
    for (const character of line) {
      if (length + character.length > limit) break;
      length += character.length;
    }
    return length;
  };
  const lines = text.match(/[^\n]*\n|[^\n]+$/g) || [];
  for (let line of lines) {
    while (line.length > limit) {
      if (current) { chunks.push(current.replace(/\n+$/, '')); current = ''; }
      const cut = safePrefix(line);
      chunks.push(line.slice(0, cut));
      line = line.slice(cut);
    }
    if (current.length + line.length > limit) {
      chunks.push(current.replace(/\n+$/, ''));
      current = line;
    } else current += line;
  }
  if (current) chunks.push(current.replace(/\n+$/, ''));
  if (!chunks.length || chunks.some(chunk => !chunk || chunk.length > limit)) {
    throw new TypeError('invalid Telegram chunks');
  }
  return chunks;
}

export async function sendTelegramOnce(fetcher, token, chatId, text) {
  if (typeof fetcher !== 'function' || !/^\d+:[A-Za-z0-9_-]{20,}$/.test(token) ||
      !/^[1-9]\d*$/.test(chatId) || typeof text !== 'string' ||
      !text || text.length > 3900) throw new TypeError('invalid Telegram send configuration');
  try {
    const response = await fetcher(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: chatId, text, parse_mode: 'HTML',
        disable_web_page_preview: true }),
      signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) return false;
    const result = await response.json();
    return result?.ok === true && Number.isSafeInteger(result.result?.message_id) &&
      result.result.message_id > 0;
  } catch { return false; }
}
