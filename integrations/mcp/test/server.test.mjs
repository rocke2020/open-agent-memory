import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';

const { createOamMcpServer } = await import('../src/server.ts');
const { OamApiError } = await import('../src/oam-client.ts');
const packageJson = JSON.parse(
  await readFile(new URL('../package.json', import.meta.url), 'utf8'),
);

const recallResult = {
  context: '[1] Lin prefers concise answers.',
  context_tokens: 9,
  tokenizer: 'o200k_base',
  trace_id: 'trace_1',
  items: [{ source_id: 'src_1' }],
};

function fakeClient(overrides = {}) {
  const calls = [];
  return {
    calls,
    async recall(input) {
      calls.push({ tool: 'recall', input });
      if (overrides.recall) return overrides.recall(input);
      return recallResult;
    },
    async add(input) {
      calls.push({ tool: 'add', input });
      if (overrides.add) return overrides.add(input);
      return { operation_id: 'op_add', source_id: 'src_add' };
    },
    async addDocument(input) {
      calls.push({ tool: 'addDocument', input });
      if (overrides.addDocument) return overrides.addDocument(input);
      return { operation_id: 'op_doc' };
    },
    async getOperation(operationId) {
      calls.push({ tool: 'getOperation', input: operationId });
      if (overrides.getOperation) return overrides.getOperation(operationId);
      return { operation_id: operationId, status: 'succeeded' };
    },
    async getProfile() {
      calls.push({ tool: 'getProfile', input: null });
      if (overrides.getProfile) return overrides.getProfile();
      return { content: 'A stored profile.', references: [] };
    },
    async remove(input) {
      calls.push({ tool: 'remove', input });
      if (overrides.remove) return overrides.remove(input);
      return { operation_id: 'op_remove' };
    },
  };
}

async function withServer(fake, run) {
  const server = createOamMcpServer(fake);
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  const client = new Client({ name: 'oam-mcp-test', version: '0.0.0' });
  await Promise.all([client.connect(clientTransport), server.connect(serverTransport)]);
  try {
    await run(client);
  } finally {
    await client.close();
    await server.close();
  }
}

function payload(result) {
  return JSON.parse(result.content[0].text);
}

test('lists the six OAM memory tools and marks read-only tools', async () => {
  await withServer(fakeClient(), async (client) => {
    assert.equal(client.getServerVersion().name, 'oam-mcp');
    assert.equal(client.getServerVersion().version, packageJson.version);
    const { tools } = await client.listTools();
    assert.deepEqual(
      tools.map((tool) => tool.name).sort(),
      [
        'memory_add',
        'memory_add_document',
        'memory_operation',
        'memory_profile',
        'memory_recall',
        'memory_remove',
      ],
    );
    const recall = tools.find((tool) => tool.name === 'memory_recall');
    assert.match(recall.description, /untrusted evidence/i);
    assert.equal(recall.annotations.readOnlyHint, true);
    const remove = tools.find((tool) => tool.name === 'memory_remove');
    assert.equal(remove.annotations.destructiveHint, true);
  });
});

test('memory_recall returns compact cited context by default and full evidence on request', async () => {
  const fake = fakeClient();
  await withServer(fake, async (client) => {
    const compact = await client.callTool({
      name: 'memory_recall',
      arguments: { query: 'How should I answer Lin?' },
    });
    assert.equal(compact.isError ?? false, false);
    assert.deepEqual(payload(compact), {
      context: recallResult.context,
      context_tokens: 9,
      tokenizer: 'o200k_base',
      trace_id: 'trace_1',
      item_count: 1,
    });
    assert.deepEqual(fake.calls[0].input, {
      query: 'How should I answer Lin?',
      recallLevel: 'fast',
      includeChunks: true,
    });

    const full = await client.callTool({
      name: 'memory_recall',
      arguments: { query: 'How should I answer Lin?', recall_level: 'normal', detail: 'full' },
    });
    assert.deepEqual(payload(full), recallResult);
    assert.deepEqual(fake.calls[1].input, {
      query: 'How should I answer Lin?',
      recallLevel: 'normal',
      includeChunks: true,
    });
  });
});

test('memory_add retains one explicit message with mapped time and eligibility', async () => {
  const fake = fakeClient();
  await withServer(fake, async (client) => {
    const result = await client.callTool({
      name: 'memory_add',
      arguments: {
        content: 'I prefer aisle seats.',
        event_time: '2026-09-23T10:00:00+08:00',
        learning_eligible: false,
      },
    });
    assert.equal(payload(result).operation_id, 'op_add');
    assert.deepEqual(fake.calls[0].input, {
      content: 'I prefer aisle seats.',
      eventTime: '2026-09-23T10:00:00+08:00',
      learningEligible: false,
    });
  });
});

test('write tools pass a caller-provided idempotency key', async () => {
  const fake = fakeClient();
  await withServer(fake, async (client) => {
    await client.callTool({
      name: 'memory_add',
      arguments: { content: 'retry', idempotency_key: 'retry-1' },
    });
    await client.callTool({
      name: 'memory_add_document',
      arguments: {
        document_id: 'notes',
        update_mode: 'replace',
        text: 'retry',
        idempotency_key: 'retry-2',
      },
    });

    assert.equal(fake.calls[0].input.idempotencyKey, 'retry-1');
    assert.equal(fake.calls[1].input.idempotencyKey, 'retry-2');
  });
});

test('memory_add_document accepts text or HTTPS URL and rejects ambiguous input', async () => {
  const fake = fakeClient();
  await withServer(fake, async (client) => {
    await client.callTool({
      name: 'memory_add_document',
      arguments: { document_id: 'notes', update_mode: 'replace', text: 'Checkup on 2026-09-20.' },
    });
    assert.deepEqual(fake.calls[0].input, {
      documentId: 'notes',
      updateMode: 'replace',
      text: 'Checkup on 2026-09-20.',
    });

    await client.callTool({
      name: 'memory_add_document',
      arguments: {
        document_id: 'scan',
        update_mode: 'replace',
        url: 'https://example.org/scan.pdf',
        media_type: 'application/pdf',
      },
    });
    assert.deepEqual(fake.calls[1].input, {
      documentId: 'scan',
      updateMode: 'replace',
      url: 'https://example.org/scan.pdf',
      mediaType: 'application/pdf',
    });

    const ambiguous = await client.callTool({
      name: 'memory_add_document',
      arguments: {
        document_id: 'both',
        update_mode: 'replace',
        text: 'text',
        url: 'https://example.org/scan.pdf',
      },
    });
    assert.equal(ambiguous.isError, true);
    assert.match(ambiguous.content[0].text, /exactly one/i);

    const insecure = await client.callTool({
      name: 'memory_add_document',
      arguments: {
        document_id: 'scan',
        update_mode: 'replace',
        url: 'http://example.org/scan.pdf',
        media_type: 'application/pdf',
      },
    });
    assert.equal(insecure.isError, true);
    assert.match(insecure.content[0].text, /https/i);

    const appendUrl = await client.callTool({
      name: 'memory_add_document',
      arguments: {
        document_id: 'scan',
        update_mode: 'append',
        url: 'https://example.org/scan.pdf',
        media_type: 'application/pdf',
      },
    });
    assert.equal(appendUrl.isError, true);
    assert.match(appendUrl.content[0].text, /append/i);
  });
});

test('operation, profile, and removal tools map to the OAM contract', async () => {
  const fake = fakeClient();
  await withServer(fake, async (client) => {
    const operation = await client.callTool({
      name: 'memory_operation',
      arguments: { operation_id: 'op_1' },
    });
    assert.equal(payload(operation).status, 'succeeded');

    const profile = await client.callTool({ name: 'memory_profile', arguments: {} });
    assert.equal(payload(profile).content, 'A stored profile.');

    await client.callTool({
      name: 'memory_remove',
      arguments: { memory_id: 'mem_1', memory_type: 'basic' },
    });
    await client.callTool({
      name: 'memory_remove',
      arguments: { memory_id: 'ins_1', memory_type: 'insight' },
    });

    assert.deepEqual(fake.calls[0], { tool: 'getOperation', input: 'op_1' });
    assert.deepEqual(fake.calls[1], { tool: 'getProfile', input: null });
    assert.deepEqual(fake.calls[2], {
      tool: 'remove',
      input: { memoryId: 'mem_1', memoryType: 'basic' },
    });
    assert.deepEqual(fake.calls[3], {
      tool: 'remove',
      input: { memoryId: 'ins_1', memoryType: 'insight' },
    });
  });
});

test('OAM errors are returned as safe tool errors', async () => {
  const fake = fakeClient({
    recall() {
      throw new OamApiError(404, 'MemoryUnavailable', 'req_404', false);
    },
  });
  await withServer(fake, async (client) => {
    const result = await client.callTool({
      name: 'memory_recall',
      arguments: { query: 'missing' },
    });
    assert.equal(result.isError, true);
    const body = payload(result);
    assert.deepEqual(body, {
      error: {
        code: 'MemoryUnavailable',
        status: 404,
        retryable: false,
        request_id: 'req_404',
        message: 'MemoryUnavailable (HTTP 404) request_id=req_404',
      },
    });
    assert.ok(!result.content[0].text.includes('secret-key'));
  });
});

test('unexpected failures do not expose stacks or memory content', async () => {
  const fake = fakeClient({
    recall() {
      throw new Error('secret internal detail');
    },
  });
  await withServer(fake, async (client) => {
    const result = await client.callTool({
      name: 'memory_recall',
      arguments: { query: 'missing' },
    });
    assert.equal(result.isError, true);
    assert.deepEqual(payload(result), {
      error: { code: 'ToolError', message: 'The OAM request failed.' },
    });
    assert.ok(!result.content[0].text.includes('secret internal detail'));
  });
});

test('invalid tool input is rejected before an OAM request', async () => {
  const fake = fakeClient();
  await withServer(fake, async (client) => {
    const emptyQuery = await client.callTool({
      name: 'memory_recall',
      arguments: { query: '' },
    });
    assert.equal(emptyQuery.isError, true);
    assert.match(emptyQuery.content[0].text, /input validation/i);

    const unsafeKey = await client.callTool({
      name: 'memory_add',
      arguments: { content: 'unsafe', idempotency_key: 'retry\nkey' },
    });
    assert.equal(unsafeKey.isError, true);
    assert.match(unsafeKey.content[0].text, /input validation/i);
    assert.equal(fake.calls.length, 0);
  });
});
