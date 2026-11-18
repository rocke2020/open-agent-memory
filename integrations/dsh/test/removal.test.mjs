import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { apply } from '../index.mjs';

const main = {session:{header:{id:'main'}}};
const child = {session:{header:{id:'child',origin:'subagent'}}};
const receipt = {target_kind:'memory',memory_id:'mem_42',status:'excluded',policy_epoch:8,operation_id:'op_remove',cleanup_status:'accepted',reused:false};

async function fixture(t, respond) {
  const requests = [];
  const server = createServer(async (req, res) => {
    let body = ''; for await (const chunk of req) body += chunk;
    requests.push({method:req.method,path:req.url,authorization:req.headers.authorization,body:body ? JSON.parse(body) : undefined});
    res.setHeader('content-type','application/json');
    respond(req, res);
  });
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  const tools = new Map(); const hooks = {}; let dispose;
  const ctx = {on(name,fn){hooks[name]=fn;},inject(names,fn){if(names[0]==='tools') fn({tools:{register(tool,options){tools.set(tool.name,{...tool,options});}}});},effect(fn){dispose=fn();},logger:{info(){},warn(){}}};
  apply(ctx,{baseUrl:`http://127.0.0.1:${server.address().port}`,userId:'owner/name',apiKey:'test-key'});
  t.after(async () => {await dispose(); await new Promise(resolve => server.close(resolve));});
  return {tools,hooks,requests,dispose};
}

test('memory_search exposes exact removal IDs beside their context references', async t => {
  const {tools} = await fixture(t,(_req,res) => res.end(JSON.stringify({
    context:'[1] fact memory\nTest fact.\n[2] passage memory\nOriginal.\n[3] insight memory\nTest insight.',
    context_tokens:24,
    items:[
      {unit_id:'mem_42',kind:'fact',origin:'memory',memory_id:'mem_42',insight_id:null,text:'Test fact.'},
      {unit_id:'passage_1',kind:'passage',origin:'memory',memory_id:null,insight_id:null,text:'Original.'},
      {unit_id:'ins_7',kind:'insight',origin:'memory',memory_id:null,insight_id:'ins_7',text:'Test insight.'},
    ],
  })));
  const result = await tools.get('memory_search').execute({query:'test'},{agent:main});
  assert.match(result,/\[1\] memory_id=mem_42/);
  assert.match(result,/\[3\] insight_id=ins_7/);
  assert.doesNotMatch(result,/memory_id=passage_1|insight_id=passage_1/);
});

test('memory_remove sends typed DELETE requests for the configured user and reports serving exclusion', async t => {
  const {tools,requests} = await fixture(t,(req,res) => {
    res.statusCode=202;
    res.end(JSON.stringify(req.url.endsWith('/ins_7') ? {...receipt,target_kind:'insight',memory_id:undefined,insight_id:'ins_7',cleanup_status:'succeeded'} : receipt));
  });
  const remove = tools.get('memory_remove');
  assert.ok(remove,'memory_remove must be registered');
  const memory = await remove.execute({memory_id:' mem_42 '},{agent:main});
  const insight = await remove.execute({insight_id:'ins_7'},{agent:main});
  assert.match(memory,/mem_42.*from serving/);
  assert.match(memory,/Cleanup accepted; operation op_remove/);
  assert.match(insight,/ins_7.*from serving/);
  assert.match(insight,/Cleanup succeeded/);
  assert.deepEqual(requests,[
    {method:'DELETE',path:'/v1/users/owner%2Fname/memories/mem_42',authorization:'Bearer test-key',body:undefined},
    {method:'DELETE',path:'/v1/users/owner%2Fname/insights/ins_7',authorization:'Bearer test-key',body:undefined},
  ]);
  assert.deepEqual(remove.output.render({},memory),[{type:'text',text:memory}]);
});

test('memory_remove rejects ambiguous IDs and identity overrides before contacting OAM', async t => {
  const {tools,requests} = await fixture(t,(_req,res) => {res.end('{}');});
  const remove = tools.get('memory_remove');
  assert.ok(remove);
  for (const args of [undefined,null,{},[],{memory_id:'mem_42',insight_id:'ins_7'},
    {memory_id:'mem_42',insight_id:''},{memory_id:' '},{insight_id:'../ins_7'},
    {memory_id:42},{memory_id:'mem_42',user_id:'other'},{memory_id:'a?b'}]) {
    await assert.rejects(async () => remove.execute(args,{agent:main}),/exactly one valid memory_id or insight_id/);
  }
  assert.deepEqual(requests,[]);
});

test('memory_remove excludes subagents, missing sessions and disposed plugins', async t => {
  const {tools,requests,dispose} = await fixture(t,(_req,res) => {res.end('{}');});
  const remove = tools.get('memory_remove');
  assert.ok(remove);
  assert.equal(remove.options.when({agent:main}),true);
  for (const agent of [child,undefined,{}]) {
    assert.equal(remove.options.when({agent}),false);
    await assert.rejects(remove.execute({memory_id:'mem_42'},{agent}),/unavailable/);
  }
  await dispose();
  assert.equal(remove.options.when({agent:main}),false);
  await assert.rejects(remove.execute({memory_id:'mem_42'},{agent:main}),/unavailable/);
  assert.deepEqual(requests,[]);
});

test('memory_remove rejects transport success without a matching exclusion receipt', async t => {
  let response;
  const {tools} = await fixture(t,(_req,res) => {res.statusCode=response.status;res.end(JSON.stringify(response.body));});
  const remove = tools.get('memory_remove');
  assert.ok(remove);
  for (response of [
    {status:200,body:receipt},{status:403,body:{detail:'PRIVATE KEY'}},{status:404,body:{}},
    ...[null,{}, {...receipt,status:'active'}, {...receipt,target_kind:'insight'},
      {...receipt,memory_id:'other'}, {...receipt,operation_id:''}, {...receipt,operation_id:5},
      {...receipt,policy_epoch:-1}, {...receipt,policy_epoch:1.5}, {...receipt,cleanup_status:''},
    ].map(body => ({status:202,body})),
  ]) {
    await assert.rejects(remove.execute({memory_id:'mem_42'},{agent:main}),error => {
      assert.match(error.message,/Memory removal unavailable/);
      assert.doesNotMatch(error.message,/PRIVATE KEY|test-key/);
      return true;
    });
  }
});

test('memory_remove honors cancellation before dispatch', async t => {
  const {tools,requests} = await fixture(t,(_req,res) => {res.end('{}');});
  const remove = tools.get('memory_remove');
  assert.ok(remove);
  const controller = new AbortController(); controller.abort();
  await assert.rejects(remove.execute({memory_id:'mem_42'},{agent:main,signal:controller.signal}),/Memory removal unavailable/);
  assert.deepEqual(requests,[]);
});

test('removal turns retain human evidence without learning it and later ordinary turns remain eligible', async t => {
  const {tools,hooks,requests,dispose} = await fixture(t,(req,res) => {
    if (req.method==='DELETE') {res.statusCode=202;res.end(JSON.stringify(receipt));}
    else {res.statusCode=202;res.end(JSON.stringify({operation_id:'op',source_id:'src',revision:1,readiness:{accepted:true}}));}
  });
  const remove = tools.get('memory_remove');
  assert.ok(remove);
  const human = {id:'m1',role:'user',source:{kind:'user'},content:[{type:'text',text:'Remove the memory that I like tea.'}]};
  const turn = (id,text) => {
    hooks['session/event'](main.session,{type:'turn/start'});
    hooks['session/event'](main.session,{type:'user/message',time:1000,data:{...human,id,content:[{type:'text',text}]}});
  };
  const end = () => hooks['session/event'](main.session,{type:'turn/end',data:{reason:{kind:'completed'}}});
  turn('m1',human.content[0].text);
  await remove.execute({memory_id:'mem_42'},{agent:main});
  end();
  turn('m2','That one, please.');
  hooks['session/event'](main.session,{type:'tool/call',data:{turn:2,step:1,callId:'bad-remove',name:'memory_remove',arguments:'{}'}});
  end();
  turn('m3','I prefer coffee.');
  hooks['session/event'](main.session,{type:'tool/call',data:{turn:3,step:1,callId:'search',name:'memory_search',arguments:'{}'}});
  end();
  await dispose();
  const messages = requests.filter(r => r.method==='POST').map(r => r.body.messages[0]);
  assert.deepEqual(messages,[
    {role:'user',content:'Remove the memory that I like tea.',event_time:'1970-01-01T00:00:01.000Z',learning_eligible:false},
    {role:'user',content:'That one, please.',event_time:'1970-01-01T00:00:01.000Z',learning_eligible:false},
    {role:'user',content:'I prefer coffee.',event_time:'1970-01-01T00:00:01.000Z',learning_eligible:true},
  ]);
});
