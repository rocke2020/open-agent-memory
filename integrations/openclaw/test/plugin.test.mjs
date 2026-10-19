import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { after, before, test } from 'node:test';

let server;
let baseUrl;
const requests = [];
let stallRecall = false;
let removalResponse;
let retainResponses = {};

before(async () => {
  server = createServer(async (request, response) => {
    let body = '';
    for await (const part of request) body += part;
    requests.push({ method: request.method, path: request.url, headers: request.headers, body: JSON.parse(body || '{}') });
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
      const retained = JSON.parse(body);
      const configured = retainResponses[retained.input_type];
      response.statusCode = configured?.status ?? 202;
      response.end(JSON.stringify(configured?.body ?? { operation_id: 'op1', source_id: 's1', revision: 1, readiness: { accepted: true } }));
    } else if (request.method === 'DELETE' && /^\/v1\/users\/u1\/(?:memories\/mem_42|insights\/ins_7)$/.test(request.url)) {
      const insight = request.url.includes('/insights/');
      response.statusCode = removalResponse?.status ?? 202;
      response.end(JSON.stringify(removalResponse?.body ?? {
        target_kind: insight ? 'insight' : 'memory',
        [insight ? 'insight_id' : 'memory_id']: insight ? 'ins_7' : 'mem_42',
        status: 'excluded', policy_epoch: 8, operation_id: 'op_remove', cleanup_status: 'accepted',
      }));
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
  let removeTool;
  const logs = [];
  const api = {
    registrationMode,
    pluginConfig: {
      baseUrl,
      userId: 'u1',
      apiKey: 'private-key',
      allowedDirectConversations: [{ channel: 'telegram', chatId: '42', senderId: '7' }],
      ...pluginConfig,
    },
    logger: { info(message) { logs.push(message); }, warn(message) { logs.push(message); }, error() {} },
    registerMemoryCapability() {},
    registerTool(factory, options) {
      if (options.name === 'memory_remove') removeTool = factory;
      else tool = factory;
    },
    on(name, callback) { hooks[name] = callback; },
  };
  plugin.register(api);
  return { hooks, tool, removeTool, logs };
}

test('plugin manifest declares every registered memory tool', async () => {
  const manifest = JSON.parse(await readFile(new URL('../openclaw.plugin.json', import.meta.url), 'utf8'));
  const { tool, removeTool } = await installedPlugin();
  assert.ok(tool && removeTool);
  assert.deepEqual(manifest.contracts.tools, ['memory_search', 'memory_remove']);
});

test('built plugin loads all cron code from its shipped JavaScript entrypoint', async () => {
  const build = spawnSync('npm', ['run', 'build'], { cwd: new URL('../', import.meta.url), encoding: 'utf8' });
  assert.equal(build.status, 0, build.stderr);
  const { default: built } = await import('../dist/index.js');
  assert.equal(built.id, 'oam-openclaw');
  assert.equal(typeof built.register, 'function');
});

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

const cronJob = {
  id: 'job-news', agentId: 'main', name: 'Daily news', description: 'Read the daily news',
  enabled: true, schedule: { kind: 'cron', expr: '0 7 * * *', tz: 'Asia/Shanghai' },
  sessionTarget: 'session:news', wakeMode: 'now',
  payload: { kind: 'agentTurn', message: 'Summarize today\'s science news.', model: 'model-secret' },
  createdAtMs: 1780000000000, updatedAtMs: 1780000001000,
  state: { lastRunAtMs: 1780000002000, lastRunStatus: 'ok', lastDurationMs: 123 },
};

test('cron reconciliation learns enabled user tasks and skips inactive or maintenance jobs', async () => {
  requests.length = 0;
  const { hooks } = await installedPlugin({ captureCronJobs: true });
  const controller = new AbortController();
  assert.equal(typeof hooks.cron_reconciled, 'function', 'cron definitions need a reconciliation hook');
  await hooks.cron_reconciled({ reason: 'startup', enabled: true }, {
    abortSignal: controller.signal,
    getCron: () => ({ async list(options) {
      assert.deepEqual(options, { includeDisabled: true });
      return [cronJob, { ...cronJob, id: 'disabled-job', enabled: false, state: {} },
        { ...cronJob, id: 'maintenance-job', payload: { kind: 'heartbeat' } },
        { ...cronJob, id: 'foreign-job', agentId: 'research' }, { ...cronJob, id: 'unknown-job', agentId: undefined }];
    } }),
  });
  assert.equal(requests.length, 1);
  assert.match(requests[0].body.messages[0].content, /Read the daily news/);
  assert.equal(requests[0].body.messages[0].learning_eligible, true);
  assert.ok(requests.every((r) => !JSON.stringify(r.body).includes('model-secret')));
});

test('cron heartbeat events never reach OAM even with errors or result text', async () => {
  requests.length = 0;
  const { hooks, logs } = await installedPlugin({ captureCronJobs: true });
  for (const enabled of [true, false]) {
    const job = { ...cronJob, enabled, payload: { kind: 'heartbeat' } };
    for (const action of ['added', 'updated', 'removed', 'finished']) {
      await hooks.cron_changed({ job, jobId: job.id, action, runAtMs: 1780000002000,
        status: 'error', error: 'Maintenance failure', summary: 'Heartbeat diagnostics' }, {});
    }
  }
  assert.equal(requests.length, 0);
  assert.ok(logs.some((line) => /skipped.*reason=maintenance-payload/.test(line)));
});

test('cron definitions keep purpose and schedule without internal metadata or workflow prompts', async () => {
  requests.length = 0;
  const { hooks } = await installedPlugin({ captureCronJobs: true });
  const job = { ...cronJob, schedule: { kind: 'every', everyMs: 1800000, anchorMs: 471294, staggerMs: 1234 } };
  await hooks.cron_changed({ action: 'added', jobId: job.id, job }, {});
  const content = requests[0].body.messages[0].content;
  assert.match(content, /Daily news/);
  assert.match(content, /Read the daily news/);
  assert.match(content, /1800000/);
  assert.doesNotMatch(content, /job-news|agentId|session:news|wakeMode|anchorMs|staggerMs|model-secret|science news/);
});

test('cron tasks without descriptions preserve their name and timing without learning payload prompts', async () => {
  requests.length = 0;
  const { hooks } = await installedPlugin({ captureCronJobs: true });
  const job = { ...cronJob, name: 'heartbeat research', description: undefined,
    payload: { kind: 'agentTurn', message: '# Weekly research\n\nReport new systems research.\n\n## Procedure\n\nRun a long operational workflow.' } };
  await hooks.cron_changed({ action: 'added', jobId: job.id, job }, {});
  const content = requests[0].body.messages[0].content;
  assert.match(content, /heartbeat research/);
  assert.match(content, /0 7 \* \* \*/);
  assert.doesNotMatch(content, /Report new systems research|Procedure|operational workflow/);
});

test('cron command and script tasks never learn executable payloads', async () => {
  requests.length = 0;
  const { hooks } = await installedPlugin({ captureCronJobs: true });
  for (const payload of [{ kind: 'command', argv: ['run', 'secret-command'] },
    { kind: 'script', script: 'SECRET_SCRIPT=private' }, { kind: 'systemEvent', text: 'private instructions' }]) {
    const job = { ...cronJob, payload };
    await hooks.cron_changed({ action: 'added', jobId: job.id, job }, {});
  }
  assert.equal(requests.length, 3);
  for (const request of requests) {
    assert.match(request.body.messages[0].content, /Read the daily news/);
    assert.doesNotMatch(request.body.messages[0].content, /secret-command|SECRET_SCRIPT|private instructions/);
  }
});

test('cron execution and delivery diagnostics are never archived as memory', async () => {
  requests.length = 0;
  const { hooks } = await installedPlugin({ captureCronJobs: true });
  for (const outcome of [{ status: 'error' }, { status: 'ok', deliveryError: 'Recipient unavailable' }]) {
    await hooks.cron_changed({ action: 'finished', jobId: cronJob.id, job: { ...cronJob, enabled: false },
      runAtMs: 1780000002000, summary: 'NO_REPLY', ...outcome }, {});
  }
  assert.equal(requests.length, 0);
});

test('cron successful report content enters learning as assistant-authored text', async () => {
  requests.length = 0;
  const { hooks } = await installedPlugin({ captureCronJobs: true });
  const event = { action: 'finished', jobId: cronJob.id, job: cronJob, agentId: 'main',
    runAtMs: 1780000002000, durationMs: 123, status: 'ok', completionStatus: 'succeeded',
    deliveryStatus: 'delivered', sessionId: 'news-session', runId: 'news-run',
    summary: 'Research result: use <tag> literally.\nSource https://example.com/report' };
  await hooks.cron_changed(event, {});
  assert.equal(requests.length, 1);
  const [report] = requests;
  assert.equal(report.body.input_type, 'interaction');
  assert.equal(report.path, '/v1/retain');
  assert.equal(report.body.user_id, 'u1');
  assert.equal(report.body.messages.length, 1);
  const message = report.body.messages[0];
  assert.equal(message.role, 'assistant');
  assert.equal(message.learning_eligible, true);
  assert.equal(message.content, event.summary);
  assert.doesNotMatch(message.content, /news-run|news-session|jobId|durationMs|deliveryStatus|completionStatus/);
  assert.equal(message.event_time, '2026-05-28T20:26:42.000Z');
  await hooks.cron_changed(event, {});
  assert.deepEqual(requests[1].body, report.body);
  assert.equal(requests[1].headers['idempotency-key'], report.headers['idempotency-key']);
});

test('cron transient task definitions are skipped while their successful reports can be learned', async () => {
  requests.length = 0;
  const { hooks } = await installedPlugin({ captureCronJobs: true });
  for (const schedule of [{ kind: 'at', at: '2026-05-28T20:30:00Z' },
    { kind: 'on-exit', command: 'wait-for-build' }, { kind: 'stream', command: ['watch-build'] }]) {
    const job = { ...cronJob, schedule };
    for (const action of ['added', 'updated', 'removed']) {
      await hooks.cron_changed({ action, jobId: job.id, job }, {});
    }
    assert.equal(requests.length, 0);
  }
  await hooks.cron_changed({ action: 'finished', jobId: cronJob.id,
    job: { ...cronJob, schedule: { kind: 'at', at: '2026-05-28T20:30:00Z' } },
    runAtMs: 1780000002000, status: 'ok', summary: 'The completed analysis is available at https://example.com/result.' }, {});
  assert.equal(requests.length, 1);
  assert.equal(requests[0].body.messages[0].role, 'assistant');
});

test('cron unsuccessful or host-suppressed output is skipped despite nonblank summary', async () => {
  requests.length = 0;
  const { hooks } = await installedPlugin({ captureCronJobs: true });
  for (const outcome of [{ status: 'error', error: 'Provider unavailable' }, { status: 'skipped' },
    { status: 'ok', error: 'Incomplete execution' },
    ...['empty', 'silent', 'heartbeat', 'channel_transform'].map((deliverySuppressionReason) => ({ status: 'ok', deliverySuppressionReason }))]) {
    await hooks.cron_changed({ action: 'finished', jobId: cronJob.id, job: cronJob,
      runAtMs: 1780000002000, summary: 'A nonblank status message.', ...outcome }, {});
  }
  assert.equal(requests.length, 0);
});

test('cron execution-success report is learned even when delivery or overall completion failed', async () => {
  requests.length = 0;
  const { hooks } = await installedPlugin({ captureCronJobs: true });
  await hooks.cron_changed({ action: 'finished', jobId: cronJob.id, job: { ...cronJob, enabled: false },
    runAtMs: 1780000002000, status: 'ok', completionStatus: 'failed', deliveryStatus: 'not-delivered',
    deliveryError: 'Recipient unavailable', summary: 'A complete report with source https://example.com/source.' }, {});
  assert.equal(requests.length, 1);
  assert.equal(requests[0].body.messages[0].learning_eligible, true);
  assert.ok(requests[0].body.messages[0].content.endsWith('https://example.com/source.'));
  assert.doesNotMatch(requests[0].body.messages[0].content, /Recipient unavailable/);
});

test('cron routine outcomes and standalone host control replies make no OAM request', async () => {
  requests.length = 0;
  const { hooks } = await installedPlugin({ captureCronJobs: true });
  for (const summary of [undefined, '', '  ', 'HEARTBEAT_OK', 'NO_REPLY', ' NO_REPLY\nNO_REPLY ']) {
    for (const status of ['ok', 'skipped']) {
      await hooks.cron_changed({ action: 'finished', jobId: cronJob.id, job: cronJob,
        runAtMs: 1780000002000, status, summary, delivered: true, durationMs: 123 }, {});
    }
  }
  assert.equal(requests.length, 0);
  await hooks.cron_changed({ action: 'finished', jobId: cronJob.id, job: cronJob,
    runAtMs: 1780000002000, status: 'ok', summary: 'The report explains how NO_REPLY works.' }, {});
  assert.equal(requests.length, 1);
  assert.equal(requests[0].body.input_type, 'interaction');
});

test('cron definitions ignore runtime state and distinguish removed jobs', async () => {
  requests.length = 0;
  const { hooks } = await installedPlugin({ captureCronJobs: true });
  for (const event of [
    { action: 'added', jobId: cronJob.id, job: cronJob },
    { action: 'updated', jobId: cronJob.id, job: { ...cronJob, state: { nextRunAtMs: 1789999999000 } } },
    { action: 'removed', jobId: cronJob.id, job: cronJob },
  ]) await hooks.cron_changed(event, {});
  assert.deepEqual(requests[0].body, requests[1].body);
  assert.equal(requests[0].headers['idempotency-key'], requests[1].headers['idempotency-key']);
  assert.notEqual(requests[0].headers['idempotency-key'], requests[2].headers['idempotency-key']);
  assert.match(requests[2].body.messages[0].content, /removed/);
  await hooks.cron_changed({ action: 'updated', jobId: cronJob.id, job: { ...cronJob, enabled: false } }, {});
  assert.match(requests[3].body.messages[0].content, /disabled/);
});

test('cron reconciliation never backfills transient execution failures', async () => {
  requests.length = 0;
  const { hooks } = await installedPlugin({ captureCronJobs: true });
  await hooks.cron_changed({ action: 'finished', jobId: cronJob.id, job: cronJob,
    runAtMs: cronJob.state.lastRunAtMs, durationMs: 123, status: 'error', error: 'Provider unavailable',
    completionStatus: 'failed', sessionId: 'news-session', sessionKey: 'agent:main:news', runId: 'live-run' }, {});
  assert.equal(requests.length, 0);
  await hooks.cron_reconciled({ reason: 'reload', enabled: true }, {
    getCron: () => ({ async list() { return [{ ...cronJob,
      state: { ...cronJob.state, lastRunStatus: 'error', lastError: 'Provider unavailable' } }]; } }),
  });
  assert.equal(requests.length, 1);
  assert.equal(requests[0].body.messages[0].role, 'user');
  assert.doesNotMatch(requests[0].body.messages[0].content, /Provider unavailable/);
});

test('cron capture is opt-in and rejects foreign, malformed and cancelled events', async () => {
  requests.length = 0;
  const disabled = await installedPlugin();
  assert.equal(disabled.hooks.cron_changed, undefined);
  await assert.rejects(installedPlugin({ captureCronJobs: 'yes' }), /captureCronJobs/);
  const { hooks } = await installedPlugin({ captureCronJobs: true });
  for (const event of [
    { action: 'updated', jobId: 'other-id', job: cronJob },
    { action: 'updated', jobId: cronJob.id, job: { ...cronJob, agentId: 'research' } },
    { action: 'updated', jobId: cronJob.id, job: { ...cronJob, updatedAtMs: 1e30 } },
    { action: 'started', jobId: cronJob.id, job: cronJob },
    { action: 'finished', jobId: cronJob.id, job: cronJob, runAtMs: 1e30, status: 'ok' },
  ]) await hooks.cron_changed(event, {});
  const controller = new AbortController();
  await hooks.cron_reconciled({ reason: 'reload', enabled: false }, {
    abortSignal: controller.signal,
    getCron: () => ({ async list() { controller.abort(); return [cronJob]; } }),
  });
  assert.equal(requests.length, 0);
});

test('cron report learning rejects failed or invalid acceptance receipts', async () => {
  const event = { action: 'finished', jobId: cronJob.id, job: cronJob,
    runAtMs: 1780000002000, status: 'ok', summary: 'A completed result' };
  try {
    for (const responses of [
      { interaction: { status: 503, body: {} } },
      { interaction: { status: 202, body: {} } },
    ]) {
      requests.length = 0;
      retainResponses = responses;
      const { hooks, logs } = await installedPlugin({ captureCronJobs: true });
      await hooks.cron_changed(event, {});
      assert.deepEqual(requests.map((r) => r.body.input_type), ['interaction']);
      assert.equal(logs.length, 1);
      assert.match(logs[0], /cron report job=job-news/);
      assert.equal(logs.filter((line) => line.includes('operation=')).length, 0);
      assert.ok(logs.every((line) => !line.includes('private-key')));
    }
    requests.length = 0;
    retainResponses = {};
    const { hooks } = await installedPlugin({ captureCronJobs: true });
    await hooks.cron_changed({ ...event, summary: undefined }, {});
    assert.equal(requests.length, 0);
  } finally {
    retainResponses = {};
  }
});

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

test('removal and control user messages are retained as ineligible evidence', async () => {
  requests.length = 0;
  const { hooks } = await installedPlugin();
  await hooks.agent_end({ success: true, messages: [
    { role: 'user', content: 'Remember this ordinary message.', timestamp: 1780000000000, idempotencyKey: 'ordinary' },
    { role: 'user', content: '/reset', timestamp: 1780000000001, idempotencyKey: 'reset', command: true },
  ] }, route);
  await hooks.agent_end({ success: true, messages: [
    { role: 'user', content: 'Forget this memory.', timestamp: 1780000000002, idempotencyKey: 'forget' },
    { role: 'assistant', content: [{ type: 'toolCall', id: 'remove-1', name: 'memory_remove', arguments: { memory_id: 'mem_42' } }] },
  ] }, route);
  assert.deepEqual(requests.map(({ path, body }) => ({ path, messages: body.messages })), [
    { path: '/v1/retain', messages: [{ role: 'user', content: '/reset', event_time: '2026-05-28T20:26:40.001Z', learning_eligible: false }] },
    { path: '/v1/retain', messages: [{ role: 'user', content: 'Forget this memory.', event_time: '2026-05-28T20:26:40.002Z', learning_eligible: false }] },
  ]);
});

test('host-dispatched removal results make only their current user turn ineligible', async () => {
  requests.length = 0;
  const { hooks } = await installedPlugin();
  const user = { role: 'user', content: 'Please remove that memory.', timestamp: 1780000000000, idempotencyKey: 'host-remove' };
  for (const isError of [false, true]) {
    await hooks.agent_end({ success: true, messages: [user, {
      role: 'toolResult', toolName: 'tool_call', isError,
      details: { tool: { id: 'openclaw:oam-openclaw:memory_remove', name: 'memory_remove', source: 'openclaw' } },
      content: [{ type: 'text', text: isError ? 'Unavailable.' : 'Removed.' }],
    }] }, route);
  }
  await hooks.agent_end({ success: true, messages: [user, {
    role: 'toolResult', toolName: 'tool_call', details: {
      tool: { id: 'openclaw:oam-openclaw:memory_remove', name: 'memory_remove', source: 'openclaw' },
    },
  }, { ...user, content: 'I prefer concise answers.', idempotencyKey: 'next-turn' }] }, route);
  assert.deepEqual(requests.map(({ body }) => body.messages[0].learning_eligible), [false, false, true]);
});

test('ordinary prose and slash paths still capture after an earlier removal turn', async () => {
  requests.length = 0;
  const { hooks } = await installedPlugin();
  for (const content of ['Forget deadlines sometimes; I use reminders.', '/home is my working directory.']) {
    await hooks.agent_end({ success: true, messages: [
      { role: 'user', content: 'Remove an old memory.', timestamp: 1780000000000, idempotencyKey: 'old-remove' },
      { role: 'assistant', content: [{ type: 'toolCall', id: 'old-call', name: 'memory_remove', arguments: { memory_id: 'mem_42' } }] },
      { role: 'user', content, timestamp: 1780000000001, idempotencyKey: content },
    ] }, route);
  }
  assert.deepEqual(requests.map(({ method, body }) => ({ method, content: body.messages[0].content })), [
    { method: 'POST', content: 'Forget deadlines sometimes; I use reminders.' },
    { method: 'POST', content: '/home is my working directory.' },
  ]);
});

test('marked control turns and failed removal results retain the latest user message as ineligible', async () => {
  requests.length = 0;
  const { hooks } = await installedPlugin();
  const message = { role: 'user', content: 'That one, please.', timestamp: 1780000000000, idempotencyKey: 'control' };
  await hooks.agent_end({ success: true, messages: [{ ...message, learning_eligible: false }] }, route);
  await hooks.agent_end({ success: true, messages: [message,
    { role: 'toolResult', toolCallId: 'remove-failed', toolName: 'memory_remove', isError: true,
      content: [{ type: 'text', text: 'Removal unavailable.' }] },
  ] }, route);
  assert.equal(requests.length, 2);
  for (const { path, body } of requests) {
    assert.equal(path, '/v1/retain');
    assert.deepEqual(body.messages, [{ role: 'user', content: 'That one, please.', event_time: '2026-05-28T20:26:40.000Z', learning_eligible: false }]);
  }
  assert.deepEqual(requests[0].body, requests[1].body);
  assert.equal(requests[0].headers['idempotency-key'], requests[1].headers['idempotency-key']);
});

test('known current control and typed removal requests skip automatic recall', async () => {
  requests.length = 0;
  const { hooks } = await installedPlugin();
  for (const [content, metadata] of [
    ['/reset', { command: true }],
    ['Which candidate?', { learning_eligible: false }],
    ['memory_remove(memory_id=mem_42)', {}],
    ['memory_remove(insight_id="ins_7")', {}],
  ]) {
    const message = { role: 'user', content, timestamp: 1780000000000, idempotencyKey: content, ...metadata };
    assert.equal(await hooks.before_prompt_build({ prompt: content, messages: [message] }, route), undefined);
    await hooks.agent_end({ success: true, messages: [message] }, route);
  }
  assert.deepEqual(requests.map(({ path, body }) => ({ path, eligible: body.messages[0].learning_eligible })), [
    { path: '/v1/retain', eligible: false }, { path: '/v1/retain', eligible: false },
    { path: '/v1/retain', eligible: false }, { path: '/v1/retain', eligible: false },
  ]);
});

test('old control history and typed ID mentions do not suppress ordinary recall or capture', async () => {
  requests.length = 0;
  const { hooks } = await installedPlugin();
  const old = { role: 'user', content: '/reset', command: true, timestamp: 1780000000000, idempotencyKey: 'old-control' };
  for (const content of ['What does memory_id=mem_42 mean?', 'Explain memory_remove(memory_id=mem_42).']) {
    const recalled = await hooks.before_prompt_build({ prompt: content, currentUserMessage: content, messages: [old] }, route);
    assert.match(recalled.prependContext, /Lin prefers concise answers/);
    await hooks.agent_end({ success: true, messages: [old,
      { role: 'user', content, timestamp: 1780000000001, idempotencyKey: content },
    ] }, route);
  }
  assert.deepEqual(requests.map(({ path }) => path), ['/v1/recall', '/v1/retain', '/v1/recall', '/v1/retain']);
  assert.ok(requests.filter(({ path }) => path === '/v1/retain').every(({ body }) => body.messages[0].learning_eligible === true));
});

test('local control recall suppression preserves owner admission and frozen capture eligibility', async () => {
  requests.length = 0;
  const { hooks } = await installedPlugin({ allowLocalMainAgent: true, allowedDirectConversations: [] });
  const content = 'That one, please.';
  const context = { agentId: 'main', runId: 'local-control', sessionKey: 'agent:main:main', sessionId: 'local-session',
    channel: 'webchat', chatId: 'webchat', trigger: 'user', toolAuthority: route.toolAuthority, hookInvocation: route.hookInvocation };
  const message = { role: 'user', content, timestamp: 1780000000000 };
  assert.equal(await hooks.before_prompt_build({ prompt: content, messages: [{ ...message, learning_eligible: false }] }, context), undefined);
  await hooks.before_agent_run({ senderIsOwner: true }, context);
  await hooks.agent_end({ success: true, messages: [message] }, context);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, '/v1/retain');
  assert.equal(requests[0].body.messages[0].learning_eligible, false);
  requests.length = 0;
  const denied = { ...context, runId: 'local-denied' };
  await hooks.before_prompt_build({ prompt: content, messages: [{ ...message, command: true }] }, denied);
  await hooks.before_agent_run({ senderIsOwner: false }, denied);
  await hooks.agent_end({ success: true, messages: [message] }, denied);
  assert.equal(requests.length, 0);
});

test('explicit search uses the fixed user and defaults to normal recall', async () => {
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
  assert.equal(requests[0].body.recall_level, 'normal');
  assert.equal(requests[0].body.query, 'Lin preference');
  assert.equal('include_profile' in requests[0].body, false);
});

test('explicit search rejects removed modes without dispatching recall', async () => {
  requests.length = 0;
  const { tool } = await installedPlugin();
  const search = tool.create({
    agentId: 'main', messageChannel: 'telegram', nativeChannelId: '42', requesterSenderId: '7',
    conversationReadOrigin: 'direct-operator', assertInvocationCurrent() {},
  });
  for (const recall_level of ['deep', 'mid']) {
    const result = await search.execute('removed-mode', { query: 'Lin preference', recall_level });
    assert.equal(result.isError, true);
  }
  assert.deepEqual(requests, []);
  assert.deepEqual(search.parameters.properties.recall_level.enum, ['fast', 'normal']);
});

test('memory_remove deletes exactly one memory or Insight ID through its typed route', async () => {
  requests.length = 0;
  const { removeTool } = await installedPlugin();
  assert.ok(removeTool);
  const remove = removeTool.create({
    agentId: 'main', messageChannel: 'telegram', nativeChannelId: '42', requesterSenderId: '7',
    conversationReadOrigin: 'direct-operator', assertInvocationCurrent() {},
  });
  assert.equal(remove.name, 'memory_remove');
  assert.equal(remove.parameters.additionalProperties, false);
  const memoryResult = await remove.execute('remove-memory', { memory_id: ' mem_42 ' });
  const insightResult = await remove.execute('remove-insight', { insight_id: 'ins_7' });
  assert.equal(memoryResult.isError, undefined);
  assert.match(memoryResult.content[0].text, /mem_42/);
  assert.equal(insightResult.isError, undefined);
  assert.match(insightResult.content[0].text, /ins_7/);
  assert.deepEqual(requests.map(({ method, path, body }) => ({ method, path, body })), [
    { method: 'DELETE', path: '/v1/users/u1/memories/mem_42', body: {} },
    { method: 'DELETE', path: '/v1/users/u1/insights/ins_7', body: {} },
  ]);
});

test('memory_remove rejects missing, duplicate, blank, and malformed IDs without a request', async () => {
  requests.length = 0;
  const { removeTool } = await installedPlugin();
  assert.ok(removeTool);
  const remove = removeTool.create({
    agentId: 'main', messageChannel: 'telegram', nativeChannelId: '42', requesterSenderId: '7',
    conversationReadOrigin: 'direct-operator', assertInvocationCurrent() {},
  });
  for (const params of [{}, { memory_id: 'mem_42', insight_id: 'ins_7' },
    { memory_id: '   ' }, { insight_id: '../ins_7' }, { memory_id: 42 },
    { memory_id: 'mem_42', insight_id: '' }, { memory_id: 'mem_42', user_id: 'other' }]) {
    const result = await remove.execute('invalid-remove', params);
    assert.equal(result.isError, true);
  }
  assert.equal(requests.length, 0);
});

test('memory_remove rejects non-202 responses and malformed or mismatched receipts', async () => {
  const { removeTool } = await installedPlugin();
  const remove = removeTool.create({
    agentId: 'main', messageChannel: 'telegram', nativeChannelId: '42', requesterSenderId: '7',
    conversationReadOrigin: 'direct-operator', assertInvocationCurrent() {},
  });
  const receipt = { target_kind: 'memory', memory_id: 'mem_42', status: 'excluded',
    policy_epoch: 8, operation_id: 'op_remove', cleanup_status: 'accepted' };
  try {
    for (const response of [{ status: 200, body: receipt }, { status: 404, body: {} },
      { status: 202, body: {} }, { status: 202, body: { ...receipt, memory_id: 'other' } },
      { status: 202, body: { ...receipt, target_kind: 'insight' } },
      { status: 202, body: { ...receipt, operation_id: '' } }]) {
      removalResponse = response;
      assert.equal((await remove.execute('remove-invalid-receipt', { memory_id: 'mem_42' })).isError, true);
    }
  } finally {
    removalResponse = undefined;
  }
});

test('memory_remove preserves approved conversation and current invocation requirements', async () => {
  requests.length = 0;
  const { removeTool } = await installedPlugin({ allowLocalMainAgent: true });
  const direct = { agentId: 'main', messageChannel: 'telegram', nativeChannelId: '42', requesterSenderId: '7',
    conversationReadOrigin: 'direct-operator', assertInvocationCurrent() {} };
  for (const context of [{ ...direct, agentId: 'other' }, { ...direct, requesterSenderId: 'other' },
    { ...direct, conversationReadOrigin: 'delegated' }, { ...direct, assertInvocationCurrent: undefined },
    { agentId: 'main', sessionKey: 'agent:main:main', messageChannel: 'webchat', senderIsOwner: false,
      assertInvocationCurrent() {} }]) {
    assert.equal(removeTool.create(context), null);
  }
  assert.ok(removeTool.create({ agentId: 'main', sessionKey: 'agent:main:main',
    messageChannel: 'webchat', senderIsOwner: true, assertInvocationCurrent() {} }));
  const revoked = removeTool.create({ ...direct, assertInvocationCurrent() { throw new Error('revoked'); } });
  assert.equal((await revoked.execute('revoked-remove', { memory_id: 'mem_42' })).isError, true);
  assert.equal(requests.length, 0);
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
