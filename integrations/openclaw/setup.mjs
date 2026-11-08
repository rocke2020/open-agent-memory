#!/usr/bin/env node
import { spawnSync } from 'node:child_process';
import { createInterface } from 'node:readline/promises';

const entry = 'plugins.entries.oam-openclaw';
const usage = 'Usage: node setup.mjs [--url https://host-or-loopback] [--user-id ID] [--key-env ENV_VAR] [--agent-id ID]\nConfigures the installed plugin for an existing OAM API and the selected agent (default: main).';

function openclaw(args, allowMissing = false) {
  const result = spawnSync('openclaw', args, { encoding: 'utf8' });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    let error;
    try {
      const response = JSON.parse(result.stdout);
      error = response.error?.message || response.errors?.[0]?.message;
    } catch { /* non-JSON CLI failure */ }
    if (allowMissing && result.status === 1 && error?.startsWith('Config path is valid but unset:')) return '{}';
    throw new Error(error || result.stderr.trim() || `openclaw ${args.slice(0, 2).join(' ')} failed`);
  }
  return result.stdout;
}

function parseArgs(argv) {
  const options = {};
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    if (flag === '--help' || flag === '-h') return { help: true };
    if (!['--url', '--user-id', '--key-env', '--agent-id'].includes(flag) || !argv[i + 1] || argv[i + 1].startsWith('--')) {
      throw new Error(`Unknown or incomplete option: ${flag}`);
    }
    options[flag] = argv[++i];
  }
  return options;
}

async function values(options) {
  let url = options['--url'];
  let userId = options['--user-id'];
  let keyEnv = options['--key-env'];
  let agentId = options['--agent-id'];
  if (process.stdin.isTTY && (!url || !userId || !keyEnv)) {
    const prompt = createInterface({ input: process.stdin, output: process.stdout });
    try {
      url ??= (await prompt.question('OAM URL [http://127.0.0.1:17333]: ')).trim() || 'http://127.0.0.1:17333';
      userId ??= (await prompt.question('OAM user ID: ')).trim();
      keyEnv ??= (await prompt.question('Gateway key environment variable [OAM_USER_API_KEY]: ')).trim() || 'OAM_USER_API_KEY';
    } finally {
      prompt.close();
    }
  }
  url ??= 'http://127.0.0.1:17333';
  keyEnv ??= 'OAM_USER_API_KEY';
  userId = userId?.trim();
  keyEnv = keyEnv.trim();
  const origin = new URL(url);
  if ((origin.protocol !== 'https:' && !(origin.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(origin.hostname))) ||
      origin.username || origin.password || origin.pathname !== '/' || origin.search || origin.hash) {
    throw new Error('OAM URL must be an HTTPS origin or loopback HTTP origin');
  }
  if (!userId) throw new Error('OAM user ID is required (--user-id)');
  if (!/^[A-Z][A-Z0-9_]*$/.test(keyEnv)) throw new Error('Key environment variable must be UPPER_SNAKE_CASE');
  if (agentId !== undefined) {
    agentId = agentId.trim();
    if (!/^[a-z0-9][a-z0-9_-]{0,63}$/i.test(agentId)) throw new Error('Invalid OpenClaw agent ID');
    agentId = agentId.toLowerCase();
  }
  return { url: origin.origin, userId, keyEnv, agentId };
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.help) {
    console.log(usage);
    return;
  }
  const { url, userId, keyEnv, agentId } = await values(options);
  const existing = JSON.parse(openclaw(['config', 'get', entry, '--json'], true));
  if (existing.config?.allowedDirectConversations?.length > 0) {
    throw new Error('Existing direct conversation routes require manual review before changing OAM identity');
  }
  if (existing.hooks?.allowConversationAccess === false) {
    throw new Error('Conversation access is explicitly disabled; enable it before setup');
  }
  const batch = [
    { path: `${entry}.config.baseUrl`, value: url },
    { path: `${entry}.config.userId`, value: userId },
    { path: `${entry}.config.apiKey`, ref: { source: 'env', provider: 'default', id: keyEnv } },
    { path: `${entry}.config.allowLocalMainAgent`, value: true },
  ];
  if (agentId !== undefined) batch.push({ path: `${entry}.config.targetAgentId`, value: agentId });
  if (existing.config?.allowedDirectConversations === undefined) {
    batch.push({ path: `${entry}.config.allowedDirectConversations`, value: [] });
  }
  if (existing.hooks?.allowConversationAccess === undefined) {
    batch.push({ path: `${entry}.hooks.allowConversationAccess`, value: true });
  }
  batch.push(
    { path: `${entry}.enabled`, value: true },
    { path: 'plugins.slots.memory', value: 'oam-openclaw' },
    { path: 'plugins.entries.memory-core.enabled', value: false },
  );
  const update = ['config', 'set', '--batch-json', JSON.stringify(batch)];
  openclaw([...update, '--dry-run']);
  openclaw(update);
  console.log(`Configured OAM user ${userId} at ${url}. No credential value was stored.`);
  console.log(`Ensure the Gateway has ${keyEnv}, then run: openclaw gateway restart --safe`);
}

main().catch((error) => {
  console.error(`oam-openclaw-setup: ${error.message}`);
  process.exitCode = 1;
});
