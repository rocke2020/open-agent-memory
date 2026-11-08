import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync, chmodSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { test } from 'node:test';

const setup = resolve('setup.mjs');

test('setup writes one validated batch with an env SecretRef and preserves an empty route', () => {
  const dir = mkdtempSync(join(tmpdir(), 'oam-setup-test-'));
  try {
    const cli = join(dir, 'openclaw');
    const calls = join(dir, 'calls.jsonl');
    writeFileSync(cli, `#!/usr/bin/env node
const { appendFileSync } = require('node:fs');
const args = process.argv.slice(2);
appendFileSync(process.env.SETUP_CALLS, JSON.stringify(args) + '\\n');
if (args[0] === 'config' && args[1] === 'get') console.log(JSON.stringify({config:{allowedDirectConversations:[]},hooks:{allowConversationAccess:true}}));
`);
    chmodSync(cli, 0o755);
    const result = spawnSync(process.execPath, [setup, '--url', 'http://127.0.0.1:17333', '--user-id', 'personal', '--key-env', 'OAM_USER_API_KEY', '--agent-id', 'research'], {
      encoding: 'utf8', env: { ...process.env, PATH: `${dir}:${process.env.PATH}`, SETUP_CALLS: calls },
    });
    assert.equal(result.status, 0, result.stderr);
    const invoked = readFileSync(calls, 'utf8').trim().split('\n').map(JSON.parse);
    assert.deepEqual(invoked.map((args) => args.slice(0, 2)), [['config', 'get'], ['config', 'set'], ['config', 'set']]);
    assert.ok(invoked[1].includes('--dry-run'));
    const batch = JSON.parse(invoked[2][3]);
    assert.equal(invoked[2][2], '--batch-json');
    assert.deepEqual(batch.find(({ path }) => path.endsWith('.apiKey'))?.ref, { source: 'env', provider: 'default', id: 'OAM_USER_API_KEY' });
    assert.equal(batch.find(({ path }) => path === 'plugins.slots.memory')?.value, 'oam-openclaw');
    assert.equal(batch.find(({ path }) => path === 'plugins.entries.memory-core.enabled')?.value, false);
    assert.equal(batch.find(({ path }) => path.endsWith('.allowLocalMainAgent'))?.value, true);
    assert.equal(batch.find(({ path }) => path.endsWith('.targetAgentId'))?.value, 'research');
    assert.equal(batch.find(({ path }) => path.endsWith('.allowedDirectConversations')), undefined);
    assert.ok(!JSON.stringify(batch).includes('Bearer '));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('fresh plugin config is treated as unset and sets the required local route', () => {
  const dir = mkdtempSync(join(tmpdir(), 'oam-setup-fresh-'));
  try {
    const cli = join(dir, 'openclaw');
    const calls = join(dir, 'calls.jsonl');
    writeFileSync(cli, `#!/usr/bin/env node
const { appendFileSync } = require('node:fs');
const args = process.argv.slice(2);
appendFileSync(process.env.SETUP_CALLS, JSON.stringify(args) + '\\n');
if (args[1] === 'get') {
  console.log(JSON.stringify({ok:false,error:{type:'cli_error',message:'Config path is valid but unset: plugins.entries.oam-openclaw.'}}));
  process.exit(1);
}
`);
    chmodSync(cli, 0o755);
    const result = spawnSync(process.execPath, [setup, '--user-id', 'new-user'], {
      encoding: 'utf8', env: { ...process.env, PATH: `${dir}:${process.env.PATH}`, SETUP_CALLS: calls },
    });
    assert.equal(result.status, 0, result.stderr);
    const invoked = readFileSync(calls, 'utf8').trim().split('\n').map(JSON.parse);
    const batch = JSON.parse(invoked[2][3]);
    assert.deepEqual(batch.find(({ path }) => path.endsWith('.allowedDirectConversations'))?.value, []);
    assert.equal(batch.find(({ path }) => path.endsWith('.hooks.allowConversationAccess'))?.value, true);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('existing direct chat routes refuse setup before changing memory identity', () => {
  const dir = mkdtempSync(join(tmpdir(), 'oam-setup-direct-'));
  try {
    const cli = join(dir, 'openclaw');
    const calls = join(dir, 'calls.jsonl');
    writeFileSync(cli, `#!/usr/bin/env node
const { appendFileSync } = require('node:fs');
const args = process.argv.slice(2);
appendFileSync(process.env.SETUP_CALLS, JSON.stringify(args) + '\\n');
if (args[1] === 'get') console.log(JSON.stringify({config:{allowedDirectConversations:[{channel:'telegram',chatId:'42',senderId:'7'}]}}));
`);
    chmodSync(cli, 0o755);
    const result = spawnSync(process.execPath, [setup, '--user-id', 'different-user'], {
      encoding: 'utf8', env: { ...process.env, PATH: `${dir}:${process.env.PATH}`, SETUP_CALLS: calls },
    });
    assert.equal(result.status, 1);
    assert.match(result.stderr, /direct conversation routes/);
    const invoked = readFileSync(calls, 'utf8').trim().split('\n').map(JSON.parse);
    assert.deepEqual(invoked.map((args) => args.slice(0, 2)), [['config', 'get']]);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('setup command is included in the installable plugin package', () => {
  const manifest = JSON.parse(readFileSync(resolve('package.json'), 'utf8'));
  assert.equal(manifest.bin['oam-openclaw-setup'], './setup.mjs');
  assert.ok(manifest.files.includes('setup.mjs'));
});

test('missing key fails preflight before any config write', () => {
  const dir = mkdtempSync(join(tmpdir(), 'oam-setup-missing-'));
  try {
    const cli = join(dir, 'openclaw');
    const calls = join(dir, 'calls.jsonl');
    writeFileSync(cli, `#!/usr/bin/env node
const { appendFileSync } = require('node:fs');
const args = process.argv.slice(2);
appendFileSync(process.env.SETUP_CALLS, JSON.stringify(args) + '\\n');
if (args[1] === 'get') console.log('{}');
if (args[1] === 'set' && args.includes('--dry-run')) {
  console.log(JSON.stringify({ok:false,errors:[{kind:'resolvability',message:'Environment variable is missing or empty.'}]}));
  process.exit(1);
}
`);
    chmodSync(cli, 0o755);
    const result = spawnSync(process.execPath, [setup, '--user-id', 'new-user', '--key-env', 'MISSING_KEY'], {
      encoding: 'utf8', env: { ...process.env, PATH: `${dir}:${process.env.PATH}`, SETUP_CALLS: calls },
    });
    assert.equal(result.status, 1);
    assert.match(result.stderr, /missing or empty/);
    const invoked = readFileSync(calls, 'utf8').trim().split('\n').map(JSON.parse);
    assert.equal(invoked.filter((args) => args[1] === 'set' && !args.includes('--dry-run')).length, 0);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
