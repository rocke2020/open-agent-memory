import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { apply } from '../index.mjs';

test('retains schedule definitions and delivery receipts without job output', async () => {
  const previousHome = process.env.DSH_HOME;
  const testHome = await mkdtemp(join(tmpdir(), 'oam-dsh-test-'));
  process.env.DSH_HOME = testHome;
  const requests = [];
  let notify;
  const arrived = () => new Promise(resolve => { notify = resolve; });
  const server = createServer(async (req, res) => {
    let data = ''; for await (const chunk of req) data += chunk;
    requests.push(JSON.parse(data));
    res.writeHead(202, {'content-type':'application/json'});
    res.end(JSON.stringify({operation_id:'op',source_id:'src',revision:1,readiness:{accepted:true}}));
    notify?.();
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const hooks = {}; const effects = []; let scheduleContext;
    const job = {id:'job-1',sessionId:'s1',kind:'cron',title:'Daily story',prompt:'Write a story',expression:'30 20 * * *',timeZone:'Asia/Shanghai',scheduledAt:'2026-10-30T12:30:00.000Z',status:'active'};
    let catalog = [job];
    const ctx = {on(n,f){hooks[n]=f;},inject(names,f){if(names[0]==='schedule') scheduleContext=f;},effect(f){effects.push(f());},logger:{info(){},warn(){}}};
    apply(ctx,{baseUrl:`http://127.0.0.1:${server.address().port}`,userId:'u',apiKey:'key'});
    const first = arrived();
    scheduleContext({schedule:{async catalog(){return catalog;}},on(n,f){hooks[n]=f;},effect(fn){fn();}});
    await first;
    catalog = [{...job,scheduledAt:'2026-10-31T12:30:00.000Z',lastDelivery:{scheduledAt:'2026-10-30T12:30:00.000Z',deliveredAt:'2026-10-30T12:30:01.000Z',messageId:'m1'}}];
    const second = arrived();
    hooks['schedule/changed']();
    await second;
    hooks['session/event']({header:{id:'s1'}},{type:'assistant/message',data:{message:{role:'assistant',content:[{type:'text',text:'PRIVATE JOB OUTPUT'}]}}});
    catalog = [];
    const third = arrived();
    hooks['schedule/changed']();
    await third;
    await effects[0]();
    const text = requests.map(r=>r.messages[0].content).join('\n');
    assert.match(text,/Daily story/);
    assert.match(text,/30 20 \* \* \*/);
    assert.doesNotMatch(text,/Write a story/);
    assert.match(text,/delivered/i);
    assert.match(text,/removed from the schedule catalog/i);
    assert.doesNotMatch(text,/completed|success/i);
    assert.doesNotMatch(text,/PRIVATE JOB OUTPUT/);
    assert.equal(requests.length, 3);
  } finally {
    await new Promise(resolve => server.close(resolve));
    if (previousHome === undefined) delete process.env.DSH_HOME; else process.env.DSH_HOME = previousHome;
    await rm(testHome, {recursive:true,force:true});
  }
});

test('restarting reuses the definition body and idempotency key', async () => {
  const oldHome = process.env.DSH_HOME;
  const testHome = await mkdtemp(join(tmpdir(), 'oam-dsh-restart-'));
  process.env.DSH_HOME = testHome;
  const requests = [];
  const server = createServer(async (req,res) => {
    let body=''; for await (const chunk of req) body+=chunk;
    requests.push({body:JSON.parse(body),key:req.headers['idempotency-key']});
    res.writeHead(202,{'content-type':'application/json'});
    res.end(JSON.stringify({operation_id:'op',source_id:'src',revision:1,readiness:{accepted:true}}));
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  try {
    const job={id:'j',sessionId:'s',kind:'cron',title:'Daily job',prompt:'SECRET PROMPT',expression:'0 7 * * *',timeZone:'UTC',scheduledAt:'2026-10-30T07:00:00Z',status:'active',lastDelivery:{scheduledAt:'2026-10-29T07:00:00Z',deliveredAt:'2026-10-29T07:00:01Z',messageId:'m'}};
    for (let i=0; i<2; i++) {
      let dispose;
      const ctx={on(){},inject(names,fn){if(names[0]==='schedule') fn({schedule:{async catalog(){return [job];}},on(){},effect(fn){fn();}});},effect(fn){dispose=fn();},logger:{info(){},warn(){}}};
      apply(ctx,{baseUrl:`http://127.0.0.1:${server.address().port}`,userId:'u',apiKey:'key'});
      await dispose();
    }
    assert.equal(requests.length,4);
    assert.deepEqual(requests[0],requests[2]);
    assert.deepEqual(requests[1],requests[3]);
    job.title = 'Renamed job';
    let dispose;
    const ctx={on(){},inject(names,fn){if(names[0]==='schedule') fn({schedule:{async catalog(){return [job];}},on(){},effect(fn){fn();}});},effect(fn){dispose=fn();},logger:{info(){},warn(){}}};
    apply(ctx,{baseUrl:`http://127.0.0.1:${server.address().port}`,userId:'u',apiKey:'key'});
    await dispose();
    assert.deepEqual(requests[1],requests[5]);
    assert.doesNotMatch(JSON.stringify(requests),/SECRET PROMPT/);
  } finally {
    await new Promise(resolve=>server.close(resolve));
    if(oldHome===undefined) delete process.env.DSH_HOME; else process.env.DSH_HOME=oldHome;
    await rm(testHome,{recursive:true,force:true});
  }
});

test('schedule change listener belongs to the injected schedule scope', async () => {
  let mount, dispose;
  const outer=[]; const scoped=[];
  const ctx={on(name,fn){if(name==='schedule/changed') outer.push(fn);},inject(names,fn){if(names[0]==='schedule') mount=fn;},effect(fn){dispose=fn();},logger:{info(){},warn(){}}};
  apply(ctx,{baseUrl:'http://127.0.0.1:1',userId:'u',apiKey:'key'});
  const inner={schedule:{async catalog(){return [];}},on(name,fn){if(name==='schedule/changed') scoped.push(fn);},effect(fn){fn();}};
  mount(inner);
  assert.equal(outer.length,0);
  assert.equal(scoped.length,1);
  await dispose();
});

test('disposed schedule scope cannot publish a late catalog snapshot', async () => {
  const oldHome=process.env.DSH_HOME;
  const home=await mkdtemp(join(tmpdir(),'oam-dsh-scope-'));
  process.env.DSH_HOME=home;
  const requests=[];
  const server=createServer(async (req,res)=>{let body='';for await(const c of req) body+=c;requests.push(JSON.parse(body));res.writeHead(202,{'content-type':'application/json'});res.end(JSON.stringify({operation_id:'op',source_id:'src',revision:1,readiness:{accepted:true}}));});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  try {
    let mount,dispose,release,seenOld; let oldDispose=()=>{};
    const oldStarted=new Promise(resolve=>{seenOld=resolve;});
    const ctx={on(){},inject(names,fn){if(names[0]==='schedule') mount=fn;},effect(fn){dispose=fn();},logger:{info(){},warn(){}}};
    apply(ctx,{baseUrl:`http://127.0.0.1:${server.address().port}`,userId:'u',apiKey:'key'});
    const job={id:'j',kind:'every',title:'Old job',everySeconds:300,status:'active'};
    mount({schedule:{async catalog(){seenOld();return new Promise(resolve=>{release=resolve;});}},on(){},effect(fn){oldDispose=fn();}});
    await oldStarted;
    const stopping=oldDispose();
    assert.ok(stopping instanceof Promise);
    release([job]);
    await stopping;
    mount({schedule:{async catalog(){return [{...job,title:'New job'}];}},on(){},effect(fn){fn();}});
    await dispose();
    assert.equal(requests.length,1);
    assert.match(requests[0].messages[0].content,/New job/);
    assert.doesNotMatch(requests[0].messages[0].content,/Old job/);
  } finally {
    await new Promise(resolve=>server.close(resolve));
    if(oldHome===undefined) delete process.env.DSH_HOME; else process.env.DSH_HOME=oldHome;
    await rm(home,{recursive:true,force:true});
  }
});

test('recalls human input and retains it only after successful completion', async () => {
  const requests = [];
  const server = createServer(async (req, res) => {
    let data = ''; for await (const chunk of req) data += chunk;
    requests.push({ path: req.url, body: JSON.parse(data), key: req.headers['idempotency-key'] });
    res.setHeader('content-type', 'application/json');
    if (req.url === '/v1/recall') res.end(JSON.stringify({ context: '[source 1] Likes tea.', context_tokens: 8, items: [{}] }));
    else { res.statusCode = 202; res.end(JSON.stringify({operation_id:'op1', source_id:'src1', revision:1, readiness:{accepted:true}})); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const hooks = {}; const tools = []; const effects = [];
    const ctx = { on(name, fn) { hooks[name] = fn; }, inject(_, fn) { fn({ tools: { register(tool) { tools.push(tool); } } }); }, effect(fn) { effects.push(fn()); }, logger: { info() {}, warn() {} } };
    apply(ctx, { baseUrl:`http://127.0.0.1:${server.address().port}`, userId:'u1', apiKey:'test-key' });
    const session = { header: { id:'s1' } };
    const human = { id:'m1', role:'user', source:{kind:'user'}, content:[{type:'text',text:'I like tea.'}] };
    const decision = await hooks['agent/pre-step']({ agent:{session}, signal:new AbortController().signal }, async () => ({kind:'enter', messages:[human], marker: true}));
    assert.equal(decision.marker, true);
    assert.match(decision.messages[1].content[0].text, /Likes tea/);
    assert.equal(decision.messages[1].source.kind, 'plugin:oam-memory');
    assert.deepEqual(requests[0].body, {user_id:'u1',query:'I like tea.',recall_level:'fast',allow_partial:false});
    hooks['session/event'](session, {type:'turn/start',data:{turn:1}});
    hooks['session/event'](session, {type:'user/message',time:1000,seq:2,data:human});
    hooks['session/event'](session, {type:'turn/end',data:{turn:1,reason:{kind:'completed'}}});
    await effects[0]();
    assert.equal(requests[1].body.messages[0].content, 'I like tea.');
    assert.equal(requests[1].body.messages[0].event_time, '1970-01-01T00:00:01.000Z');
    assert.ok(requests[1].key);
    assert.equal(tools[0].name, 'memory_search');
  } finally { await new Promise(resolve => server.close(resolve)); }
});

test('excludes synthetic input, subagents and failed turns without contacting OAM', async () => {
  const hooks = {}; const registered = []; const effects = [];
  const ctx = {on(n,f){hooks[n]=f;},inject(_,f){f({tools:{register(t){registered.push(t);}}});},effect(f){effects.push(f());},logger:{info(){},warn(){}}};
  apply(ctx,{baseUrl:'http://127.0.0.1:1', userId:'u', apiKey:'key'});
  const child = {header:{id:'child',origin:'subagent'}};
  const main = {header:{id:'main'}};
  const synthetic = {id:'x',role:'user',source:{kind:'plugin:other'},content:[{type:'text',text:'synthetic'}]};
  const original = {kind:'enter',messages:[synthetic]};
  assert.equal(await hooks['agent/pre-step']({agent:{session:main},signal:new AbortController().signal},async()=>original),original);
  assert.equal(await hooks['agent/pre-step']({agent:{session:child},signal:new AbortController().signal},async()=>original),original);
  assert.match(await registered[0].execute({query:'test'},{agent:{session:child}}),/unavailable/);
  hooks['session/event'](main,{type:'turn/start',data:{turn:1}});
  hooks['session/event'](main,{type:'user/message',time:1000,data:{...synthetic,source:{kind:'user'}}});
  hooks['session/event'](main,{type:'turn/end',data:{reason:{kind:'error'}}});
  await effects[0]();
});

test('oversized recall is omitted and a failed backend does not block the turn', async () => {
  const server=createServer((req,res)=>{res.setHeader('content-type','application/json');res.end(JSON.stringify({context:'large',context_tokens:5,items:[]}));});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  try {
    const hooks={};const warnings=[];let dispose;
    const ctx={on(n,f){hooks[n]=f;},inject(){},effect(f){dispose=f();},logger:{warn(s){warnings.push(s);},info(){}}};
    apply(ctx,{baseUrl:`http://127.0.0.1:${server.address().port}`,userId:'u',apiKey:'key',maxContextTokens:4});
    const decision={kind:'enter',messages:[{role:'user',source:{kind:'user'},content:[{type:'text',text:'question'}]}]};
    assert.equal(await hooks['agent/pre-step']({agent:{session:{header:{id:'s'}}},signal:new AbortController().signal},async()=>decision),decision);
    assert.match(warnings[0],/exceeds host budget/);
    await dispose();
  } finally {await new Promise(resolve=>server.close(resolve));}
});

test('search failures reach the host as errors', async () => {
  let release; let seen;
  const arrived = new Promise(resolve => { seen = resolve; });
  const server=createServer(async (req,res)=>{
    if (req.url === '/v1/recall') { seen(); await new Promise(resolve=>{release=resolve;}); res.statusCode=503; res.end('{}'); }
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  try {
    const hooks={};const tools=[];let dispose;
    const ctx={on(n,f){hooks[n]=f;},inject(_,f){f({tools:{register(t){tools.push(t);}}});},effect(f){dispose=f();},logger:{warn(){},info(){}}};
    apply(ctx,{baseUrl:`http://127.0.0.1:${server.address().port}`,userId:'u',apiKey:'key'});
    const agent={session:{header:{id:'s'}}};
    const search=tools[0].execute({query:'query'},{agent});
    await arrived; release();
    await assert.rejects(search,/Memory search unavailable/);
    await dispose();
  } finally {await new Promise(resolve=>server.close(resolve));}
});

test('a late recall cannot inject after disposal', async () => {
  let release, seen; const arrived=new Promise(resolve=>{seen=resolve;});
  const server=createServer(async (_req,res)=>{seen();await new Promise(resolve=>{release=resolve;});res.setHeader('content-type','application/json');res.end(JSON.stringify({context:'late',context_tokens:1,items:[]}));});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  try {
    const hooks={};let dispose;const ctx={on(n,f){hooks[n]=f;},inject(){},effect(f){dispose=f();},logger:{warn(){},info(){}}};
    apply(ctx,{baseUrl:`http://127.0.0.1:${server.address().port}`,userId:'u',apiKey:'key'});
    const original={kind:'enter',messages:[{role:'user',source:{kind:'user'},content:[{type:'text',text:'hello'}]}]};
    const result=hooks['agent/pre-step']({agent:{session:{header:{id:'s'}}},signal:new AbortController().signal},async()=>original);
    await arrived;await dispose();release();assert.equal(await result,original);
  } finally {await new Promise(resolve=>server.close(resolve));}
});

test('tool declarations exclude subagents and tool results mark memory as untrusted', async () => {
  const server=createServer((_req,res)=>{res.setHeader('content-type','application/json');res.end(JSON.stringify({context:'[source] fact',context_tokens:2,items:[]}));});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  try {
    let tool, options, dispose;
    const ctx={on(){},inject(_,f){f({tools:{register(t,o){tool=t;options=o;}}});},effect(f){dispose=f();},logger:{warn(){},info(){}}};
    apply(ctx,{baseUrl:`http://127.0.0.1:${server.address().port}`,userId:'u',apiKey:'key'});
    const main={session:{header:{id:'s'}}};const child={session:{header:{id:'c',origin:'subagent'}}};
    assert.equal(options.when({agent:main}),true);assert.equal(options.when({agent:child}),false);
    assert.match(await tool.execute({query:'test'},{agent:main}),/<untrusted_oam_memory>/);
    await dispose();
  } finally {await new Promise(resolve=>server.close(resolve));}
});
