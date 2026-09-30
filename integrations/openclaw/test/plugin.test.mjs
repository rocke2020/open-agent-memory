import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { after, before, test } from 'node:test';

let server;
let baseUrl;
const requests = [];
let stallRecall = false;

before(async () => {
  server = createServer(async (request, response) => {
    let body = '';
    for await (const part of request) body += part;
    requests.push({ path: request.url, headers: request.headers, body: JSON.parse(body) });
    response.setHeader('content-type', 'application/json');
    if (request.url === '/v1/recall') {
      if (stallRecall) {
        await new Promise((resolve) => {
          const timer = setTimeout(resolve, 10_000);
          response.once('close', () => { clearTimeout(timer); resolve(); });
        });
        if (response.destroyed) return;
      }
      response.end(JSON.stringify({ context: '[source: s1] Lin prefers concise answers.', context_tokens: 16, items: [{ source_id: 's1' }] }));
    } else if (request.url === '/v1/retain') {
      response.statusCode = 202;
      response.end(JSON.stringify({ operation_id: 'op1', source_id: 's1', revision: 1, readiness: { accepted: true } }));
    } else {
      response.statusCode = 404;
      response.end('{}');
    }
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  baseUrl = `http://127.0.0.1:${server.address().port}`;
});

after(() => server?.close());

async function installedPlugin(pluginConfig = {}, registrationMode = 'full') {
  const { default: plugin } = await import('../index.ts');
  const hooks = {};
  let tool;
  const api = {
    registrationMode,
    pluginConfig: {
      baseUrl,
      userId: 'u1',
      apiKey: 'private-key',
      allowedDirectConversations: [{ channel: 'telegram', chatId: '42', senderId: '7' }],
      ...pluginConfig,
    },
    logger: { info() {}, warn() {}, error() {} },
    registerMemoryCapability() {},
    registerTool(factory) { tool = factory; },
    on(name, callback) { hooks[name] = callback; },
  };
  plugin.register(api);
  return { hooks, tool };
}

test('offline inspection registers schemas with a SecretRef but full runtime rejects it', async () => {
  const apiKey = { source: 'env', provider: 'default', id: 'OAM_USER_API_KEY' };
  const { tool } = await installedPlugin({ apiKey }, 'tool-discovery');
  assert.equal(typeof tool.create, 'function');
  await assert.rejects(installedPlugin({ apiKey }, 'full'), /resolved apiKey/);
});

test('read-only discovery accepts a SecretRef but full runtime still requires a resolved key', async () => {
  const apiKey = { source: 'env', provider: 'default', id: 'OAM_USER_API_KEY' };
  const { hooks } = await installedPlugin({ apiKey }, 'discovery');
  assert.equal(typeof hooks.before_prompt_build, 'function');
  await assert.rejects(installedPlugin({ apiKey }, 'full'), /resolved apiKey/);
});

test('inspection never sends an unresolved credential to OAM', async () => {
  requests.length = 0;
  const apiKey = { source: 'env', provider: 'default', id: 'OAM_USER_API_KEY' };
  const { tool } = await installedPlugin({ apiKey }, 'tool-discovery');
  const search = tool.create({
    agentId: 'main', messageChannel: 'telegram', nativeChannelId: '42', requesterSenderId: '7',
    conversationReadOrigin: 'direct-operator', assertInvocationCurrent() {},
  });
  const result = await search.execute('inspect-call', { query: 'Lin preference' });
  assert.equal(result.isError, true);
  assert.equal(requests.length, 0);
});

const route = {
  agentId: 'main', trigger: 'user', inputProvenance: { kind: 'external_user', sourceChannel: 'telegram' }, channel: 'telegram', chatId: '42', senderId: '7', sessionId: 'session-1',
  toolAuthority: { allows: (name) => name === 'memory_search', assertActive() {} },
  hookInvocation: { assertActive() {} },
};

test('owner-authorized local main turn recalls and captures its exact human text', async () => {
  requests.length = 0;
  const { hooks, tool } = await installedPlugin({ allowLocalMainAgent: true, allowedDirectConversations: [] });
  const ownerTool = tool.create({
    agentId: 'main', sessionKey: 'agent:main:openresponses-user:owner',
    messageChannel: 'webchat', senderIsOwner: true,
    conversationReadOrigin: 'delegated', assertInvocationCurrent() {},
  });
  assert.ok(ownerTool);
  const localRoute = {
    agentId: 'main', runId: 'response-local-1', sessionKey: 'agent:main:openresponses-user:owner', sessionId: 'session-local',
    channel: 'webchat', chatId: 'webchat', trigger: 'user',
    toolAuthority: { allows: (name) => name === 'memory_search' && Boolean(ownerTool), assertActive() {} },
    hookInvocation: { assertActive() {} },
  };
  const recalled = await hooks.before_prompt_build({ prompt: 'Remember I prefer concise answers.', messages: [] }, localRoute);
  assert.match(recalled.prependContext, /\[source: s1\] Lin prefers concise answers/);
  await hooks.before_agent_run({ senderIsOwner: true }, localRoute);
  await hooks.agent_end({ success: true, messages: [
    { role: 'user', content: [{ type: 'text', text: 'Remember I prefer concise answers.' }], timestamp: 1780000000000 },
  ] }, localRoute);
  assert.deepEqual(requests.map((request) => request.path), ['/v1/recall', '/v1/retain']);
  assert.equal(requests[1].body.messages[0].content, 'Remember I prefer concise answers.');
});

test('owner default main WebChat turn recalls and captures its exact human text', async () => {
  requests.length = 0;
  const { hooks, tool } = await installedPlugin({ allowLocalMainAgent: true, allowedDirectConversations: [] });
  const sessionKey = 'agent:main:main';
  const ownerTool = tool.create({
    agentId: 'main', sessionKey, messageChannel: 'webchat', senderIsOwner: true,
    conversationReadOrigin: 'delegated', assertInvocationCurrent() {},
  });
  assert.ok(ownerTool);
  const context = {
    agentId: 'main', runId: 'default-main-owner-1', sessionKey, sessionId: 'default-main-session',
    channel: 'webchat', chatId: 'webchat', trigger: 'user',
    toolAuthority: { allows: (name) => name === 'memory_search' && Boolean(ownerTool), assertActive() {} },
    hookInvocation: { assertActive() {} },
  };
  const prompt = 'My family lives in Shanghai.';
  const recalled = await hooks.before_prompt_build({ prompt, messages: [] }, context);
  assert.match(recalled.prependContext, /Lin prefers concise answers/);
  await hooks.before_agent_run({ senderIsOwner: true }, context);
  await hooks.agent_end({ success: true, messages: [
    { role: 'user', content: [{ type: 'text', text: prompt }], timestamp: 1780000000000 },
  ] }, context);
  assert.deepEqual(requests.map((request) => request.path), ['/v1/recall', '/v1/retain']);
  assert.equal(requests[1].body.messages[0].content, prompt);
});

test('owner custom WebChat session for the target agent uses OAM', async () => {
  requests.length = 0;
  const { hooks, tool } = await installedPlugin({ allowLocalMainAgent: true, allowedDirectConversations: [] });
  const sessionKey = 'agent:main:family-planning';
  const ownerTool = tool.create({
    agentId: 'main', sessionKey, messageChannel: 'webchat', senderIsOwner: true,
    conversationReadOrigin: 'delegated', assertInvocationCurrent() {},
  });
  assert.ok(ownerTool);
  const context = {
    agentId: 'main', runId: 'custom-owner-1', sessionKey, sessionId: 'custom-session',
    channel: 'webchat', chatId: 'webchat', trigger: 'user',
    toolAuthority: { allows: (name) => name === 'memory_search' && Boolean(ownerTool), assertActive() {} },
    hookInvocation: { assertActive() {} },
  };
  const prompt = 'Remember my family visits every summer.';
  const recalled = await hooks.before_prompt_build({ prompt, messages: [] }, context);
  assert.match(recalled.prependContext, /Lin prefers concise answers/);
  await hooks.before_agent_run({ senderIsOwner: true }, context);
  await hooks.agent_end({ success: true, messages: [
    { role: 'user', content: prompt, timestamp: 1780000000000 },
  ] }, context);
  assert.deepEqual(requests.map(({ path }) => path), ['/v1/recall', '/v1/retain']);
});

test('configured agent owns its default WebChat memory route', async () => {
  requests.length = 0;
  const { hooks, tool } = await installedPlugin({
    targetAgentId: 'research', allowLocalMainAgent: true, allowedDirectConversations: [],
  });
  const sessionKey = 'agent:research:main';
  const ownerTool = tool.create({
    agentId: 'research', sessionKey, messageChannel: 'webchat', senderIsOwner: true,
    conversationReadOrigin: 'delegated', assertInvocationCurrent() {},
  });
  assert.ok(ownerTool);
  assert.equal(tool.create({
    agentId: 'main', sessionKey: 'agent:main:main', messageChannel: 'webchat', senderIsOwner: true,
    conversationReadOrigin: 'delegated', assertInvocationCurrent() {},
  }), null);
  const context = {
    agentId: 'research', runId: 'research-owner-1', sessionKey, sessionId: 'research-session',
    channel: 'webchat', chatId: 'webchat', trigger: 'user',
    toolAuthority: { allows: (name) => name === 'memory_search' && Boolean(ownerTool), assertActive() {} },
    hookInvocation: { assertActive() {} },
  };
  const prompt = 'My preferred answer style is concise.';
  const recalled = await hooks.before_prompt_build({ prompt, messages: [] }, context);
  assert.match(recalled.prependContext, /Lin prefers concise answers/);
  await hooks.before_agent_run({ senderIsOwner: true }, context);
  await hooks.agent_end({ success: true, messages: [
    { role: 'user', content: prompt, timestamp: 1780000000000 },
  ] }, context);
  assert.deepEqual(requests.map(({ path }) => path), ['/v1/recall', '/v1/retain']);
});

test('configured agent limits approved direct routes to that agent', async () => {
  requests.length = 0;
  const { hooks, tool } = await installedPlugin({ targetAgentId: 'research' });
  const approved = { ...route, agentId: 'research' };
  const recalled = await hooks.before_prompt_build({ prompt: 'My preference?', messages: [] }, approved);
  assert.match(recalled.prependContext, /Lin prefers concise answers/);
  assert.equal(await hooks.before_prompt_build({ prompt: 'My preference?', messages: [] }, { ...route, agentId: 'main' }), undefined);
  assert.ok(tool.create({
    agentId: 'research', messageChannel: 'telegram', nativeChannelId: '42', requesterSenderId: '7',
    conversationReadOrigin: 'direct-operator', assertInvocationCurrent() {},
  }));
  assert.equal(tool.create({
    agentId: 'main', messageChannel: 'telegram', nativeChannelId: '42', requesterSenderId: '7',
    conversationReadOrigin: 'direct-operator', assertInvocationCurrent() {},
  }), null);
  assert.deepEqual(requests.map(({ path }) => path), ['/v1/recall']);
});

test('invalid target agent IDs are rejected', async () => {
  await assert.rejects(installedPlugin({ targetAgentId: 'research:main' }), /targetAgentId/);
});

test('owner Dashboard main turn recalls and captures the matching human prompt', async () => {
  requests.length = 0;
  const { hooks, tool } = await installedPlugin({ allowLocalMainAgent: true, allowedDirectConversations: [] });
  const sessionKey = 'agent:main:dashboard:455794fd-29c0-4c7d-a189-48ec17127417';
  const ownerTool = tool.create({
    agentId: 'main', sessionKey, messageChannel: 'webchat', senderIsOwner: true,
    conversationReadOrigin: 'delegated', assertInvocationCurrent() {},
  });
  assert.ok(ownerTool);
  const context = {
    agentId: 'main', runId: 'dashboard-owner-1', sessionKey, sessionId: 'dashboard-owner-session',
    channel: 'webchat', chatId: sessionKey, trigger: 'user',
    toolAuthority: { allows: (name) => name === 'memory_search' && Boolean(ownerTool), assertActive() {} },
    hookInvocation: { assertActive() {} },
  };
  const recalled = await hooks.before_prompt_build({ prompt: 'What does Lin prefer?', messages: [] }, context);
  assert.match(recalled.prependContext, /Lin prefers concise answers/);
  await hooks.before_agent_run({ senderIsOwner: true }, context);
  await hooks.agent_end({ success: true, messages: [
    { role: 'user', content: [{ type: 'text', text: 'What does Lin prefer?' }], timestamp: 1780000000000 },
  ] }, context);
  assert.deepEqual(requests.map((request) => request.path), ['/v1/recall', '/v1/retain']);
});

test('owner Dashboard memory_search uses OAM recall', async () => {
  requests.length = 0;
  const { tool } = await installedPlugin({ allowLocalMainAgent: true, allowedDirectConversations: [] });
  const search = tool.create({
    agentId: 'main', sessionKey: 'agent:main:dashboard:455794fd-29c0-4c7d-a189-48ec17127417',
    messageChannel: 'webchat', senderIsOwner: true,
    conversationReadOrigin: 'delegated', assertInvocationCurrent() {},
  });
  assert.ok(search);
  const result = await search.execute('dashboard-search', { query: 'Lin preference' });
  assert.match(result.content[0].text, /Lin prefers concise answers/);
  assert.deepEqual(requests.map((request) => request.path), ['/v1/recall']);
});

test('local WebChat memory excludes nonowners, incognito, internal, scheduled, channel and other-agent turns', async () => {
  requests.length = 0;
  const { hooks, tool } = await installedPlugin({ allowLocalMainAgent: true, allowedDirectConversations: [] });
  const key = 'agent:main:dashboard:455794fd-29c0-4c7d-a189-48ec17127417';
  const baseTool = { agentId: 'main', sessionKey: key, messageChannel: 'webchat',
    conversationReadOrigin: 'delegated', assertInvocationCurrent() {} };
  for (const denied of [
    { ...baseTool, senderIsOwner: false },
    { ...baseTool, agentId: 'guest', senderIsOwner: true },
    { ...baseTool, sessionKey: 'agent:main:dashboard:incognito-455794fd-29c0-4c7d-a189-48ec17127417', senderIsOwner: true },
    { ...baseTool, sessionKey: 'agent:main:main', senderIsOwner: false },
    { ...baseTool, sessionKey: 'agent:main:cron:job-1', senderIsOwner: true },
    { ...baseTool, sessionKey: 'agent:main:subagent:child-1', senderIsOwner: true },
    { ...baseTool, sessionKey: 'agent:main:openclaw-weixin:group:room-1', senderIsOwner: true },
    { ...baseTool, messageChannel: 'telegram', senderIsOwner: true },
  ]) assert.equal(tool.create(denied), null);
  const context = { agentId: 'main', runId: 'dashboard-internal', sessionKey: key,
    channel: 'webchat', chatId: 'webchat', trigger: 'user', inputProvenance: { kind: 'inter_session' },
    toolAuthority: { allows: () => false, assertActive() {} }, hookInvocation: { assertActive() {} } };
  assert.equal(await hooks.before_prompt_build({ prompt: 'private?', messages: [] }, context), undefined);
  await hooks.agent_end({ success: true, messages: [
    { role: 'user', content: 'private', timestamp: 1780000000000 },
  ] }, context);
  assert.equal(requests.length, 0);
  const wrongChat = { ...context, runId: 'dashboard-wrong-chat', inputProvenance: undefined,
    chatId: 'agent:main:dashboard:12345678-1234-1234-1234-123456789abc',
    toolAuthority: { allows: () => true, assertActive() {} } };
  assert.equal(await hooks.before_prompt_build({ prompt: 'private?', messages: [] }, wrongChat), undefined);
  assert.equal(requests.length, 0);
});

test('local main route rejects nonowner tools and messages', async () => {
  requests.length = 0;
  const { hooks, tool } = await installedPlugin({ allowLocalMainAgent: true, allowedDirectConversations: [] });
  assert.equal(tool.create({
    agentId: 'main', sessionKey: 'agent:main:openresponses-user:visitor',
    messageChannel: 'webchat', senderIsOwner: false,
    conversationReadOrigin: 'direct-operator', assertInvocationCurrent() {},
  }), null);
  const localRoute = {
    agentId: 'main', runId: 'response-visitor-1', sessionKey: 'agent:main:openresponses-user:visitor', sessionId: 'visitor-session',
    channel: 'webchat', chatId: 'webchat', trigger: 'user',
    toolAuthority: { allows: () => false, assertActive() {} }, hookInvocation: { assertActive() {} },
  };
  assert.equal(await hooks.before_prompt_build({ prompt: 'private?', messages: [] }, localRoute), undefined);
  await hooks.before_agent_run({ senderIsOwner: false }, localRoute);
  await hooks.agent_end({ success: true, messages: [
    { role: 'user', content: [{ type: 'text', text: 'private' }], timestamp: 1780000000000 },
  ] }, localRoute);
  assert.equal(requests.length, 0);
});

test('local main capture skips a different user message added after owner admission', async () => {
  requests.length = 0;
  const { hooks } = await installedPlugin({ allowLocalMainAgent: true, allowedDirectConversations: [] });
  const context = {
    agentId: 'main', runId: 'response-steered-1', sessionKey: 'agent:main:openresponses-user:owner', sessionId: 'session-steered',
    channel: 'webchat', chatId: 'webchat', trigger: 'user',
    toolAuthority: { allows: () => true, assertActive() {} }, hookInvocation: { assertActive() {} },
  };
  await hooks.before_prompt_build({ prompt: 'Original user text.', messages: [] }, context);
  await hooks.before_agent_run({ senderIsOwner: true }, context);
  await hooks.agent_end({ success: true, messages: [
    { role: 'user', content: [{ type: 'text', text: 'Different steered text.' }], timestamp: 1780000000000 },
  ] }, context);
  assert.deepEqual(requests.map((request) => request.path), ['/v1/recall']);
});

test('authorized direct turn recalls cited context and captures exact keyed human text', async () => {
  requests.length = 0;
  const { hooks } = await installedPlugin();
  const recalled = await hooks.before_prompt_build({ prompt: 'What does Lin prefer?', messages: [] }, route);
  assert.match(recalled.prependContext, /Lin prefers concise answers/);
  await hooks.agent_end({ success: true, messages: [
    { role: 'user', content: 'Remember I prefer concise answers.', timestamp: 1780000000000, idempotencyKey: 'turn-1' },
    { role: 'assistant', content: 'Sure.' },
  ] }, route);
  assert.deepEqual(requests.map((request) => request.path), ['/v1/recall', '/v1/retain']);
  assert.equal(requests[0].body.user_id, 'u1');
  assert.equal(requests[0].body.recall_level, 'fast');
  assert.equal(requests[0].body.query, 'What does Lin prefer?');
  assert.deepEqual(requests[1].body.messages.map(({ role, content }) => ({ role, content })), [
    { role: 'user', content: 'Remember I prefer concise answers.' },
  ]);
  assert.equal(requests[1].body.input_type, 'interaction');
  assert.equal(requests[1].headers.authorization, 'Bearer private-key');
  assert.ok(requests[1].headers['idempotency-key']);
});

test('unapproved sender cannot recall, capture, or use the tool', async () => {
  requests.length = 0;
  const { hooks, tool } = await installedPlugin();
  const denied = { ...route, senderId: 'other' };
  assert.equal(await hooks.before_prompt_build({ prompt: 'private?', messages: [] }, denied), undefined);
  await hooks.agent_end({ success: true, messages: [{ role: 'user', content: 'secret', timestamp: 1780000000000, idempotencyKey: 'turn-2' }] }, denied);
  assert.equal(tool.create({
    agentId: 'main', messageChannel: 'telegram', nativeChannelId: '42', requesterSenderId: 'other',
    conversationReadOrigin: 'direct-operator', assertInvocationCurrent() {},
  }), null);
  assert.equal(requests.length, 0);
});

test('missing or internal provenance cannot use an approved hook route', async () => {
  requests.length = 0;
  const { hooks } = await installedPlugin();
  for (const inputProvenance of [undefined, { kind: 'inter_session' }, { kind: 'internal_system' }]) {
    const context = { ...route, inputProvenance };
    assert.equal(await hooks.before_prompt_build({ prompt: 'private?', messages: [] }, context), undefined);
    await hooks.agent_end({ success: true, messages: [
      { role: 'user', content: 'secret', timestamp: 1780000000000, idempotencyKey: 'turn-internal' },
    ] }, context);
  }
  assert.equal(requests.length, 0);
});

test('mixed media user turn is not retained as plain text memory', async () => {
  requests.length = 0;
  const { hooks } = await installedPlugin();
  await hooks.agent_end({ success: true, messages: [
    { role: 'user', content: 'Please inspect this image', attachments: [{ kind: 'image' }], timestamp: 1780000000000, idempotencyKey: 'image-turn' },
  ] }, route);
  assert.equal(requests.length, 0);
});

test('persisted media metadata prevents plain text capture', async () => {
  requests.length = 0;
  const { hooks } = await installedPlugin();
  await hooks.agent_end({ success: true, messages: [
    { role: 'user', content: 'Please inspect this image', __openclaw: { media: [{ kind: 'image', url: 'https://example.invalid/image.png' }] }, timestamp: 1780000000000, idempotencyKey: 'persisted-image-turn' },
  ] }, route);
  assert.equal(requests.length, 0);
});

test('last user message with internal provenance is not captured', async () => {
  requests.length = 0;
  const { hooks } = await installedPlugin();
  await hooks.agent_end({ success: true, messages: [
    { role: 'user', content: 'Original direct text', timestamp: 1780000000000, idempotencyKey: 'direct-turn', provenance: { kind: 'external_user' } },
    { role: 'user', content: 'Internal handoff', timestamp: 1780000000001, idempotencyKey: 'internal-turn', provenance: { kind: 'inter_session' } },
  ] }, route);
  assert.equal(requests.length, 0);
});

test('explicit search uses the fixed user and defaults to mid recall', async () => {
  requests.length = 0;
  const { tool } = await installedPlugin();
  const search = tool.create({
    agentId: 'main', messageChannel: 'telegram', nativeChannelId: '42', requesterSenderId: '7',
    conversationReadOrigin: 'direct-operator', assertInvocationCurrent() {},
  });
  const result = await search.execute('call-1', { query: '  Lin preference  ' });
  assert.match(result.content[0].text, /Lin prefers concise answers/);
  assert.deepEqual(requests.map(({ path }) => path), ['/v1/recall']);
  assert.equal(requests[0].body.user_id, 'u1');
  assert.equal(requests[0].body.recall_level, 'mid');
  assert.equal(requests[0].body.query, 'Lin preference');
});

test('missing tool origin is delegated and cannot read an approved route', async () => {
  requests.length = 0;
  const { tool } = await installedPlugin();
  assert.equal(tool.create({
    agentId: 'main', messageChannel: 'telegram', nativeChannelId: '42', requesterSenderId: '7',
    assertInvocationCurrent() {},
  }), null);
  assert.equal(requests.length, 0);
});

test('cancelled explicit search aborts its OAM request promptly', async () => {
  requests.length = 0;
  const { tool } = await installedPlugin();
  const search = tool.create({
    agentId: 'main', messageChannel: 'telegram', nativeChannelId: '42', requesterSenderId: '7',
    conversationReadOrigin: 'direct-operator', assertInvocationCurrent() {},
  });
  const controller = new AbortController();
  stallRecall = true;
  const started = Date.now();
  try {
    const pending = search.execute('call-cancelled', { query: 'Lin preference' }, controller.signal);
    setTimeout(() => controller.abort(), 50);
    const result = await pending;
    assert.equal(result.isError, true);
    assert.ok(Date.now() - started < 2_000);
  } finally {
    stallRecall = false;
  }
});

test('a duplicate callback uses the same idempotency key and changed turn uses a new key', async () => {
  requests.length = 0;
  const { hooks } = await installedPlugin();
  const event = { success: true, messages: [
    { role: 'user', content: 'Same text', timestamp: 1780000000000, idempotencyKey: 'turn-a' },
  ] };
  await hooks.agent_end(event, route);
  await hooks.agent_end(event, route);
  await hooks.agent_end({ ...event, messages: [
    { ...event.messages[0], idempotencyKey: 'turn-b' },
  ] }, route);
  assert.deepEqual(requests.map(({ path }) => path), ['/v1/retain', '/v1/retain', '/v1/retain']);
  assert.equal(requests[0].headers['idempotency-key'], requests[1].headers['idempotency-key']);
  assert.notEqual(requests[0].headers['idempotency-key'], requests[2].headers['idempotency-key']);
  assert.equal(requests[0].body.messages[0].content, requests[2].body.messages[0].content);
});

test('revoked prompt tool authority never injects returned memory', async () => {
  requests.length = 0;
  const { hooks } = await installedPlugin();
  let active = true;
  const guarded = {
    ...route,
    toolAuthority: {
      allows: () => true,
      assertActive() { if (!active) throw new Error('revoked'); },
    },
    hookInvocation: { assertActive() { active = false; } },
  };
  assert.equal(await hooks.before_prompt_build({ prompt: 'Lin preference', messages: [] }, guarded), undefined);
});

test('stalled automatic recall aborts within its wait limit and a completed turn still captures', async () => {
  requests.length = 0;
  const { hooks } = await installedPlugin();
  stallRecall = true;
  const started = Date.now();
  try {
    assert.equal(await hooks.before_prompt_build({ prompt: 'Lin preference', messages: [] }, route), undefined);
  } finally {
    stallRecall = false;
  }
  assert.ok(Date.now() - started < 6_000);
  await hooks.agent_end({ success: true, messages: [
    { role: 'user', content: 'Remember this', timestamp: 1780000000000, idempotencyKey: 'after-timeout' },
  ] }, route);
  assert.deepEqual(requests.map(({ path }) => path), ['/v1/recall', '/v1/retain']);
});
