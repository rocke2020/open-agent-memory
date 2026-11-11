import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const packageDirectory = fileURLToPath(new URL('..', import.meta.url));

function run(overrides) {
  return spawnSync(
    process.execPath,
    ['--experimental-strip-types', 'src/index.ts'],
    {
      cwd: packageDirectory,
      encoding: 'utf8',
      timeout: 15_000,
      env: {
        ...process.env,
        OAM_BASE_URL: '',
        OAM_USER_ID: '',
        OAM_USER_API_KEY: '',
        ...overrides,
      },
    },
  );
}

test('the CLI fails before connecting when required configuration is missing', () => {
  const result = run({});

  assert.equal(result.status, 1);
  assert.match(result.stderr, /OAM_BASE_URL is required/);
});

test('the CLI rejects non-HTTP endpoints without printing the API key', () => {
  const result = run({
    OAM_BASE_URL: 'ftp://example.org',
    OAM_USER_ID: 'u1',
    OAM_USER_API_KEY: 'super-secret-key',
  });

  assert.equal(result.status, 1);
  assert.match(result.stderr, /must use http or https/);
  assert.ok(!result.stderr.includes('super-secret-key'));
});
