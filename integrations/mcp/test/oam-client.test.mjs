import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { after, before, beforeEach, test } from 'node:test';

const { createOamClient, OamApiError } = await import('../src/oam-client.ts');

let server;
let baseUrl;
let requests;
let responses;

before(async () => {
  server = createServer(async (request, response) => {
    let body = '';
    for await (const part of request) body += part;
    requests.push({
      method: request.method,
      path: request.url,
      headers: request.headers,
      body: body ? JSON.parse(body) : null,
    });
    const next = responses.shift() ?? { status: 200, body: {} };
    response.statusCode = next.status;
    response.setHeader('content-type', 'application/json');
    for (const [name, value] of Object.entries(next.headers ?? {})) response.setHeader(name, value);
    response.end(JSON.stringify(next.body));
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  baseUrl = `http://127.0.0.1:${server.address().port}`;
});

beforeEach(() => {
  requests = [];
  responses = [];
});

after(() => server?.close());

function queue(status, body, headers = {}) {
  responses.push({ status, body, headers });
}

function client(options = {}) {
  return createOamClient({ baseUrl, userId: 'u1', apiKey: 'secret-key', ...options });
}

test('recall sends the fast cited-context contract by default', async () => {
  queue(200, {
    context: '[1] Lin prefers concise answers.',
    context_tokens: 7,
    tokenizer: 'o200k_base',
    trace_id: 'trace_1',
    items: [],
  });

  const result = await client().recall({ query: 'How should I answer Lin?' });

  assert.equal(result.context, '[1] Lin prefers concise answers.');
  assert.equal(result.trace_id, 'trace_1');
  assert.equal(requests.length, 1);
  assert.equal(requests[0].method, 'POST');
  assert.equal(requests[0].path, '/v1/recall');
  assert.equal(requests[0].headers.authorization, 'Bearer secret-key');
  assert.deepEqual(requests[0].body, {
    user_id: 'u1',
    query: 'How should I answer Lin?',
    recall_level: 'fast',
    include: { chunks: true },
  });
});

test('recall passes normal mode and chunk omission', async () => {
  queue(200, { context: 'evidence', context_tokens: 1, items: [] });

  await client().recall({ query: 'status', recallLevel: 'normal', includeChunks: false });

  assert.deepEqual(requests[0].body, {
    user_id: 'u1',
    query: 'status',
    recall_level: 'normal',
    include: { chunks: false },
  });
});

test('add sends one eligible user message with an idempotency key', async () => {
  queue(202, { operation_id: 'op_1', source_id: 'src_1', readiness: { accepted: true } });

  const result = await client().add({ content: 'I prefer aisle seats.' });

  assert.equal(result.operation_id, 'op_1');
  assert.equal(requests[0].method, 'POST');
  assert.equal(requests[0].path, '/v1/retain');
  assert.equal(requests[0].headers.authorization, 'Bearer secret-key');
  assert.equal(typeof requests[0].headers['idempotency-key'], 'string');
  assert.ok(requests[0].headers['idempotency-key'].length >= 16);
  assert.equal(requests[0].body.user_id, 'u1');
  assert.equal(requests[0].body.input_type, 'interaction');
  assert.equal(requests[0].body.messages.length, 1);
  assert.equal(requests[0].body.messages[0].role, 'user');
  assert.equal(requests[0].body.messages[0].content, 'I prefer aisle seats.');
  assert.equal(requests[0].body.messages[0].learning_eligible, true);
  assert.ok(Number.isFinite(Date.parse(requests[0].body.messages[0].event_time)));
});

test('write methods reuse a caller-provided idempotency key', async () => {
  queue(202, { operation_id: 'op_retry' });
  queue(202, { operation_id: 'op_doc_retry' });

  await client().add({ content: 'retry', idempotencyKey: 'retry-key-1' });
  await client().addDocument({
    documentId: 'notes',
    updateMode: 'replace',
    text: 'retry',
    idempotencyKey: 'retry-key-2',
  });

  assert.equal(requests[0].headers['idempotency-key'], 'retry-key-1');
  assert.equal(requests[1].headers['idempotency-key'], 'retry-key-2');
});

test('write methods reject header-unsafe idempotency keys before dispatch', async () => {
  for (const idempotencyKey of ['retry\nkey', 'a'.repeat(201)]) {
    await assert.rejects(
      () => client().add({ content: 'unsafe', idempotencyKey }),
      (error) => {
        assert.ok(error instanceof OamApiError);
        assert.equal(error.code, 'InvalidIdempotencyKey');
        assert.equal(error.retryable, false);
        return true;
      },
    );
  }
  assert.equal(requests.length, 0);
});

test('a stalled response body maps to RequestTimeout', async () => {
  const stalled = createServer((request, response) => {
    response.writeHead(200, { 'content-type': 'application/json' });
    response.write('{"context":');
  });
  await new Promise((resolve) => stalled.listen(0, '127.0.0.1', resolve));
  const stalledBase = `http://127.0.0.1:${stalled.address().port}`;

  try {
    await assert.rejects(
      () =>
        createOamClient({
          baseUrl: stalledBase,
          userId: 'u1',
          apiKey: 'probe-key',
          timeoutMs: 50,
        }).recall({ query: 'stall' }),
      (error) => {
        assert.ok(error instanceof OamApiError);
        assert.equal(error.code, 'RequestTimeout');
        assert.equal(error.retryable, true);
        return true;
      },
    );
  } finally {
    stalled.closeAllConnections();
    await new Promise((resolve) => stalled.close(resolve));
  }
});

test('addDocument sends text and HTTPS URL documents without multipart', async () => {
  queue(202, { operation_id: 'op_text' });
  queue(202, { operation_id: 'op_url' });

  await client().addDocument({
    documentId: 'notes',
    updateMode: 'replace',
    text: 'A checkup happened on 2026-09-20.',
  });
  await client().addDocument({
    documentId: 'scan',
    updateMode: 'replace',
    url: 'https://example.org/scan.pdf',
    mediaType: 'application/pdf',
  });

  assert.equal(requests[0].path, '/v1/users/u1/resources');
  assert.equal(requests[0].body.input_type, 'document');
  assert.equal(requests[0].body.text, 'A checkup happened on 2026-09-20.');
  assert.equal(typeof requests[0].headers['idempotency-key'], 'string');
  assert.equal(requests[1].body.input_type, 'document_url');
  assert.equal(requests[1].body.url, 'https://example.org/scan.pdf');
  assert.equal(requests[1].body.media_type, 'application/pdf');
  assert.equal(typeof requests[1].headers['idempotency-key'], 'string');
});

test('operation and profile reads use the user-scoped paths', async () => {
  queue(200, { operation_id: 'op_1', status: 'succeeded' });
  queue(200, { content: 'A stored profile.', freshness: { current: true } });

  const operation = await client().getOperation('op_1');
  const profile = await client().getProfile();

  assert.equal(operation.status, 'succeeded');
  assert.equal(profile.content, 'A stored profile.');
  assert.equal(requests[0].method, 'GET');
  assert.equal(requests[0].path, '/v1/users/u1/operations/op_1');
  assert.equal(requests[1].method, 'GET');
  assert.equal(requests[1].path, '/v1/users/u1/profile');
});

test('remove selects the typed memory or insight route', async () => {
  queue(202, { operation_id: 'op_remove_memory' });
  queue(202, { operation_id: 'op_remove_insight' });

  await client().remove({ memoryId: 'mem_1', memoryType: 'basic' });
  await client().remove({ memoryId: 'ins_1', memoryType: 'insight' });

  assert.equal(requests[0].method, 'DELETE');
  assert.equal(requests[0].path, '/v1/users/u1/memories/mem_1');
  assert.equal(requests[1].method, 'DELETE');
  assert.equal(requests[1].path, '/v1/users/u1/insights/ins_1');
});

test('OAM errors expose a safe code, status, and request ID', async () => {
  queue(
    404,
    { detail: { code: 'MemoryUnavailable', retryable: false } },
    { 'x-request-id': 'req_404' },
  );

  await assert.rejects(
    () => client().recall({ query: 'missing' }),
    (error) => {
      assert.ok(error instanceof OamApiError);
      assert.equal(error.status, 404);
      assert.equal(error.code, 'MemoryUnavailable');
      assert.equal(error.requestId, 'req_404');
      assert.equal(error.retryable, false);
      assert.ok(!error.message.includes('secret-key'));
      return true;
    },
  );
});

test('base URL trailing slashes do not create double-slash paths', async () => {
  queue(200, { context: 'ok', items: [] });

  await client({ baseUrl: `${baseUrl}/` }).recall({ query: 'hello' });

  assert.equal(requests[0].path, '/v1/recall');
});

test('client rejects unsafe endpoints and empty credentials before any request', () => {
  assert.throws(
    () => client({ baseUrl: 'http://user:pass@example.org' }),
    /must not contain credentials/,
  );
  assert.throws(() => client({ baseUrl: 'ftp://example.org' }), /must use http or https/);
  assert.throws(() => client({ baseUrl: `${baseUrl}/?x=1` }), /query or fragment/);
  assert.throws(() => client({ baseUrl: `${baseUrl}/#x` }), /query or fragment/);
  assert.throws(() => client({ apiKey: '   ' }), /OAM_USER_API_KEY is required/);
  assert.equal(requests.length, 0);
});

test('client refuses redirects instead of forwarding the bearer key', async () => {
  queue(302, {}, { location: '/v1/other' });

  await assert.rejects(
    () => client().recall({ query: 'redirect' }),
    (error) => {
      assert.ok(error instanceof OamApiError);
      assert.equal(error.code, 'NetworkError');
      return true;
    },
  );
  assert.equal(requests.length, 1);
});
