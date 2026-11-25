import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import { HELP, START } from '../deploy/cloudflare-js/static-commands.mjs';

// Every /command named in /start and /help must be one the router recognizes.
test('help and start only name commands the router accepts', () => {
  const router = readFileSync(new URL('../deploy/cloudflare-js/command-router.mjs', import.meta.url),
    'utf8');
  const aliases = new Set([...router.matchAll(/\['([^']+)', '[a-z_]+'\]/g)].map(match => match[1]));
  const named = new Set([...`${START}\n${HELP}`.replace(/<[^>]+>/g, " ").matchAll(/\/([A-Za-z_가-힣]+)/g)]
    .map(match => match[1]));
  assert.ok(named.size >= 15, `only ${named.size} commands named`);
  for (const name of named) assert.ok(aliases.has(name), `/${name} is not a router command`);
  for (const feature of ['모으기설정', '모으기삭제', '모으기복원', '모으기이력', '모으기시점',
    '추천적용', '분석갱신']) assert.ok(named.has(feature), feature);
  assert.ok(HELP.length < 3900, 'fits one Telegram message');
});
