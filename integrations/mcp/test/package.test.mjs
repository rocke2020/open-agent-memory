import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { delimiter, dirname, join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

const packageDirectory = fileURLToPath(new URL('..', import.meta.url));
const requestOptions = { timeout: 5_000 };

async function listInstalledTools(command, cwd, env) {
  const transport = new StdioClientTransport({ command, cwd, env, stderr: 'pipe' });
  const client = new Client({ name: 'oam-package-test', version: '0.0.0' });
  let stderr = '';
  transport.stderr.on('data', (chunk) => { stderr += chunk; });
  let pid;
  try {
    await client.connect(transport, requestOptions);
    pid = transport.pid;
    const { tools } = await client.listTools(undefined, requestOptions);
    return tools.map((tool) => tool.name).sort();
  } catch (error) {
    throw new Error(`Installed oam-mcp failed: ${error.message}\n${stderr}`, { cause: error });
  } finally {
    await client.close();
    if (pid) {
      assert.throws(() => process.kill(pid, 0), { code: 'ESRCH' }, 'MCP child must exit');
    }
  }
}

test('the packed npm package exposes its tools through the installed bin', async (t) => {
  const temporaryDirectory = await mkdtemp(join(tmpdir(), 'oam-mcp-package-'));
  const prefix = join(temporaryDirectory, 'install');
  const env = {
    PATH: `${dirname(process.execPath)}${delimiter}${process.env.PATH ?? ''}`,
    HOME: temporaryDirectory,
    USERPROFILE: temporaryDirectory,
    npm_config_userconfig: join(temporaryDirectory, 'npmrc'),
    npm_config_globalconfig: join(temporaryDirectory, 'global-npmrc'),
    npm_config_cache: join(temporaryDirectory, 'cache'),
    npm_config_offline: 'true',
    npm_config_update_notifier: 'false',
  };
  let apiRequests = 0;
  const api = createServer((_request, response) => {
    apiRequests += 1;
    response.writeHead(503).end();
  });
  try {
    await mkdir(prefix);
    await writeFile(env.npm_config_userconfig, '');
    await writeFile(env.npm_config_globalconfig, '');
    function npm(args, cwd) {
      const result = spawnSync('npm', args, {
        cwd, env, encoding: 'utf8', timeout: 60_000,
      });
      assert.ifError(result.error);
      assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
      return result.stdout;
    }
    const [packed] = JSON.parse(npm(
      ['pack', '--json', '--pack-destination', temporaryDirectory], packageDirectory,
    ));
    npm([
      'install', '--prefix', prefix, '--offline', '--ignore-scripts',
      '--no-audit', '--no-fund', '--no-save', '--no-package-lock',
      join(temporaryDirectory, packed.filename),
    ], temporaryDirectory);

    await new Promise((resolve) => api.listen(0, '127.0.0.1', resolve));
    const serverEnv = {
      ...env,
      OAM_BASE_URL: `http://127.0.0.1:${api.address().port}`,
      OAM_USER_ID: 'package-test-user',
      OAM_USER_API_KEY: 'synthetic-package-test-key',
    };
    const command = join(prefix, 'node_modules', '.bin', 'oam-mcp');
    const entrypoint = join(prefix, 'node_modules', packed.name, 'dist', 'index.js');
    const originalEntrypoint = await readFile(entrypoint);
    const probe = () => listInstalledTools(command, prefix, serverEnv);

    await t.test('initializes and lists exactly the six documented tools', async () => {
      assert.deepEqual(await probe(), [
        'memory_add',
        'memory_add_document',
        'memory_operation',
        'memory_profile',
        'memory_recall',
        'memory_remove',
      ]);
      assert.equal(apiRequests, 0, 'initialization and tool listing must not call OAM');
    });
    await t.test('rejects a missing installed entrypoint', async () => {
      await rm(entrypoint);
      try {
        await assert.rejects(probe, /ENOENT/);
      } finally {
        await writeFile(entrypoint, originalEntrypoint, { mode: 0o755 });
      }
    });
    await t.test('rejects a broken installed entrypoint', async () => {
      await writeFile(entrypoint, '#!/usr/bin/env node\nthrow new Error("broken-package-fixture");\n');
      try {
        await assert.rejects(probe, /broken-package-fixture/);
      } finally {
        await writeFile(entrypoint, originalEntrypoint);
      }
    });
    assert.equal(apiRequests, 0);
  } finally {
    api.closeAllConnections();
    await new Promise((resolve) => api.close(resolve));
    await rm(temporaryDirectory, { recursive: true, force: true });
  }
});
