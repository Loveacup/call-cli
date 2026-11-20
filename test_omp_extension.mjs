import test from 'node:test';
import assert from 'node:assert/strict';
import * as ompExt from './omp-extension.mjs';
import register, {createAdapter} from './omp-extension.mjs';

function fixture(override) {
  const sent=[], timers=[], calls=[];
  let sid='host-a';
  const ctx={sessionManager:{getSessionId:()=>sid},agent:{id:'main'},isIdle:()=>false,
    setTimeout(fn){timers.push(fn);return fn;},clearTimer(fn){const i=timers.indexOf(fn);if(i>=0)timers.splice(i,1);}};
  const pi={sendMessage:(message,options)=>sent.push({message,options})};
  let packet={task:{id:'task-a',state:'running'},events:[],after:0};
  const request=async value=>{
    calls.push(value);
    if(override)return override(value);
    if(value.action==='bind')return {capability:'private-capability'};
    if(value.args.action==='start')return {task:{id:'task-a',state:'queued'}};
    return packet;
  };
  const adapter=createAdapter(pi,{request,pollMs:1});
  return {adapter,ctx,sent,timers,calls,setPacket:p=>packet=p,switch:()=>{sid='host-b';adapter.detach();}};
}

test('real bridge wrapper preserves safe model rejection without exposing provider errors', async () => {
  const {mkdtemp,writeFile,rm}=await import('node:fs/promises');
  const {tmpdir}=await import('node:os');
  const {join}=await import('node:path');
  const root=await mkdtemp(join(tmpdir(),'callcli-bridge-error-'));
  const previous=process.env.CALL_CLI_PYTHON;
  try {
    const executable=join(root,'fake-python');
    await writeFile(executable,`#!${process.execPath}\nlet data='';for await(const chunk of process.stdin)data+=chunk;const r=JSON.parse(data);if(r.action==='bind'){console.log(JSON.stringify({capability:'test-only'}));}else{console.log(JSON.stringify(r.args.model==='ocss'?{error:'ModelSelectionError',message:'Invalid AGY shorthand'}:{error:'RuntimeError',message:'PRIVATE_PROVIDER_BODY'}));process.exitCode=1;}\n`,{mode:0o700});
    process.env.CALL_CLI_PYTHON=executable;
    const f=fixture();
    const adapter=createAdapter({sendMessage:()=>{}},{db:join(root,'unused.db')});
    await assert.rejects(adapter.execute({action:'start',agent:'agy',model:'ocss',cwd:root,prompt:'never send'},f.ctx),/Invalid AGY shorthand/);
    await assert.rejects(adapter.execute({action:'start',agent:'agy',model:'exact',cwd:root,prompt:'never send'},f.ctx),error=>!error.message.includes('PRIVATE_PROVIDER_BODY') && error.message.includes('RuntimeError'));
    assert.deepEqual(f.timers,[]);
    adapter.detach();
  } finally {
    if(previous===undefined)delete process.env.CALL_CLI_PYTHON;else process.env.CALL_CLI_PYTHON=previous;
    await rm(root,{recursive:true,force:true});
  }
});

test('dialogue keeps native owner polling and requires explicit stable intent', async () => {
  const f=fixture();
  await f.adapter.execute({action:'start',agent:'agy',cwd:'/work',prompt:'owned'},f.ctx);
  f.setPacket({task:{id:'task-a',state:'running',turn:1},events:[]});
  await f.adapter.execute({action:'dialogue',task_id:'task-a',prompt:'revise',command_id:'d1',expected_turn:1},f.ctx);
  assert.equal(f.calls.at(-1).args.action,'dialogue');
  assert.equal(f.timers.length,1);
  for (const missing of ['expected_turn','command_id']) {
    const args={action:'dialogue',task_id:'task-a',prompt:'revise',command_id:'d1',expected_turn:1};
    delete args[missing];
    await assert.rejects(f.adapter.execute(args,f.ctx),/dialogue requires/);
  }
  await f.adapter.execute({action:'dialogue',task_id:'task-a',prompt:'revise 2',command_id:'d2',expected_turn:1,target_command_id:'d1'},f.ctx);
  assert.deepEqual(f.calls.at(-1).args,{action:'dialogue',task_id:'task-a',prompt:'revise 2',command_id:'d2',expected_turn:1,target_command_id:'d1'});
  await f.adapter.execute({action:'dialogue',task_id:'task-a',command_id:'w2',expected_turn:1,target_command_id:'d2',withdraw:true},f.ctx);
  assert.deepEqual(f.calls.at(-1).args,{action:'dialogue',task_id:'task-a',command_id:'w2',expected_turn:1,target_command_id:'d2',withdraw:true});
  await assert.rejects(f.adapter.execute({action:'status',task_id:'task-a',target_command_id:'d1'},f.ctx),/target_command_id requires dialogue/);
  await assert.rejects(f.adapter.execute({action:'status',task_id:'task-a',withdraw:true},f.ctx),/withdraw requires dialogue/);
  await assert.rejects(f.adapter.execute({action:'dialogue',task_id:'task-a',command_id:'w3',expected_turn:1,target_command_id:'d2',withdraw:'yes'},f.ctx),/withdraw requires dialogue/);
  assert.ok(!f.calls.some(c=>c.args?.action==='cancel'));
  f.adapter.detach();
});

test('registered OMP tool advertises dialogue without host identity fields', () => {
  const enums=[];
  const shapes=[];
  const chain=new Proxy({}, {get:()=>()=>chain});
  const z=new Proxy({}, {get:(_target,name)=>(...args)=>{
    if(name==='enum') enums.push(args[0]);
    if(name==='object') shapes.push(Object.keys(args[0]));
    return chain;
  }});
  let registered;
  register({zod:z,registerTool:tool=>{registered=tool;},on:()=>{}});
  assert.equal(registered.name,'call_cli_omp');
  assert.ok(enums.some(values=>values.includes('dialogue')));
  assert.ok(shapes.some(keys=>keys.includes('target_command_id') && keys.includes('withdraw')));
  assert.ok(enums.some(values=>values.includes('teamwork')));
  assert.ok(enums.some(values=>values.includes('skip-permissions') && values.includes('approve-all') && values.includes('approve-reads')));
});

test('model timeout is refused before binding and continuation clear remains available', async () => {
  const f=fixture();
  for(const timeout of [300,null]) {
    await assert.rejects(f.adapter.execute({action:'start',cwd:'/work',prompt:'inspect',timeout},f.ctx),/timeout/);
  }
  assert.deepEqual(f.calls,[]);
  await f.adapter.execute({action:'start',cwd:'/work',prompt:'inspect'},f.ctx);
  f.setPacket({task:{id:'task-a',state:'cancelling'},events:[]});
  await f.adapter.execute({action:'intervene',task_id:'task-a',command_id:'clear',prompt:'revise',clear_timeout:true},f.ctx);
  assert.equal(f.timers.length,1);
  f.adapter.detach();
});

test('stage and question wake correct session once and never expose capability',async()=>{
  const f=fixture();
  await f.adapter.execute({action:'start',agent:'agy',model:'gemini-4-argon',cwd:'/work',prompt:'inspect'},f.ctx);
  f.setPacket({task:{id:'task-a',state:'running'},events:[{seq:4,turn:1,token:'1:2',kind:'stage',text:'ready'},{seq:7,turn:1,token:'1:3',kind:'question',text:'Q1 format?'}],after:7});
  await f.timers.shift()();
  assert.equal(f.sent.length,1);
  assert.match(f.sent[0].message.content,/Q1 format/);
  assert.match(f.sent[0].message.content,/"seq":7/);
  assert.equal(f.sent[0].options.deliverAs,'followUp');
  assert.ok(!JSON.stringify(f.sent).includes('private-capability'));
  f.setPacket({task:{id:'task-a',state:'waiting_reply'},events:[],after:7});
  await f.timers.shift()();
  assert.equal(f.sent.length,1);
  assert.equal(f.calls.at(-1).args.after,7);
});

test('Dynamic permission wakes origin with exact script and decision retains polling', async () => {
  const f=fixture();
  await f.adapter.execute({action:'start',agent:'agy',agy_mode:'dynamic',model:'gemini-4-argon',cwd:'/work',prompt:'workflow'},f.ctx);
  f.setPacket({task:{id:'task-a',state:'running',turn:1},events:[{seq:1,kind:'permission',permission_id:'exact',script:'print(5)',script_sha256:'hash',turn:1}]});
  await f.timers.shift()();
  assert.equal(JSON.parse(f.sent[0].message.content).events[0].script,'print(5)');
  await f.adapter.execute({action:'permission',task_id:'task-a',permission_id:'exact',script_sha256:'hash',decision:'allow_once',expected_turn:1,command_id:'once'},f.ctx);
  assert.equal(f.calls.at(-1).args.action,'permission');
  assert.equal(f.timers.length,1);
  assert.ok(!f.calls.some(c=>['cancel','close'].includes(c.args?.action)));
  f.adapter.detach();
});

test('quiet and active review requests survive stage coalescing without automatic control', async () => {
  const f=fixture();
  await f.adapter.execute({action:'start',cwd:'/work',prompt:'inspect'},f.ctx);
  for (const [seq,notice_kind] of [[1,'quiet'],[3,'checkpoint']]) {
    f.setPacket({task:{id:'task-a',state:'running'},events:[
      {seq,kind:'stage',text:'Coordinator review requested',quiet_notice:true,notice_kind,elapsed:seq===1?600:1200,turn:1},
      {seq:seq+1,kind:'stage',text:'same status'}],after:seq+1});
    await f.timers.shift()();
  }
  const events=f.sent.flatMap(item=>JSON.parse(item.message.content).events);
  assert.deepEqual(events.filter(event=>event.quiet_notice).map(event=>event.notice_kind),['quiet','checkpoint']);
  assert.ok(!f.calls.some(call=>['cancel','close','intervene'].includes(call.args?.action)));
  assert.ok(f.sent.every(item=>item.options.triggerTurn===true));
});

test('session switch discards in-flight notification without canceling business',async()=>{
  const f=fixture();
  await f.adapter.execute({action:'start',agent:'agy',cwd:'/work',prompt:'inspect'},f.ctx);
  f.switch();
  assert.equal(f.timers.length,0);
  assert.ok(!f.calls.some(c=>c.args?.action==='cancel'));
  await assert.rejects(f.adapter.execute({action:'status',task_id:'task-a'},f.ctx),/not bound/);
});

test('injected host fields are rejected before bridge access',async()=>{
  const f=fixture();
  await assert.rejects(f.adapter.execute({action:'start',origin:{session_id:'forged'}},f.ctx),/host field/);
  assert.equal(f.calls.length,0);
});

test('switch during bridge read drops late old-session result',async()=>{
  let release;
  const sent=[],timers=[];
  let session='a';
  const ctx={sessionManager:{getSessionId:()=>session},agent:{id:'main'},
    setTimeout(fn){timers.push(fn);return fn;},clearTimer(){}};
  const adapter=createAdapter({sendMessage:m=>sent.push(m)},{request:async r=>{
    if(r.action==='bind')return {capability:'private'};
    if(r.args.action==='start')return {task:{id:'t',state:'queued'}};
    return new Promise(resolve=>{release=resolve;});
  }});
  await adapter.execute({action:'start',cwd:'/work',prompt:'x'},ctx);
  const pending=timers.shift()();
  session='b';adapter.detach();
  release({task:{id:'t',state:'completed'},events:[{seq:2,kind:'completed',text:'old'}]});
  await pending;
  assert.deepEqual(sent,[]);
});

test('reply resumes polling after consumed question without replaying its seq',async()=>{
  const f=fixture();
  await f.adapter.execute({action:'start',cwd:'/work',prompt:'inspect'},f.ctx);
  f.setPacket({task:{id:'task-a',state:'waiting_reply'},events:[{seq:8,turn:1,token:'1:2',kind:'question',text:'Q1 format?'}],after:8});
  await f.timers.shift()();
  f.setPacket({task:{id:'task-a',state:'queued',turn:2},events:[],after:8});
  await f.adapter.execute({action:'reply',task_id:'task-a',question_id:'1:2',prompt:'JSON'},f.ctx);
  f.setPacket({task:{id:'task-a',state:'completed'},events:[{seq:9,turn:2,token:'2:2',kind:'answer',text:'JSON RESULT'},{seq:10,turn:2,token:'2:settled',kind:'completed',text:'done'}],after:10});
  await f.timers.shift()();
  assert.equal(f.calls.at(-1).args.after,8);
  assert.equal(f.sent.length,2);
  assert.match(f.sent[1].message.content,/JSON RESULT/);
  assert.doesNotMatch(f.sent[1].message.content,/Q1 format/);
});

test('late waiting_reply poll cannot stop a successful reply round',async()=>{
  let release, waits=0;
  const f=fixture(async r=>{
    if(r.action==='bind')return {capability:'private-capability'};
    if(r.args.action==='start')return {task:{id:'task-a',state:'running',turn:1}};
    if(r.args.action==='reply')return {task:{id:'task-a',state:'queued',turn:2}};
    if(++waits===1)return new Promise(resolve=>{release=resolve;});
    return {task:{id:'task-a',state:'completed',turn:2},events:[
      {seq:9,turn:2,kind:'answer',text:'NEW ROUND RESULT'},
      {seq:10,turn:2,kind:'completed',text:'done'}]};
  });
  await f.adapter.execute({action:'start',cwd:'/work',prompt:'inspect'},f.ctx);
  const pending=f.timers.shift()();
  await f.adapter.execute({action:'reply',task_id:'task-a',question_id:'1:2',prompt:'JSON'},f.ctx);
  release({task:{id:'task-a',state:'waiting_reply',turn:1},events:[{seq:8,turn:1,kind:'waiting_reply',text:'old'}]});
  await pending;
  assert.equal(f.timers.length,1,'new reply round must remain scheduled');
  await f.timers.shift()();
  assert.equal(f.sent.length,1);
  assert.match(f.sent[0].message.content,/NEW ROUND RESULT/);
  assert.equal(f.timers.length,0);
});

test('concurrent first starts share binding and both deliver completion',async()=>{
  const releases=[];
  let starts=0;
  const f=fixture(async r=>{
    if(r.action==='bind')return new Promise(resolve=>releases.push(()=>resolve({capability:'private'})));
    if(r.args.action==='start')return {task:{id:`task-${++starts}`,state:'queued'}};
    return {task:{id:r.args.task_id,state:'completed'},events:[{seq:starts,kind:'completed',text:'done'}]};
  });
  const one=f.adapter.execute({action:'start',cwd:'/work',prompt:'one'},f.ctx);
  const two=f.adapter.execute({action:'start',cwd:'/work',prompt:'two'},f.ctx);
  releases[0]();
  const first=await one;
  if(releases[1])releases[1]();
  const second=await two;
  assert.notEqual(first.task.id,second.task.id);
  assert.equal(releases.length,1,'one bind per host session');
  await f.timers.shift()();
  assert.deepEqual(f.sent.map(s=>JSON.parse(s.message.content).task_id).sort(),['task-1','task-2']);
});

for(const transient of ['busy','error'])test(`${transient} read does not stop another tracked task`,async()=>{
  let starts=0, first=true;
  const f=fixture(async r=>{
    if(r.action==='bind')return {capability:'private'};
    if(r.args.action==='start')return {task:{id:`task-${++starts}`,state:'queued'}};
    if(r.args.task_id==='task-1' && first){
      first=false;
      if(transient==='error')throw Error('OMP bridge action failed (OperationalError)');
      return {task:null,events:[],after:0,busy:true};
    }
    return {task:{id:r.args.task_id,state:'completed'},events:[{seq:1,kind:'completed',text:'done'}]};
  });
  await f.adapter.execute({action:'start',cwd:'/work',prompt:'one'},f.ctx);
  await f.adapter.execute({action:'start',cwd:'/work',prompt:'two'},f.ctx);
  await f.timers.shift()();
  assert.ok(f.sent.some(s=>s.message.customType==='org.callcli.events' && JSON.parse(s.message.content).task_id==='task-2'));
  if(transient==='busy'){
    assert.equal(f.sent.length,1,'busy is not an error or model wakeup');
    assert.equal(f.timers.length,1);
    await f.timers.shift()();
    assert.match(f.sent.at(-1).message.content,/task-1/);
  }else{
    assert.equal(f.sent.filter(s=>s.message.customType==='org.callcli.error').length,1);
    assert.equal(f.timers.length,0,'failed task pauses without infinite retry');
  }
  assert.equal(f.calls.filter(c=>c.args?.action==='start').length,2);
  assert.ok(!f.calls.some(c=>c.args?.action==='cancel'));
});

test('stage bursts coalesce while repeated stage text stays quiet',async()=>{
  const f=fixture();
  await f.adapter.execute({action:'start',cwd:'/work',prompt:'inspect'},f.ctx);
  f.setPacket({task:{id:'task-a',state:'running'},events:[
    {seq:1,kind:'stage',text:'reading'}, {seq:2,kind:'stage',text:'ready to adjust'}]});
  await f.timers.shift()();
  assert.deepEqual(JSON.parse(f.sent[0].message.content).events.map(e=>e.text),['ready to adjust']);
  f.setPacket({task:{id:'task-a',state:'running'},events:[{seq:3,kind:'stage',text:'ready to adjust'}]});
  await f.timers.shift()();
  assert.equal(f.sent.length,1);
  f.setPacket({task:{id:'task-a',state:'waiting_reply'},events:[{seq:4,kind:'question',text:'Q1 proceed?'}]});
  await f.timers.shift()();
  assert.match(f.sent.at(-1).message.content,/Q1 proceed/);
});

test('pending intervention polls past cancelled source into target completion without status',async()=>{
  const packets=[
    {task:{id:'task-a',state:'cancelled',intervention:{status:'pending'}},events:[{seq:1,kind:'cancelled',text:'source cancelled'}]},
    {task:{id:'task-a',state:'queued',intervention:{status:'target_started'}},events:[]},
    {task:{id:'task-a',state:'completed',intervention:{status:'target_completed'}},events:[{seq:2,kind:'answer',text:'REVISED RESULT'},{seq:3,kind:'completed',text:'target done'}]},
  ];
  const f=fixture(async r=>{
    if(r.action==='bind')return {capability:'private'};
    if(r.args.action==='start')return {task:{id:'task-a',state:'running'}};
    if(r.args.action==='intervene')return {task:{id:'task-a',state:'cancelling',intervention:{status:'pending'}}};
    assert.equal(r.args.action,'wait','continuation must not rely on driver status');
    assert.ok(packets.length,'no wait after completion');
    return packets.shift();
  });
  await f.adapter.execute({action:'start',cwd:'/work',prompt:'inspect'},f.ctx);
  const accepted=await f.adapter.execute({action:'intervene',task_id:'task-a',command_id:'narrow-1',prompt:'README only'},f.ctx);
  assert.equal(accepted.task.state,'cancelling');
  await f.timers.shift()();
  assert.equal(f.timers.length,1,'pending intervention must keep polling after source cancellation');
  await f.timers.shift()();
  assert.equal(f.timers.length,1,'queued target must remain tracked');
  await f.timers.shift()();
  assert.equal(f.timers.length,0,'completed target must stop polling');
  assert.deepEqual(f.calls.filter(c=>c.args).map(c=>c.args.action),['start','intervene','wait','wait','wait']);
  assert.deepEqual(f.calls.filter(c=>c.args?.action==='wait').map(c=>c.args.after),[0,1,1]);
  assert.deepEqual(f.sent.map(s=>JSON.parse(s.message.content).events.map(e=>e.kind)),[['cancelled'],['answer','completed']]);
  assert.match(f.sent.at(-1).message.content,/REVISED RESULT/);
});

for(const state of ['completed','cancelled','closed','unknown'])test(`${state} without an authorized continuation stops polling`,async()=>{
  const f=fixture();
  await f.adapter.execute({action:'start',cwd:'/work',prompt:'inspect'},f.ctx);
  f.setPacket({task:{id:'task-a',state,...(state==='unknown'?{intervention:{status:'pending'}}:{})},events:[{seq:1,kind:state,text:state}]});
  await f.timers.shift()();
  assert.equal(f.timers.length,0,'terminal state must not automatically retry');
  assert.equal(f.calls.filter(c=>c.args?.action==='wait').length,1);
});

for(const stop of ['close','detach'])test(`${stop} clears a pending intervention timer`,async()=>{
  const f=fixture();
  await f.adapter.execute({action:'start',cwd:'/work',prompt:'inspect'},f.ctx);
  f.setPacket({task:{id:'task-a',state:'cancelled',intervention:{status:'pending'}},events:[{seq:1,kind:'cancelled',text:'source cancelled'}]});
  await f.timers.shift()();
  assert.equal(f.timers.length,1,'authorized continuation must be scheduled before stopping');
  if(stop==='close'){
    f.setPacket({task:{id:'task-a',state:'closed'},events:[]});
    await f.adapter.execute({action:'close',task_id:'task-a'},f.ctx);
  }else f.adapter.detach();
  assert.equal(f.timers.length,0,'stop must leave no timer');
  assert.equal(f.calls.filter(c=>c.args?.action==='wait').length,1,'stop must not advance the target');
});

test('AGY unknown wakes original initiator without a status request and preserves review intent', async () => {
  const f=fixture();
  await f.adapter.execute({action:'start',agent:'agy',cwd:'/work',prompt:'owned'},f.ctx);
  f.setPacket({task:{id:'task-a',agent:'agy',turn:1,state:'unknown'},events:[{
    seq:3,kind:'unknown',turn:1,token:'1:settled',text:'AGY reported a fatal execution error',
    attention_required:true,next_action:'Inspect exact task; no automatic replay'}]});
  await f.timers.shift()();
  assert.equal(f.sent.length,1);
  const notice=JSON.parse(f.sent[0].message.content);
  assert.equal(notice.task_id,'task-a');
  assert.equal(notice.events[0].attention_required,true);
  assert.deepEqual(f.sent[0].options,{deliverAs:'followUp',triggerTurn:true});
  assert.equal(f.calls.filter(c=>c.args?.action==='status').length,0);
  assert.equal(f.calls.at(-1).host.session_id,'host-a');
  f.setPacket({task:{id:'task-a',state:'unknown'},events:[]});
  await f.adapter.execute({action:'intervene',task_id:'task-a',command_id:'review-1',prompt:'Review failure; do not replay'},f.ctx);
  assert.equal(f.calls.at(-1).args.command_id,'review-1');
  f.adapter.detach();
});

test('AGY unknown snapshot without terminal event still raises one origin attention notice', async () => {
  const f=fixture();
  await f.adapter.execute({action:'start',agent:'agy',cwd:'/work',prompt:'owned'},f.ctx);
  f.setPacket({task:{id:'task-a',agent:'agy',turn:2,state:'unknown',settlement_note:'disconnected'},events:[]});
  await f.timers.shift()();
  assert.equal(f.sent.length,1);
  assert.match(f.sent[0].message.content,/disconnected/);
  assert.equal(f.sent[0].options.triggerTurn,true);
  assert.equal(f.timers.length,0);
  assert.ok(!f.calls.some(c=>['intervene','cancel'].includes(c.args?.action)));
});

test('OMP unknown snapshot wakes once per turn and preserves actionable HOLD', async () => {
  const f=fixture();
  await f.adapter.execute({action:'start',agent:'omp',cwd:'/work',prompt:'owned'},f.ctx);
  const task={id:'task-a',agent:'omp',turn:1,state:'unknown',settlement_note:'no final answer'};
  f.setPacket({task,events:[]});
  await f.timers.shift()();
  assert.equal(f.sent.length,1);
  let notice=JSON.parse(f.sent[0].message.content);
  assert.equal(notice.task_id,'task-a');
  assert.equal(notice.events[0].turn,1);
  assert.equal(notice.events[0].attention_required,true);
  assert.match(notice.events[0].next_action,/Do not replay/);
  assert.equal(notice.events[0].notification_scope,'originating-session');
  assert.deepEqual(f.sent[0].options,{deliverAs:'followUp',triggerTurn:true});
  assert.equal(f.calls.at(-1).host.session_id,'host-a');
  await f.adapter.execute({action:'status',task_id:'task-a'},f.ctx);
  assert.equal(f.timers.length,0,'status must not reactivate paused polling');
  f.setPacket({task,events:[{seq:7,kind:'unknown',turn:1,text:'no final answer',attention_required:true}]});
  await f.adapter.execute({action:'dialogue',task_id:'task-a',command_id:'rearm-1',expected_turn:1,prompt:'follow-up'},f.ctx);
  await f.timers.shift()();
  assert.equal(f.sent.length,1,'late terminal event must not repeat snapshot attention');
  await f.adapter.execute({action:'status',task_id:'task-a'},f.ctx);
  assert.equal(f.timers.length,0);
  f.setPacket({task:{...task,turn:2},events:[]});
  await f.adapter.execute({action:'dialogue',task_id:'task-a',command_id:'rearm-2',expected_turn:2,prompt:'follow-up-2'},f.ctx);
  await f.timers.shift()();
  assert.equal(f.sent.length,2,'a different execution turn has a new obligation');
  assert.equal(JSON.parse(f.sent[1].message.content).events[0].turn,2);
  assert.ok(!f.calls.some(c=>['intervene','cancel','close','reply'].includes(c.args?.action)));
});

test('OMP terminal attention is deduplicated without hiding legacy unknown events', async () => {
  const f=fixture();
  await f.adapter.execute({action:'start',agent:'omp',cwd:'/work',prompt:'owned'},f.ctx);
  const task={id:'task-a',agent:'omp',turn:1,state:'unknown'};
  f.setPacket({task,events:[{seq:1,kind:'unknown',turn:1,attention_required:true,text:'inspect exact task'}]});
  await f.timers.shift()();
  assert.equal(f.sent.length,1);
  await f.adapter.execute({action:'status',task_id:'task-a'},f.ctx);
  assert.equal(f.timers.length,0);
  f.setPacket({task,events:[{seq:2,kind:'unknown',turn:1,attention_required:true,text:'same obligation'}]});
  await f.adapter.execute({action:'dialogue',task_id:'task-a',command_id:'rearm-dedupe',expected_turn:1,prompt:'follow-up'},f.ctx);
  await f.timers.shift()();
  assert.equal(f.sent.length,1);
  const legacy=fixture();
  await legacy.adapter.execute({action:'start',cwd:'/work',prompt:'owned'},legacy.ctx);
  legacy.setPacket({task:{id:'task-a',state:'unknown'},events:[{seq:1,kind:'unknown',text:'legacy unknown'}]});
  await legacy.timers.shift()();
  assert.equal(legacy.sent.length,1);
  assert.match(legacy.sent[0].message.content,/legacy unknown/);
});

test('late prior-turn unknown cannot block current-turn snapshot attention', async () => {
  const f=fixture();
  await f.adapter.execute({action:'start',agent:'omp',cwd:'/work',prompt:'owned'},f.ctx);
  const task={id:'task-a',agent:'omp',turn:1,state:'unknown'};
  f.setPacket({task,events:[]});
  await f.timers.shift()();
  f.setPacket({task:{...task,turn:2},events:[
    {seq:1,kind:'unknown',turn:1,text:'late old unknown'},
    {seq:2,kind:'stage',turn:2,text:'progress'},
    {seq:3,kind:'question',turn:2,text:'question'},
    {seq:4,kind:'answer',turn:2,text:'answer'},
    {seq:5,kind:'skill',turn:2,text:'skill'}]});
  await f.adapter.execute({action:'status',task_id:'task-a'},f.ctx);
  assert.equal(f.timers.length,0);
  await f.adapter.execute({action:'dialogue',task_id:'task-a',command_id:'rearm-late',expected_turn:2,prompt:'follow-up'},f.ctx);
  await f.timers.shift()();
  assert.equal(f.sent.length,2);
  const events=JSON.parse(f.sent[1].message.content).events;
  assert.deepEqual(events.map(e=>e.kind),['stage','question','answer','unknown']);
  assert.equal(events.at(-1).turn,2);
  assert.equal(events.at(-1).source,'task_snapshot');
  assert.equal(f.timers.length,0);
});

test('unknown event with running snapshot is not repeated by next snapshot', async () => {
  const f=fixture();
  await f.adapter.execute({action:'start',agent:'omp',cwd:'/work',prompt:'owned'},f.ctx);
  const task={id:'task-a',agent:'omp',turn:1,state:'running'};
  f.setPacket({task,events:[{seq:1,kind:'unknown',turn:1,text:'settled between reads'}]});
  await f.timers.shift()();
  f.setPacket({task:{...task,state:'unknown'},events:[]});
  await f.timers.shift()();
  assert.equal(f.sent.length,1);
  assert.equal(f.timers.length,0);
});

test('legacy unknown without turn uses current task turn without duplicate fallback', async () => {
  const f=fixture();
  await f.adapter.execute({action:'start',agent:'omp',cwd:'/work',prompt:'owned'},f.ctx);
  const task={id:'task-a',agent:'omp',turn:2,state:'unknown'};
  f.setPacket({task,events:[{seq:1,kind:'unknown',text:'legacy terminal'},
    {seq:2,kind:'question',text:'keep this question'}]});
  await f.timers.shift()();
  assert.deepEqual(JSON.parse(f.sent[0].message.content).events.map(e=>e.kind),['unknown','question']);
  await f.adapter.execute({action:'status',task_id:'task-a'},f.ctx);
  assert.equal(f.timers.length,0);
  f.setPacket({task,events:[]});
  await f.adapter.execute({action:'dialogue',task_id:'task-a',command_id:'rearm-legacy',expected_turn:2,prompt:'follow-up'},f.ctx);
  await f.timers.shift()();
  assert.equal(f.sent.length,1);
  assert.equal(f.timers.length,0);
});

for (const previouslyNotified of [true,false]) test(`historical turn attention ${previouslyNotified ? 'does not repeat' : 'is delivered when first late'}`, async () => {
  const f=fixture();
  await f.adapter.execute({action:'start',agent:'omp',cwd:'/work',prompt:'owned'},f.ctx);
  const task={id:'task-a',agent:'omp',state:'unknown'};
  if (previouslyNotified) {
    f.setPacket({task:{...task,turn:1},events:[]});
    await f.timers.shift()();
    await f.adapter.execute({action:'status',task_id:'task-a'},f.ctx);
    assert.equal(f.timers.length,0);
    await f.adapter.execute({action:'dialogue',task_id:'task-a',command_id:'rearm-hist-1',expected_turn:1,prompt:'follow-up'},f.ctx);
  }
  f.setPacket({task:{...task,turn:2},events:[]});
  await f.timers.shift()();
  await f.adapter.execute({action:'status',task_id:'task-a'},f.ctx);
  assert.equal(f.timers.length,0);
  f.setPacket({task:{...task,turn:3,state:'running'},events:[
    {seq:1,kind:'unknown',turn:1,text:'late turn one'},
    {seq:2,kind:'stage',turn:3,text:'progress'},
    {seq:3,kind:'question',turn:3,text:'question'},
    {seq:4,kind:'answer',turn:3,text:'answer'},
    {seq:5,kind:'skill',turn:3,text:'skill'}]});
  await f.adapter.execute({action:'dialogue',task_id:'task-a',command_id:'rearm-hist-2',expected_turn:3,prompt:'follow-up'},f.ctx);
  await f.timers.shift()();
  const delivered=f.sent.flatMap(s=>JSON.parse(s.message.content).events);
  assert.deepEqual(delivered.filter(e=>e.kind==='unknown').map(e=>e.turn),previouslyNotified ? [1,2] : [2,1]);
  assert.deepEqual(JSON.parse(f.sent.at(-1).message.content).events.filter(e=>e.kind!=='unknown').map(e=>e.kind),
    ['stage','question','answer']);
  f.setPacket({task:{...task,turn:3,state:'running'},events:[{seq:6,kind:'unknown',turn:1,text:'repeat late turn one'}]});
  assert.equal(f.calls.at(-1).args.after,0);
  const count=f.sent.length;
  await f.timers.shift()();
  assert.equal(f.calls.at(-1).args.after,5,'cursor must advance past suppressed routine skill seq 5');
  assert.equal(f.sent.length,count,'a delivered old turn is now remembered too');
  f.adapter.detach();
});

test('unattributed legacy unknown events stay visible without repeated snapshot attention', async () => {
  const f=fixture();
  await f.adapter.execute({action:'start',agent:'omp',cwd:'/work',prompt:'owned'},f.ctx);
  const task={id:'task-a',agent:'omp',state:'unknown'};
  f.setPacket({task,events:[]});
  await f.timers.shift()();
  await f.adapter.execute({action:'status',task_id:'task-a'},f.ctx);
  assert.equal(f.timers.length,0);
  await f.adapter.execute({action:'dialogue',task_id:'task-a',command_id:'rearm-unattr-1',expected_turn:1,prompt:'follow-up'},f.ctx);
  await f.timers.shift()();
  assert.equal(f.sent.length,1,'a no-turn snapshot still has a remembered fallback obligation');
  await f.adapter.execute({action:'status',task_id:'task-a'},f.ctx);
  assert.equal(f.timers.length,0);
  f.setPacket({task,events:[{seq:1,kind:'unknown',text:'unattributed legacy terminal'}]});
  await f.adapter.execute({action:'dialogue',task_id:'task-a',command_id:'rearm-unattr-2',expected_turn:1,prompt:'follow-up'},f.ctx);
  await f.timers.shift()();
  assert.equal(f.sent.length,2,'unknown-turn events remain cursor-visible');
  await f.adapter.execute({action:'status',task_id:'task-a'},f.ctx);
  assert.equal(f.timers.length,0);
  f.setPacket({task,events:[]});
  await f.adapter.execute({action:'dialogue',task_id:'task-a',command_id:'rearm-unattr-3',expected_turn:1,prompt:'follow-up'},f.ctx);
  await f.timers.shift()();
  assert.equal(f.sent.length,2);
});

test('current host can explicitly release an old unknown task without a start or old identity', async () => {
  const f=fixture();
  f.setPacket({task:{id:'old-task',state:'unknown',resource_state:'released',close_receipt:{operation:'explicit-abandonment',risk:'may still be active'}}});
  await f.adapter.execute({action:'release_only',task_id:'old-task',expected_turn:1,abandon:true},f.ctx);
  assert.equal(f.calls[0].action,'bind');
  assert.equal(f.calls[1].host.session_id,'host-a');
  assert.equal(f.calls[1].args.abandon,true);
  assert.equal(f.timers.length,0);
  f.adapter.detach();
});

test('diagnostic events forward in follow-ups without stalling cursor', async () => {
  const f=fixture();
  await f.adapter.execute({action:'start',agent:'codex',cwd:'/work',prompt:'inspect'},f.ctx);
  f.setPacket({task:{id:'task-a',state:'running'},events:[{seq:5,turn:1,kind:'diagnostic',text:'ACPX runtime diagnostic detail'}],after:5});
  await f.timers.shift()();
  assert.equal(f.sent.length,1);
  assert.equal(f.sent[0].options.deliverAs,'followUp');
  const payload=JSON.parse(f.sent[0].message.content);
  assert.equal(payload.events.length,1);
  assert.equal(payload.events[0].kind,'diagnostic');
  assert.match(payload.events[0].text,/ACPX runtime diagnostic detail/);
  f.setPacket({task:{id:'task-a',state:'running'},events:[],after:5});
  await f.timers.shift()();
  assert.equal(f.sent.length,1);
  assert.equal(f.calls.at(-1).args.after,5);
  f.adapter.detach();
});

test('start forwards host_context.session_name safely and validates optional group', async () => {
  const fields=[];
  const chain=new Proxy({}, {get:()=>()=>chain});
  const z=new Proxy({}, {get:(_target,name)=>(...args)=>{
    if(name==='object') fields.push(...Object.keys(args[0]));
    return chain;
  }});
  let registered;
  register({zod:z,registerTool:tool=>{registered=tool;},on:()=>{}});
  assert.ok(fields.includes('group'));
  assert.equal(registered.name,'call_cli_omp');

  const calls=[];
  const f=fixture(value=>{
    calls.push(value);
    if(value.action==='bind') return {capability:'cap'};
    if(value.args.action==='start') return {task:{id:'task-g',state:'queued'}};
    return {tasks:[{id:'task-g',group:'alpha'}]};
  });
  await f.adapter.execute({action:'start',cwd:'/work',prompt:'fallback-name'},f.ctx);
  assert.deepEqual(calls.at(-1).host,{session_id:'host-a',agent_id:'main'});
  assert.deepEqual(calls.at(-1).host_context,{session_name:''});
  assert.ok(!Object.hasOwn(calls.at(-1).args,'host_context'));

  f.ctx.sessionManager.getSessionName=()=>'Sprint Board';
  await f.adapter.execute({action:'start',cwd:'/work',prompt:'grouped',group:'alpha'},f.ctx);
  assert.deepEqual(calls.at(-1).host,{session_id:'host-a',agent_id:'main'});
  assert.deepEqual(calls.at(-1).host_context,{session_name:'Sprint Board'});
  assert.equal(calls.at(-1).args.group,'alpha');

  await f.adapter.execute({action:'list',group:'alpha'},f.ctx);
  assert.equal(calls.at(-1).args.group,'alpha');
  assert.ok(!Object.hasOwn(calls.at(-1),'host_context'));

  const beforeRejects=calls.length;
  await assert.rejects(f.adapter.execute({action:'status',task_id:'task-g',group:'alpha'},f.ctx),/group requires start or list/);
  for(const bad of ['', '   ', null, 123]) {
    await assert.rejects(f.adapter.execute({action:'start',cwd:'/work',prompt:'bad',group:bad},f.ctx),/group must be a non-empty string/);
  }
  assert.equal(calls.length,beforeRejects);
  f.adapter.detach();
});

test('real omp_bridge roundtrip preserves group, origin_context, runtime_links, and secrets without launching runner', async () => {
  const {mkdtemp,writeFile,rm}=await import('node:fs/promises');
  const {tmpdir}=await import('node:os');
  const {join,dirname}=await import('node:path');
  const {fileURLToPath}=await import('node:url');
  const root=await mkdtemp(join(tmpdir(),'callcli-omp-real-bridge-'));
  const previous=process.env.CALL_CLI_PYTHON;
  const implDir=dirname(fileURLToPath(import.meta.url));
  try {
    const wrapper=join(root,'no-launch-python.sh');
    await writeFile(
      wrapper,
      `#!/bin/sh\nPYTHONPATH=${JSON.stringify(implDir)} exec python3 -B -c 'import sys,collab,omp_bridge; collab.Collaboration._launch=lambda self,t:None; sys.argv[0]=sys.argv[1]; raise SystemExit(omp_bridge.main())' "$2"\n`,
      {mode:0o700},
    );
    process.env.CALL_CLI_PYTHON=wrapper;
    const f=fixture();
    f.ctx.sessionManager.getSessionName=()=>'Cross Process Room';
    const adapter=createAdapter({sendMessage:()=>{}},{db:join(root,'tasks.db'),pollMs:60000});
    const started=await adapter.execute({action:'start',cwd:root,prompt:'cross process',group:'ops'},f.ctx);
    assert.equal(started.task.group,'ops');
    assert.equal(started.task.origin_context.surface,'omp-extension');
    assert.equal(started.task.origin_context.display_name,'Cross Process Room');
    assert.equal(started.task.runtime_links.task_id,started.task.id);
    assert.equal(started.task.runtime_links.session,started.task.session);
    assert.ok(!JSON.stringify(started).includes('host-a'));

    const listed=await adapter.execute({action:'list',group:'ops'},f.ctx);
    assert.deepEqual(listed.tasks.map(t=>t.id),[started.task.id]);
    assert.equal(listed.tasks[0].runtime_links.task_id,started.task.id);
    const otherListed=await adapter.execute({action:'list',group:'other'},f.ctx);
    assert.deepEqual(otherListed.tasks,[]);

    f.ctx.sessionManager.getSessionName=()=>'\x1b[32mStyled\x1b[0m Cross Room';
    const startedStyled=await adapter.execute({action:'start',cwd:root,prompt:'styled cross process',group:'styled-ops'},f.ctx);
    assert.equal(startedStyled.task.origin_context.session_name,'Styled Cross Room');
    assert.equal(startedStyled.task.origin_context.display_name,'Styled Cross Room');

    f.ctx.sessionManager.getSessionName=()=>'Leak host-\x1b[0ma';
    const startedSplit=await adapter.execute({action:'start',cwd:root,prompt:'split secret cross process',group:'split-ops'},f.ctx);
    assert.equal(startedSplit.task.origin_context.session_name,'unknown');
    assert.equal(startedSplit.task.origin_context.display_name,'unknown');
    assert.ok(!JSON.stringify(startedSplit).includes('host-a'));

    const listedSplit=await adapter.execute({action:'list',group:'split-ops'},f.ctx);
    assert.deepEqual(listedSplit.tasks.map(t=>t.id),[startedSplit.task.id]);
    assert.equal(listedSplit.tasks[0].origin_context.session_name,'unknown');
    assert.equal(listedSplit.tasks[0].origin_context.display_name,'unknown');
    assert.ok(!JSON.stringify(listedSplit).includes('host-a'));

    adapter.detach();
  } finally {
    if(previous===undefined)delete process.env.CALL_CLI_PYTHON;else process.env.CALL_CLI_PYTHON=previous;
    await rm(root,{recursive:true,force:true});
  }
});

test('running stream interruption diagnostic immediately triggers followUp without flushing buffered answers', async () => {
  const f = fixture();
  await f.adapter.execute({action: 'start', agent: 'agy', cwd: '/work', prompt: 'owned'}, f.ctx);
  f.setPacket({
    task: {id: 'task-a', agent: 'agy', state: 'running', turn: 1},
    events: [
      {seq: 1, turn: 1, kind: 'answer', text: 'The stream was interrupted. Please continue the task you were working on.'},
      {seq: 2, turn: 1, kind: 'diagnostic', diagnostic_code: 'agy_stream_interrupted', attention_required: true, notification_scope: 'originating-session', text: 'AGY reported stream interruption; native owner remains attached.'},
    ],
    after: 2,
  });
  await f.timers.shift()();
  assert.equal(f.sent.length, 1);
  assert.equal(f.sent[0].options.deliverAs, 'followUp');
  assert.equal(f.sent[0].options.triggerTurn, true);
  const delivered = JSON.parse(f.sent[0].message.content);
  assert.equal(delivered.task_id, 'task-a');
  assert.deepEqual(delivered.events.map(e => e.kind), ['diagnostic']);
  assert.equal(delivered.events[0].attention_required, true);
  assert.equal(delivered.events[0].diagnostic_code, 'agy_stream_interrupted');
  // Buffered answer flushes only when terminal event arrives
  f.setPacket({
    task: {id: 'task-a', agent: 'agy', state: 'completed', turn: 1},
    events: [
      {seq: 3, turn: 1, kind: 'answer', text: 'Resumed deliverable'},
      {seq: 4, turn: 1, kind: 'completed', text: 'Prompt exited 0'},
    ],
    after: 4,
  });
  await f.timers.shift()();
  assert.equal(f.sent.length, 2);
  const terminalBatch = JSON.parse(f.sent[1].message.content);
  assert.deepEqual(terminalBatch.events.map(e => e.seq), [1, 3, 4]);
  f.adapter.detach();
});

test('registered OMP tool Zod schema advertises question_ids while keeping question_id scalar string', () => {
  const objectShapes = [];
  const makeNode = (kind, args = []) => {
    const ops = [{kind, args}];
    const node = new Proxy({ops}, {
      get(target, prop) {
        if (prop === 'ops') return target.ops;
        return (...methodArgs) => {
          target.ops.push({kind: prop, args: methodArgs});
          return node;
        };
      },
    });
    return node;
  };
  const z = new Proxy({}, {
    get(_target, name) {
      return (...args) => {
        if (name === 'object') objectShapes.push(args[0]);
        return makeNode(name, args);
      };
    },
  });
  let registered;
  register({zod: z, registerTool: tool => {registered = tool;}, on: () => {}});
  assert.equal(registered.name, 'call_cli_omp');
  const shape = objectShapes.find(s => Object.hasOwn(s, 'action'));
  assert.ok(shape);
  assert.ok(Object.hasOwn(shape, 'question_id'));
  assert.ok(Object.hasOwn(shape, 'question_ids'));
  assert.deepEqual(shape.question_id.ops.map(op => op.kind), ['string', 'optional']);
  assert.equal(shape.question_ids.ops[0].kind, 'array');
  const elemNode = shape.question_ids.ops[0].args[0];
  assert.deepEqual(elemNode.ops.map(op => [op.kind, op.args]), [['string', []], ['min', [1]]]);
  assert.ok(shape.question_ids.ops.some(op => op.kind === 'min' && op.args[0] === 1));
  assert.ok(shape.question_ids.ops.some(op => op.kind === 'optional'));
  assert.ok(shape.question_ids.ops.some(op => op.kind === 'describe'));
});

test('createAdapter.execute forwards question_ids on dialogue and reply while keeping polling scheduled', async () => {
  const f = fixture();
  await f.adapter.execute({action: 'start', agent: 'agy', cwd: '/work', prompt: 'owned'}, f.ctx);
  f.setPacket({task: {id: 'task-a', state: 'running', turn: 1}, events: []});
  await f.adapter.execute({
    action: 'dialogue',
    task_id: 'task-a',
    prompt: 'multi answer',
    command_id: 'd1',
    expected_turn: 1,
    question_ids: [' 1:raw ', '1:2'],
  }, f.ctx);
  assert.deepEqual(f.calls.at(-1).args, {
    action: 'dialogue',
    task_id: 'task-a',
    prompt: 'multi answer',
    command_id: 'd1',
    expected_turn: 1,
    question_ids: [' 1:raw ', '1:2'],
  });
  assert.equal(f.timers.length, 1);

  f.setPacket({task: {id: 'task-a', state: 'waiting_reply', turn: 1}, events: []});
  await f.adapter.execute({
    action: 'reply',
    task_id: 'task-a',
    question_ids: [' 1:raw ', '1:2'],
    prompt: 'shared',
    expected_turn: 1,
    command_id: 'r1',
  }, f.ctx);
  assert.deepEqual(f.calls.at(-1).args, {
    action: 'reply',
    task_id: 'task-a',
    question_ids: [' 1:raw ', '1:2'],
    prompt: 'shared',
    expected_turn: 1,
    command_id: 'r1',
  });
  assert.equal(f.timers.length, 1);

  f.setPacket({task: {id: 'task-a', state: 'queued', turn: 2}, events: []});
  await f.adapter.execute({
    action: 'reply',
    task_id: 'task-a',
    question_ids: ['1:3'],
    prompt: 'last',
    expected_turn: 1,
    command_id: 'r2',
  }, f.ctx);
  assert.deepEqual(f.calls.at(-1).args.question_ids, ['1:3']);
  assert.equal(f.timers.length, 1);
  f.adapter.detach();
});

test('createAdapter.execute rejects invalid question_ids and question_id combinations before mutating state', async () => {
  const f = fixture();
  for (const preBindInvalid of [
    {action: 'list', question_ids: ['1:2']},
    {action: 'list', question_ids: null},
    {action: 'start', cwd: '/work', prompt: 'x', question_ids: ['1:2']},
  ]) {
    await assert.rejects(f.adapter.execute(preBindInvalid, f.ctx));
  }
  assert.equal(f.calls.length, 0);
  assert.equal(f.timers.length, 0);

  await f.adapter.execute({action: 'start', agent: 'agy', cwd: '/work', prompt: 'owned'}, f.ctx);
  const callsAfterStart = f.calls.length;
  const timersAfterStart = f.timers.length;

  const rejectCases = [
    {action: 'status', task_id: 'task-a', question_ids: ['1:2']},
    {action: 'status', task_id: 'task-a', question_ids: null},
    {action: 'intervene', task_id: 'task-a', command_id: 'i1', prompt: 'rev', question_ids: ['1:2']},
    {action: 'dialogue', task_id: 'task-a', command_id: 'd1', expected_turn: 1, prompt: 'p', question_id: '1:1', question_ids: ['1:2']},
    {action: 'dialogue', task_id: 'task-a', command_id: 'd1', expected_turn: 1, prompt: 'p', question_id: '', question_ids: ['1:2']},
    {action: 'reply', task_id: 'task-a', prompt: 'p', question_id: '1:1', question_ids: ['1:2']},
    {action: 'reply', task_id: 'task-a', prompt: 'p', question_id: '', question_ids: ['1:2']},
    {action: 'dialogue', task_id: 'task-a', command_id: 'd1', expected_turn: 1, prompt: 'p', question_ids: []},
    {action: 'reply', task_id: 'task-a', prompt: 'p', question_ids: []},
    {action: 'dialogue', task_id: 'task-a', command_id: 'd1', expected_turn: 1, prompt: 'p', question_ids: null},
    {action: 'reply', task_id: 'task-a', prompt: 'p', question_ids: null},
    {action: 'dialogue', task_id: 'task-a', command_id: 'd1', expected_turn: 1, prompt: 'p', question_ids: '1:2,1:3'},
    {action: 'reply', task_id: 'task-a', prompt: 'p', question_ids: '1:2,1:3'},
    {action: 'dialogue', task_id: 'task-a', command_id: 'd1', expected_turn: 1, prompt: 'p', question_ids: ['']},
    {action: 'reply', task_id: 'task-a', prompt: 'p', question_ids: ['']},
    {action: 'dialogue', task_id: 'task-a', command_id: 'd1', expected_turn: 1, prompt: 'p', question_ids: [1]},
    {action: 'reply', task_id: 'task-a', prompt: 'p', question_ids: [1]},
    {action: 'dialogue', task_id: 'task-a', command_id: 'd1', expected_turn: 1, prompt: 'p', question_ids: ['1:2', '1:2']},
    {action: 'reply', task_id: 'task-a', prompt: 'p', question_ids: ['1:2', '1:2']},
    {action: 'dialogue', task_id: 'task-a', command_id: 'd1', expected_turn: 1, prompt: 'p', question_id: 123},
    {action: 'reply', task_id: 'task-a', prompt: 'p', question_id: 123},
    {action: 'dialogue', task_id: 'task-a', command_id: 'd1', expected_turn: 1, prompt: 'p', question_id: null},
    {action: 'reply', task_id: 'task-a', prompt: 'p', question_id: null},
    {action: 'dialogue', task_id: 'task-a', command_id: 'w1', expected_turn: 1, target_command_id: 'd1', withdraw: true, question_id: '1:2'},
    {action: 'dialogue', task_id: 'task-a', command_id: 'w1', expected_turn: 1, target_command_id: 'd1', withdraw: true, question_id: ''},
    {action: 'dialogue', task_id: 'task-a', command_id: 'w1', expected_turn: 1, target_command_id: 'd1', withdraw: true, question_ids: ['1:2']},
  ];
  for (const invalid of rejectCases) {
    await assert.rejects(f.adapter.execute(invalid, f.ctx));
  }
  assert.equal(f.calls.length, callsAfterStart);
  assert.equal(f.timers.length, timersAfterStart);
  f.adapter.detach();
});

test('end-to-end real omp_bridge.py subprocess exercises question_ids on dialogue and reply (subset + D4 merge + raw bytes)', async () => {
  const {existsSync} = await import('node:fs');
  const {mkdtemp, writeFile, rm} = await import('node:fs/promises');
  const {execFileSync} = await import('node:child_process');
  const {tmpdir} = await import('node:os');
  const {join, dirname} = await import('node:path');
  const {fileURLToPath} = await import('node:url');
  const root = await mkdtemp(join(tmpdir(), 'callcli-omp-qids-e2e-'));
  const previous = process.env.CALL_CLI_PYTHON;
  const implDir = dirname(fileURLToPath(import.meta.url));
  const dbPath = join(root, 'tasks.db');
  try {
    process.env.CALL_CLI_PYTHON = 'python3';
    const f = fixture();
    const adapter = createAdapter({sendMessage: () => {}}, {db: dbPath, pollMs: 60000});

    // Pre-bind list must observe read-only without initializing DB; bind host-a/main cleanly via start+close.
    const initialList = await adapter.execute({action: 'list'}, f.ctx);
    assert.deepEqual(initialList.tasks, []);
    assert.equal(existsSync(dbPath), false);

    const wrapper = join(root, 'no-launch-python.sh');
    await writeFile(
      wrapper,
      `#!/bin/sh\nPYTHONPATH=${JSON.stringify(implDir)} exec python3 -B -c 'import runpy,sys,collab; collab.Collaboration._launch=lambda self,t:None; sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0], run_name="__main__")' "$2"\n`,
      {mode: 0o700},
    );
    process.env.CALL_CLI_PYTHON = wrapper;
    const bindSeed = await adapter.execute({action: 'start', agent: 'omp', cwd: root, prompt: 'bind host'}, f.ctx);
    await adapter.execute({action: 'cancel', task_id: bindSeed.task.id}, f.ctx);
    await adapter.execute({action: 'close', task_id: bindSeed.task.id}, f.ctx);
    process.env.CALL_CLI_PYTHON = 'python3';

    const seedScript = `
import json, sys
from bridge import Origin
from omp_bridge import HostBridge

db_path, cwd = sys.argv[1], sys.argv[2]
bridge = HostBridge(db_path, launch=lambda t: None)
host = {'session_id': 'host-a', 'agent_id': 'main'}
origin = Origin('omp-extension', host['session_id'], bridge.identity(host), 'omp')

dlg = bridge.store.create(origin, 'local', 'agy', cwd, 'native prompt', False)
bridge.store.update(dlg['id'], state='running', runner_pid=123, launch_turn=1, launch_id='l1', agy_swarm='native', agy_mode='swarm')
bridge.store.native_input_open(dlg['id'], 1, 'l1', 1)
for tok, text in [(' 1:raw ', 'Q1 raw?'), ('1:2', 'Q2 second?'), ('1:3', 'Q3 third?')]:
    bridge.store.event(dlg['id'], tok, 'question', text, turn=1)

rep = bridge.store.create(origin, 'local', 'codex', cwd, 'reply prompt', False)
bridge.store.update(rep['id'], state='waiting_reply', timeout=300)
for tok, text in [(' 1:raw ', 'Q1 raw?'), ('1:2', 'Q2 second?'), ('1:3', 'Q3 third?')]:
    bridge.store.event(rep['id'], tok, 'question', text, turn=1)

print(json.dumps({'dlg': dlg['id'], 'rep': rep['id']}))
`;
    const seeded = JSON.parse(execFileSync('python3', ['-B', '-c', seedScript, dbPath, root], {
      cwd: implDir,
      encoding: 'utf8',
    }));

    // Trimmed token must reject in real omp_bridge.py.
    await assert.rejects(adapter.execute({
      action: 'dialogue',
      task_id: seeded.dlg,
      prompt: 'trimmed',
      command_id: 'dq-trim',
      expected_turn: 1,
      question_ids: ['1:raw'],
    }, f.ctx));

    // Exact raw bytes on dialogue via real python3 -B omp_bridge.py.
    const dlgRes = await adapter.execute({
      action: 'dialogue',
      task_id: seeded.dlg,
      prompt: 'multi dlg',
      command_id: 'dq1',
      expected_turn: 1,
      question_ids: [' 1:raw ', '1:2'],
    }, f.ctx);
    assert.equal(dlgRes.task.commands.dq1.status, 'accepted');
    assert.deepEqual(dlgRes.task.commands.dq1.question_ids, [' 1:raw ', '1:2']);
    assert.deepEqual(dlgRes.task.commands.dq1.questions.map(q => q.id), [' 1:raw ', '1:2']);
    assert.deepEqual(dlgRes.task.questions, [' 1:raw ', '1:2', '1:3']);

    // Idempotent replay with reordered question_ids via real python3 -B omp_bridge.py.
    const dlgReplay = await adapter.execute({
      action: 'dialogue',
      task_id: seeded.dlg,
      prompt: 'multi dlg',
      command_id: 'dq1',
      expected_turn: 1,
      question_ids: ['1:2', ' 1:raw '],
    }, f.ctx);
    assert.deepEqual(dlgReplay.task.commands.dq1, dlgRes.task.commands.dq1);

    // Withdraw releases both bound IDs so a follow-up dialogue can re-bind ' 1:raw '.
    await adapter.execute({
      action: 'dialogue',
      task_id: seeded.dlg,
      command_id: 'wq1',
      expected_turn: 1,
      target_command_id: 'dq1',
      withdraw: true,
    }, f.ctx);
    const dlgRebound = await adapter.execute({
      action: 'dialogue',
      task_id: seeded.dlg,
      prompt: 'rebound',
      command_id: 'dq2',
      expected_turn: 1,
      question_ids: [' 1:raw '],
    }, f.ctx);
    assert.equal(dlgRebound.task.commands.dq2.status, 'accepted');
    assert.deepEqual(dlgRebound.task.commands.dq2.question_ids, [' 1:raw ']);

    // Subset reply via real python3 -B omp_bridge.py (stays waiting_reply, no runner launch).
    await assert.rejects(adapter.execute({
      action: 'reply',
      task_id: seeded.rep,
      question_ids: ['1:raw'],
      prompt: 'trimmed',
      expected_turn: 1,
      command_id: 'rq-trim',
    }, f.ctx));
    const subRes = await adapter.execute({
      action: 'reply',
      task_id: seeded.rep,
      question_ids: [' 1:raw ', '1:2'],
      prompt: 'shared',
      expected_turn: 1,
      command_id: 'rq1',
      clear_timeout: true,
    }, f.ctx);
    assert.equal(subRes.task.state, 'waiting_reply');
    assert.equal(subRes.task.turn, 1);
    assert.deepEqual(subRes.task.questions, ['1:3']);
    assert.equal(subRes.task.question_id, '1:3');
    assert.deepEqual(subRes.task.answers, {' 1:raw ': 'shared', '1:2': 'shared'});

    const subReplay = await adapter.execute({
      action: 'reply',
      task_id: seeded.rep,
      question_ids: ['1:2', ' 1:raw '],
      prompt: 'shared',
      expected_turn: 1,
      command_id: 'rq1',
      clear_timeout: true,
    }, f.ctx);
    assert.deepEqual(subReplay.task.answers, subRes.task.answers);

    // Final reply settles all questions into turn 2; reuse no-launch python3 wrapper to avoid spawning runner.py.
    process.env.CALL_CLI_PYTHON = wrapper;
    const finalRes = await adapter.execute({
      action: 'reply',
      task_id: seeded.rep,
      question_ids: ['1:3'],
      prompt: 'last',
      expected_turn: 1,
      command_id: 'rq2',
    }, f.ctx);
    assert.equal(finalRes.task.state, 'queued');
    assert.equal(finalRes.task.turn, 2);
    assert.equal(finalRes.task.timeout, null);
    assert.deepEqual(finalRes.task.questions, []);
    assert.deepEqual(finalRes.task.answers, {});

    const persisted = JSON.parse(execFileSync('python3', [
      '-B', '-c',
      'import json,sys; from bridge import Store; print(json.dumps(Store(sys.argv[1]).get(sys.argv[2])))',
      dbPath, seeded.rep,
    ], {cwd: implDir, encoding: 'utf8'}));
    assert.equal(persisted.prompt, ' 1:raw : shared\n1:2: shared\n1:3: last');

    adapter.detach();
  } finally {
    if (previous === undefined) delete process.env.CALL_CLI_PYTHON;
    else process.env.CALL_CLI_PYTHON = previous;
    await rm(root, {recursive: true, force: true});
  }
});

test('registered OMP tool Zod schema advertises blocker_interaction_seq as optional non-negative int', () => {
  const objectShapes = [];
  const makeNode = (kind, args = []) => {
    const ops = [{kind, args}];
    const node = new Proxy({ops}, {
      get(target, prop) {
        if (prop === 'ops') return target.ops;
        return (...methodArgs) => {
          target.ops.push({kind: prop, args: methodArgs});
          return node;
        };
      },
    });
    return node;
  };
  const z = new Proxy({}, {
    get(_target, name) {
      return (...args) => {
        if (name === 'object') objectShapes.push(args[0]);
        return makeNode(name, args);
      };
    },
  });
  let registered;
  register({zod: z, registerTool: tool => {registered = tool;}, on: () => {}});
  const shape = objectShapes.find(s => Object.hasOwn(s, 'action'));
  assert.ok(shape);
  assert.ok(Object.hasOwn(shape, 'blocker_interaction_seq'));
  assert.deepEqual(
    shape.blocker_interaction_seq.ops.map(op => [op.kind, op.args]),
    [['number', []], ['int', []], ['min', [0]], ['optional', []], ['describe', shape.blocker_interaction_seq.ops.at(-1).args]],
  );
});

test('createAdapter.execute forwards and validates blocker_interaction_seq without mutating state on rejection', async () => {
  const f = fixture();
  for (const preBindBad of [
    {action: 'list', blocker_interaction_seq: 0},
    {action: 'start', cwd: '/work', prompt: 'x', blocker_interaction_seq: 0},
  ]) {
    await assert.rejects(f.adapter.execute(preBindBad, f.ctx));
  }
  assert.equal(f.calls.length, 0);

  await f.adapter.execute({action: 'start', agent: 'agy', cwd: '/work', prompt: 'owned'}, f.ctx);
  const callsAfterStart = f.calls.length;
  const timersAfterStart = f.timers.length;

  const badCases = [
    {action: 'reply', task_id: 'task-a', prompt: 'p', question_id: '1:1', blocker_interaction_seq: 0},
    {action: 'status', task_id: 'task-a', blocker_interaction_seq: 0},
    {action: 'intervene', task_id: 'task-a', command_id: 'i1', prompt: 'p', blocker_interaction_seq: 0},
    {action: 'dialogue', task_id: 'task-a', command_id: 'd1', expected_turn: 1, prompt: 'p', blocker_interaction_seq: true},
    {action: 'dialogue', task_id: 'task-a', command_id: 'd1', expected_turn: 1, prompt: 'p', blocker_interaction_seq: false},
    {action: 'dialogue', task_id: 'task-a', command_id: 'd1', expected_turn: 1, prompt: 'p', blocker_interaction_seq: null},
    {action: 'dialogue', task_id: 'task-a', command_id: 'd1', expected_turn: 1, prompt: 'p', blocker_interaction_seq: -1},
    {action: 'dialogue', task_id: 'task-a', command_id: 'd1', expected_turn: 1, prompt: 'p', blocker_interaction_seq: 1.5},
    {action: 'dialogue', task_id: 'task-a', command_id: 'd1', expected_turn: 1, prompt: 'p', blocker_interaction_seq: '0'},
    {action: 'dialogue', task_id: 'task-a', command_id: 'd1', expected_turn: 1, prompt: 'p', blocker_interaction_seq: [0]},
    {action: 'dialogue', task_id: 'task-a', command_id: 'd1', expected_turn: 1, prompt: 'p', question_id: '1:1', blocker_interaction_seq: 0},
    {action: 'dialogue', task_id: 'task-a', command_id: 'd1', expected_turn: 1, prompt: 'p', question_ids: ['1:1'], blocker_interaction_seq: 0},
    {action: 'dialogue', task_id: 'task-a', command_id: 'w1', expected_turn: 1, target_command_id: 'd1', withdraw: true, blocker_interaction_seq: 0},
  ];
  for (const invalid of badCases) {
    await assert.rejects(f.adapter.execute(invalid, f.ctx));
  }
  assert.equal(f.calls.length, callsAfterStart);
  assert.equal(f.timers.length, timersAfterStart);

  f.setPacket({task: {id: 'task-a', state: 'running', turn: 1}, events: []});
  await f.adapter.execute({
    action: 'dialogue',
    task_id: 'task-a',
    prompt: 'explicit review 0',
    command_id: 'd-blk-0',
    expected_turn: 1,
    blocker_interaction_seq: 0,
  }, f.ctx);
  assert.deepEqual(f.calls.at(-1).args, {
    action: 'dialogue',
    task_id: 'task-a',
    prompt: 'explicit review 0',
    command_id: 'd-blk-0',
    expected_turn: 1,
    blocker_interaction_seq: 0,
  });
  f.adapter.detach();
});

test('end-to-end real omp_bridge.py subprocess exercises blocker_interaction_seq=0 accept, withdraw, re-bind and settled idempotency', async () => {
  const {existsSync} = await import('node:fs');
  const {mkdtemp, writeFile, rm} = await import('node:fs/promises');
  const {execFileSync} = await import('node:child_process');
  const {tmpdir} = await import('node:os');
  const {join, dirname} = await import('node:path');
  const {fileURLToPath} = await import('node:url');
  const root = await mkdtemp(join(tmpdir(), 'callcli-omp-blk-e2e-'));
  const previous = process.env.CALL_CLI_PYTHON;
  const implDir = dirname(fileURLToPath(import.meta.url));
  const dbPath = join(root, 'tasks.db');
  try {
    process.env.CALL_CLI_PYTHON = 'python3';
    const f = fixture();
    const adapter = createAdapter({sendMessage: () => {}}, {db: dbPath, pollMs: 60000});
    assert.deepEqual((await adapter.execute({action: 'list'}, f.ctx)).tasks, []);
    assert.equal(existsSync(dbPath), false);

    const wrapper = join(root, 'no-launch-python.sh');
    await writeFile(
      wrapper,
      `#!/bin/sh\nPYTHONPATH=${JSON.stringify(implDir)} exec python3 -B -c 'import runpy,sys,collab; collab.Collaboration._launch=lambda self,t:None; sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0], run_name="__main__")' "$2"\n`,
      {mode: 0o700},
    );
    process.env.CALL_CLI_PYTHON = wrapper;
    const bindSeed = await adapter.execute({action: 'start', agent: 'omp', cwd: root, prompt: 'bind host'}, f.ctx);
    await adapter.execute({action: 'cancel', task_id: bindSeed.task.id}, f.ctx);
    await adapter.execute({action: 'close', task_id: bindSeed.task.id}, f.ctx);
    process.env.CALL_CLI_PYTHON = 'python3';

    const seedScript = `
import json, sys
from bridge import Origin
from omp_bridge import HostBridge

db_path, cwd = sys.argv[1], sys.argv[2]
bridge = HostBridge(db_path, launch=lambda t: None)
host = {'session_id': 'host-a', 'agent_id': 'main'}
origin = Origin('omp-extension', host['session_id'], bridge.identity(host), 'omp')
dlg = bridge.store.create(origin, 'local', 'agy', cwd, 'native prompt', False)
bridge.store.update(dlg['id'], state='running', runner_pid=123, launch_turn=1, launch_id='l1', agy_swarm='native', agy_mode='swarm',
    native_observation=dict(turn=1, input_available=True, question_ids=[],
        question_blockers=[dict(interaction_seq=0, reason='incomplete_question', available_actions=['observe','dialogue','cancel'], blocks_completion=True)]))
bridge.store.native_input_open(dlg['id'], 1, 'l1', 1)
print(json.dumps({'dlg': dlg['id']}))
`;
    const seeded = JSON.parse(execFileSync('python3', ['-B', '-c', seedScript, dbPath, root], {cwd: implDir, encoding: 'utf8'}));

    const accepted = await adapter.execute({
      action: 'dialogue',
      task_id: seeded.dlg,
      prompt: 'review fragment 0',
      command_id: 'b0',
      expected_turn: 1,
      blocker_interaction_seq: 0,
    }, f.ctx);
    assert.equal(accepted.task.commands.b0.status, 'accepted');
    assert.equal(accepted.task.commands.b0.blocker_interaction_seq, 0);

    await assert.rejects(adapter.execute({
      action: 'dialogue',
      task_id: seeded.dlg,
      prompt: 'dup 0',
      command_id: 'b0-dup',
      expected_turn: 1,
      blocker_interaction_seq: 0,
    }, f.ctx));

    await adapter.execute({
      action: 'dialogue',
      task_id: seeded.dlg,
      command_id: 'wb0',
      expected_turn: 1,
      target_command_id: 'b0',
      withdraw: true,
    }, f.ctx);

    const rebound = await adapter.execute({
      action: 'dialogue',
      task_id: seeded.dlg,
      prompt: 'rebound 0',
      command_id: 'b1',
      expected_turn: 1,
      blocker_interaction_seq: 0,
    }, f.ctx);
    assert.equal(rebound.task.commands.b1.blocker_interaction_seq, 0);

    const settleScript = `
import sys
from bridge import Store
s = Store(sys.argv[1])
tid = sys.argv[2]
s.native_dialogue_claim(tid, 1, 'l1', 1)
s.native_dialogue_state(tid, 1, 'l1', 1, 'b1', 'send_started')
s.native_dialogue_state(tid, 1, 'l1', 1, 'b1', 'returned')
s.update(tid, state='completed', native_input=dict(accepting=False, settling=True, turn=1, launch_id='l1', epoch=1),
         native_observation=dict(turn=1, input_available=False, question_ids=[], question_blockers=[]))
`;
    execFileSync('python3', ['-B', '-c', settleScript, dbPath, seeded.dlg], {cwd: implDir, encoding: 'utf8'});
    const settledRetry = await adapter.execute({
      action: 'dialogue',
      task_id: seeded.dlg,
      prompt: 'rebound 0',
      command_id: 'b1',
      expected_turn: 1,
      blocker_interaction_seq: 0,
    }, f.ctx);
    assert.equal(settledRetry.task.state, 'completed');
    assert.equal(settledRetry.task.commands.b1.blocker_interaction_seq, 0);

    adapter.detach();
  } finally {
    if (previous === undefined) delete process.env.CALL_CLI_PYTHON;
    else process.env.CALL_CLI_PYTHON = previous;
    await rm(root, {recursive: true, force: true});
  }
});

test('recovery_status works pre-bind via read-only observe without scheduling timer and enforces strict task_id validation', async () => {
  const f = fixture(async value => {
    if (value.action === 'bind') return {capability: 'private-cap'};
    return {
      resource_recovery: {task_id: 'foreign-1', turn: 1, business_state: 'unknown', resource_state: 'unknown', write: true, receipt: null},
      diagnostic: {agent: 'agy', agy_mode: 'swarm', native_phase: 'unknown', stop_reason: null, exit_code: null, pending_question_count: 0, intervention_status: 'none', native_diagnostic: null},
    };
  });
  for (const [invalid, pattern] of [
    [{action: 'recovery_status'}, /recovery_status accepts only nonempty task_id/],
    [{action: 'recovery_status', task_id: ''}, /recovery_status accepts only nonempty task_id/],
    [{action: 'recovery_status', task_id: '   '}, /recovery_status accepts only nonempty task_id/],
    [{action: 'recovery_status', task_id: 't1', prompt: 'x'}, /recovery_status accepts only nonempty task_id/],
    [{action: 'recovery_status', task_id: 't1', expected_turn: 1}, /recovery_status accepts only nonempty task_id/],
    [{action: 'recovery_status', task_id: 't1', group: 'ops'}, /group requires start or list/],
    [{action: 'recovery_status', task_id: 't1', abandon: true}, /abandon requires release_only/],
  ]) {
    await assert.rejects(f.adapter.execute(invalid, f.ctx), pattern);
  }
  assert.equal(f.calls.length, 0);

  const res = await f.adapter.execute({action: 'recovery_status', task_id: 'foreign-1'}, f.ctx);
  assert.equal(res.resource_recovery.task_id, 'foreign-1');
  assert.equal(res.diagnostic.agent, 'agy');
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].action, 'observe');
  assert.equal(f.calls[0].args.action, 'recovery_status');
  assert.equal(f.timers.length, 0);
  f.adapter.detach();
});

test('status never starts, activates, or stops polling for owned or nonowned tasks while start/control wait remains active', async () => {
  const f = fixture(async value => {
    if (value.action === 'bind') return {capability: 'private-cap'};
    if (value.args.action === 'start') return {task: {id: 'owned-1', state: 'queued', host_binding: 'omp-extension', owned_by_bound_host: true}};
    if (value.args.action === 'close') return {task: {id: 'owned-1', state: 'closed', host_binding: 'omp-extension', owned_by_bound_host: true}};
    if (value.args.action === 'status' && value.args.task_id === 'foreign') {
      return {task: {id: 'foreign', state: 'running', host_binding: 'omp-extension', owned_by_bound_host: false}};
    }
    if (value.args.action === 'status' && value.args.task_id === 'owned-2') {
      return {task: {id: 'owned-2', state: 'running', host_binding: 'omp-extension', owned_by_bound_host: true}};
    }
    return {task: {id: value.args.task_id, state: 'completed'}, events: []};
  });
  await f.adapter.execute({action: 'list'}, f.ctx);
  assert.equal(f.timers.length, 0);

  // Bind host via start+close so no track is active; owned and nonowned status must keep timers=0.
  await f.adapter.execute({action: 'start', cwd: '/work', prompt: 'owned'}, f.ctx);
  assert.equal(f.timers.length, 1);
  await f.adapter.execute({action: 'close', task_id: 'owned-1'}, f.ctx);
  assert.equal(f.timers.length, 0);

  await f.adapter.execute({action: 'status', task_id: 'foreign'}, f.ctx);
  assert.equal(f.timers.length, 0, 'unowned task status must never activate polling');
  await f.adapter.execute({action: 'status', task_id: 'owned-2'}, f.ctx);
  assert.equal(f.timers.length, 0, 'owned task status must never activate polling');

  // Re-start owned-1: active polling stays intact across foreign and owned status calls.
  await f.adapter.execute({action: 'start', cwd: '/work', prompt: 'owned again'}, f.ctx);
  assert.equal(f.timers.length, 1);
  await f.adapter.execute({action: 'status', task_id: 'foreign'}, f.ctx);
  await f.adapter.execute({action: 'status', task_id: 'owned-2'}, f.ctx);
  assert.equal(f.timers.length, 1, 'status must not disturb existing control-started wait');
  f.adapter.detach();
});

test('end-to-end real omp_bridge.py subprocess supports pre-bind list and recovery_status with zero DB/dir/mode writes', async () => {
  const {existsSync} = await import('node:fs');
  const {mkdtemp, readFile, rm, stat} = await import('node:fs/promises');
  const {execFileSync} = await import('node:child_process');
  const {tmpdir} = await import('node:os');
  const {join, dirname} = await import('node:path');
  const {fileURLToPath} = await import('node:url');
  const root = await mkdtemp(join(tmpdir(), 'callcli-omp-rec-e2e-'));
  const previous = process.env.CALL_CLI_PYTHON;
  const implDir = dirname(fileURLToPath(import.meta.url));
  const missingDb = join(root, 'missing-dir', 'tasks.db');
  const dbPath = join(root, 'tasks.db');
  try {
    process.env.CALL_CLI_PYTHON = 'python3';
    const fMissing = fixture();
    const missingAdapter = createAdapter({sendMessage: () => {}}, {db: missingDb, pollMs: 60000});
    assert.deepEqual((await missingAdapter.execute({action: 'list'}, fMissing.ctx)).tasks, []);
    await assert.rejects(missingAdapter.execute({action: 'recovery_status', task_id: 'missing'}, fMissing.ctx), /KeyError/);
    assert.equal(existsSync(dirname(missingDb)), false);
    assert.equal(existsSync(missingDb), false);
    assert.equal(fMissing.timers.length, 0);
    missingAdapter.detach();

    const seedScript = `
import json, sys
from bridge import Origin
from omp_bridge import HostBridge

db_path, cwd = sys.argv[1], sys.argv[2]
bridge = HostBridge(db_path, launch=lambda t: None)
other = {'session_id': 'other-secret-sid', 'agent_id': 'other-secret-agent'}
cap = bridge.bind(other)
t = bridge.handle(other, cap, {'action': 'start', 'agent': 'agy', 'cwd': cwd, 'prompt': 'CANARY_PROMPT'})['task']
bridge.store.update(t['id'], state='unknown', runner_pid=0, settlement_note='CANARY_NOTE',
    native_observation={'turn': 1, 'history_degraded': True, 'pending_tools': [{'id': 'CANARY_TID', 'name': 'CANARY_TOOL', 'kind': 'read', 'status': 'in_progress'}]})
print(json.dumps({'task_id': t['id']}))
`;
    const seeded = JSON.parse(execFileSync('python3', ['-B', '-c', seedScript, dbPath, root], {cwd: implDir, encoding: 'utf8'}));

    const f = fixture();
    const adapter = createAdapter({sendMessage: () => {}}, {db: dbPath, pollMs: 60000});
    const before = await readFile(dbPath);
    const beforeMode = (await stat(dbPath)).mode & 0o777;
    const listed = await adapter.execute({action: 'list'}, f.ctx);
    assert.deepEqual(listed.tasks, []);
    assert.deepEqual(await readFile(dbPath), before);
    assert.equal((await stat(dbPath)).mode & 0o777, beforeMode);

    const first = await adapter.execute({action: 'recovery_status', task_id: seeded.task_id}, f.ctx);
    assert.deepEqual(await readFile(dbPath), before);
    assert.equal((await stat(dbPath)).mode & 0o777, beforeMode);
    const second = await adapter.execute({action: 'recovery_status', task_id: seeded.task_id}, f.ctx);
    assert.deepEqual(await readFile(dbPath), before);
    assert.equal(second.resource_recovery.task_id, seeded.task_id);
    assert.equal(second.diagnostic.agent, 'agy');
    assert.equal(second.diagnostic.native_diagnostic.history_degraded, true);
    assert.deepEqual(second.diagnostic.native_diagnostic.pending_tool_kinds, ['read']);
    for (const secret of ['CANARY_', 'other-secret-sid', 'other-secret-agent', 'host-a']) {
      assert.ok(!JSON.stringify(first).includes(secret));
      assert.ok(!JSON.stringify(second).includes(secret));
    }
    assert.equal(f.timers.length, 0);
    adapter.detach();
  } finally {
    if (previous === undefined) delete process.env.CALL_CLI_PYTHON;
    else process.env.CALL_CLI_PYTHON = previous;
    await rm(root, {recursive: true, force: true});
  }
});

test('routine diagnostics and routine skills are suppressed without stalling cursor while attention events deliver', async () => {
  const f = fixture();
  await f.adapter.execute({action: 'start', agent: 'omp', cwd: '/work', prompt: 'noise filter'}, f.ctx);
  f.setPacket({
    task: {id: 'task-a', state: 'running', turn: 1},
    events: [
      {seq: 1, turn: 1, kind: 'diagnostic', reason_code: 'record_observed', text: 'record observed'},
      {seq: 2, turn: 1, kind: 'diagnostic', reason_code: 'submission_attempted', text: 'submission attempted'},
      {seq: 3, turn: 1, kind: 'diagnostic', reason_code: 'acceptance_observed', text: 'acceptance observed'},
      {seq: 4, turn: 1, kind: 'diagnostic', reason_code: 'result_observed', text: 'result observed'},
      {seq: 5, turn: 1, kind: 'skill', text: 'skill-1 read'},
    ],
  });
  await f.timers.shift()();
  assert.equal(f.sent.length, 0, 'routine noise must not wake model');

  f.setPacket({
    task: {id: 'task-a', state: 'running', turn: 1},
    events: [
      {seq: 6, turn: 1, kind: 'diagnostic', reason_code: 'turn_failed', text: 'non-routine failure diagnostic'},
      {seq: 7, turn: 1, kind: 'skill', attention_required: true, text: 'skill-1 unavailable'},
      {seq: 8, turn: 1, kind: 'stage', quiet_notice: true, notice_kind: 'tool_churn_only', tool_churn_only: true, elapsed: 601, text: 'Coordinator review requested (tool_churn_only)'},
    ],
  });
  await f.timers.shift()();
  assert.equal(f.calls.at(-1).args.after, 5, 'cursor must advance past suppressed routine events');
  assert.equal(f.sent.length, 1);
  const delivered = JSON.parse(f.sent[0].message.content).events;
  assert.deepEqual(delivered.map(e => e.seq), [6, 7, 8]);
  assert.equal(delivered[2].notice_kind, 'tool_churn_only');
  assert.equal(delivered[2].tool_churn_only, true);
  f.adapter.detach();
});

test('omp extension does not export duplicate project-stop classifier', () => {
  assert.equal(Object.hasOwn(ompExt, 'classifyWorkStop'), false);
});

test('normal completed and waiting_reply turns deliver durable terminal verbatim without false global stop', async () => {
  let starts = 0;
  const packets = new Map();
  const f = fixture(async r => {
    if (r.action === 'bind') return {capability: 'cap'};
    if (r.args.action === 'start') {
      const id = `task-${++starts}`;
      return {task: {id, state: 'queued', turn: 1, agent: 'omp'}};
    }
    return packets.get(r.args.task_id) || {task: {id: r.args.task_id, state: 'running', turn: 1, agent: 'omp'}, events: []};
  });

  await f.adapter.execute({action: 'start', agent: 'omp', cwd: '/work', prompt: 'complete cleanly'}, f.ctx);
  packets.set('task-1', {
    task: {id: 'task-1', agent: 'omp', state: 'completed', turn: 1, stop_reason: 'end_turn'},
    events: [
      {seq: 1, kind: 'answer', turn: 1, text: 'DONE_OK'},
      {seq: 2, kind: 'completed', turn: 1, text: 'Prompt exited 0', answer_tail: 'DONE_OK\n'},
    ],
  });
  await f.timers.shift()();
  const msg1 = JSON.parse(f.sent[0].message.content);
  assert.equal(msg1.task_id, 'task-1');
  assert.deepEqual(msg1.events.map(e => e.kind), ['answer', 'completed']);
  assert.equal(msg1.events[0].text, 'DONE_OK');
  assert.equal(msg1.events[1].seq, 2);
  assert.equal(Object.hasOwn(msg1, 'work_stop'), false);
  assert.equal(f.sent[0].message.content.includes('工作已停，未完成'), false);
  assert.equal(f.sent[0].message.content.includes('global_stopped'), false);
  assert.equal(f.timers.length, 0);

  await f.adapter.execute({action: 'start', agent: 'omp', cwd: '/work', prompt: 'ask question'}, f.ctx);
  packets.set('task-2', {
    task: {id: 'task-2', agent: 'omp', state: 'waiting_reply', turn: 1, question_id: '1:1', questions: ['1:1']},
    events: [
      {seq: 3, kind: 'question', turn: 1, text: 'Q1 Proceed?'},
      {seq: 4, kind: 'waiting_reply', turn: 1, text: 'Prompt exited 0', question_ids: ['1:1']},
    ],
  });
  await f.timers.shift()();
  const msg2 = JSON.parse(f.sent[1].message.content);
  assert.equal(msg2.task_id, 'task-2');
  assert.deepEqual(msg2.events.map(e => e.kind), ['question', 'waiting_reply']);
  assert.deepEqual(msg2.events[1].question_ids, ['1:1']);
  assert.equal(Object.hasOwn(msg2, 'work_stop'), false);
  assert.equal(f.sent[1].message.content.includes('工作已停，未完成'), false);
  assert.equal(f.sent[1].message.content.includes('global_stopped'), false);
  assert.equal(f.timers.length, 0);
  f.adapter.detach();
});

test('real unknown event and task_snapshot fallback still flow back with unrelated running and queued tasks', async () => {
  let starts = 0;
  const packets = new Map();
  const f = fixture(async r => {
    if (r.action === 'bind') return {capability: 'cap'};
    if (r.args.action === 'start') {
      const id = `task-${++starts}`;
      return {task: {id, state: 'queued', turn: 1, agent: 'omp'}};
    }
    return packets.get(r.args.task_id) || {task: {id: r.args.task_id, state: 'running', turn: 1, agent: 'omp'}, events: []};
  });

  await f.adapter.execute({action: 'start', agent: 'omp', cwd: '/work', prompt: 'failing event task'}, f.ctx);
  await f.adapter.execute({action: 'start', agent: 'omp', cwd: '/work', prompt: 'unrelated running'}, f.ctx);
  await f.adapter.execute({action: 'start', agent: 'omp', cwd: '/work', prompt: 'unrelated queued'}, f.ctx);
  await f.adapter.execute({action: 'start', agent: 'omp', cwd: '/work', prompt: 'failing snapshot task'}, f.ctx);

  packets.set('task-1', {
    task: {id: 'task-1', agent: 'omp', state: 'unknown', turn: 1},
    events: [{seq: 1, kind: 'unknown', turn: 1, attention_required: true, text: 'OMP task-1 turn 1 needs attention'}],
  });
  packets.set('task-2', {task: {id: 'task-2', agent: 'omp', state: 'running', turn: 1}, events: []});
  packets.set('task-3', {task: {id: 'task-3', agent: 'omp', state: 'queued', turn: 1}, events: []});
  packets.set('task-4', {
    task: {id: 'task-4', agent: 'omp', state: 'unknown', turn: 1, settlement_note: 'missing final answer'},
    events: [],
  });

  await f.timers.shift()();
  assert.equal(f.sent.length, 2);
  const msg1 = JSON.parse(f.sent[0].message.content);
  assert.equal(msg1.task_id, 'task-1');
  assert.equal(msg1.events.length, 1);
  assert.equal(msg1.events[0].kind, 'unknown');
  assert.equal(msg1.events[0].attention_required, true);
  assert.equal(Object.hasOwn(msg1, 'work_stop'), false);
  assert.equal(f.sent[0].message.content.includes('branch_stopped'), false);
  assert.equal(f.sent[0].message.content.includes('工作已停，未完成'), false);

  const msg4 = JSON.parse(f.sent[1].message.content);
  assert.equal(msg4.task_id, 'task-4');
  assert.equal(msg4.events.length, 1);
  assert.equal(msg4.events[0].kind, 'unknown');
  assert.equal(msg4.events[0].source, 'task_snapshot');
  assert.equal(msg4.events[0].attention_required, true);
  assert.equal(msg4.events[0].turn, 1);
  assert.equal(Object.hasOwn(msg4, 'work_stop'), false);
  assert.equal(f.sent[1].message.content.includes('branch_stopped'), false);
  assert.equal(f.sent[1].message.content.includes('工作已停，未完成'), false);
  f.adapter.detach();
});

test('cancelled with pending intervention delivers terminal verbatim and keeps polling without work_stop', async () => {
  const f = fixture();
  await f.adapter.execute({action: 'start', agent: 'omp', cwd: '/work', prompt: 'intervened task'}, f.ctx);
  f.setPacket({
    task: {id: 'task-a', agent: 'omp', state: 'cancelled', turn: 1, intervention: {status: 'pending'}},
    events: [{seq: 1, kind: 'cancelled', turn: 1, text: 'Prompt exited 0', next_action: 'Retry intervene or call wait'}],
  });
  await f.timers.shift()();
  assert.equal(f.sent.length, 1);
  const msg = JSON.parse(f.sent[0].message.content);
  assert.equal(msg.task_id, 'task-a');
  assert.deepEqual(msg.events.map(e => e.kind), ['cancelled']);
  assert.equal(Object.hasOwn(msg, 'work_stop'), false);
  assert.equal(f.timers.length, 1, 'polling must remain active for pending intervention target');
  f.adapter.detach();
});
