import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readdirSync, readFileSync } from 'node:fs';
import { collate } from '../deploy/cloudflare-js/collate.mjs';

let seed = 11;
const rnd = k => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed % k; };
const ascii = Array.from({ length: 95 }, (_, i) => String.fromCharCode(32 + i));
const pools = [ascii, [...'ABCDEFabcdef0123456789.-:_ '], [...'AaBb가각나다힣 :-()'], [...'XYZxyz09.']];
const hangul = () => String.fromCharCode(0xac00 + rnd(11172));

test('collate orders exactly like localeCompare on the characters the bot uses', () => {
  for (let i = 0; i < 100000; i += 1) {
    const pool = pools[i % 4];
    const gen = () => { let s = ''; const len = rnd(8);
      for (let j = 0; j < len; j += 1) s += i % 5 === 0 && rnd(3) === 0 ? hangul() : pool[rnd(pool.length)];
      return s; };
    const a = gen();
    const b = i % 3 === 0 ? a.slice(0, rnd(a.length + 1)) + gen() : gen();
    assert.equal(Math.sign(collate(a, b)), Math.sign(a.localeCompare(b)), JSON.stringify([a, b]));
  }
});

test('collate keeps localeCompare order for realistic keys and falls back outside its model', () => {
  const keys = ['US:QQQ:ETF:USD', 'KOSPI:005930:EQUITY:KRW', 'US:BRK.B:EQUITY:USD', 'US:brk-b:EQUITY:USD',
    'candidate', 'kiwoom_accum', 'meritz', 'ROK', 'rok', 'ETF 국내', '개별주', 'Ä', 'é', '😀', '\t', 'ㄱ'];
  const expected = [...keys].sort((a, b) => a.localeCompare(b));
  assert.deepEqual([...keys].sort(collate), expected);
  assert.equal(collate('a', 'a'), 0);
});

test('no Worker module calls localeCompare directly', () => {
  const dir = new URL('../deploy/cloudflare-js/', import.meta.url);
  for (const name of readdirSync(dir).filter(file => file.endsWith('.mjs') && file !== 'collate.mjs')) {
    assert.doesNotMatch(readFileSync(new URL(name, dir), 'utf8'), /localeCompare\(/, name);
  }
});
