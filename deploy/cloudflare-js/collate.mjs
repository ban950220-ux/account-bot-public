// Drop-in for String.prototype.localeCompare (root collation) without the ICU
// collator, whose first use costs several ms of CPU in a fresh Worker isolate.
// Exact for printable ASCII and precomposed Hangul syllables; any other
// character falls back to localeCompare, so the order never changes.
//
// Root collation, as observed: punctuation and symbols are not ignorable and sort
// before digits, digits before letters, letters before Hangul. Case is compared
// only when the case-folded strings are equal, lowercase first.
const ORDER = " _-,;:!?.'\"()[]{}@*/\\&#%`^+<=>|~$0123456789" +
  'aAbBcCdDeEfFgGhHiIjJkKlLmMnNoOpPqQrRsStTuUvVwWxXyYzZ';
const PRIMARY = new Int32Array(128).fill(-1);
const UPPER = new Uint8Array(128);
let weight = 0;
for (let index = 0; index < ORDER.length; index += 1) {
  const code = ORDER.charCodeAt(index);
  const upper = code >= 65 && code <= 90;
  if (upper) { PRIMARY[code] = PRIMARY[code + 32]; UPPER[code] = 1; } else PRIMARY[code] = weight++;
}
const HANGUL_FIRST = 0xac00;
const HANGUL_LAST = 0xd7a3;

function primary(code) {
  if (code < 128) return PRIMARY[code];
  if (code >= HANGUL_FIRST && code <= HANGUL_LAST) return 1000 + code - HANGUL_FIRST;
  return -1;
}

function modelled(text) {
  for (let index = 0; index < text.length; index += 1) if (primary(text.charCodeAt(index)) < 0) return false;
  return true;
}

export function collate(left, right) {
  const a = String(left);
  const b = String(right);
  if (!modelled(a) || !modelled(b)) return a.localeCompare(b);
  const length = Math.min(a.length, b.length);
  let tertiary = 0;
  for (let index = 0; index < length; index += 1) {
    const x = a.charCodeAt(index);
    const y = b.charCodeAt(index);
    const px = primary(x);
    const py = primary(y);
    if (px !== py) return px < py ? -1 : 1;
    if (tertiary === 0 && x !== y) tertiary = UPPER[x] ? 1 : -1;
  }
  if (a.length !== b.length) return a.length < b.length ? -1 : 1;
  return tertiary;
}
