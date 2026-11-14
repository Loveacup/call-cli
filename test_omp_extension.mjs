import test from 'node:test';
import assert from 'node:assert/strict';
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
  assert.ok(!f.calls.some(c=>c.args?.action==='cancel'));
  f.adapter.detach();
});

test('registered OMP tool advertises dialogue without host identity fields', () => {
  const enums=[];
  const chain=new Proxy({}, {get:()=>()=>chain});
  const z=new Proxy({}, {get:(_target,name)=>(...args)=>{
    if(name==='enum') enums.push(args[0]);
    return chain;
  }});
  let registered;
  register({zod:z,registerTool:tool=>{registered=tool;},on:()=>{}});
  assert.equal(registered.name,'call_cli_omp');
  assert.ok(enums.some(values=>values.includes('dialogue')));
  assert.ok(enums.some(values=>values.includes('teamwork')));
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
  f.setPacket({task,events:[{seq:7,kind:'unknown',turn:1,text:'no final answer',attention_required:true}]});
  await f.timers.shift()();
  assert.equal(f.sent.length,1,'late terminal event must not repeat snapshot attention');
  await f.adapter.execute({action:'status',task_id:'task-a'},f.ctx);
  f.setPacket({task:{...task,turn:2},events:[]});
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
  f.setPacket({task,events:[{seq:2,kind:'unknown',turn:1,attention_required:true,text:'same obligation'}]});
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
  await f.timers.shift()();
  assert.equal(f.sent.length,2);
  const events=JSON.parse(f.sent[1].message.content).events;
  assert.deepEqual(events.map(e=>e.kind),['stage','question','answer','skill','unknown']);
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
  f.setPacket({task,events:[]});
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
  }
  f.setPacket({task:{...task,turn:2},events:[]});
  await f.timers.shift()();
  await f.adapter.execute({action:'status',task_id:'task-a'},f.ctx);
  f.setPacket({task:{...task,turn:3,state:'running'},events:[
    {seq:1,kind:'unknown',turn:1,text:'late turn one'},
    {seq:2,kind:'stage',turn:3,text:'progress'},
    {seq:3,kind:'question',turn:3,text:'question'},
    {seq:4,kind:'answer',turn:3,text:'answer'},
    {seq:5,kind:'skill',turn:3,text:'skill'}]});
  await f.timers.shift()();
  const delivered=f.sent.flatMap(s=>JSON.parse(s.message.content).events);
  assert.deepEqual(delivered.filter(e=>e.kind==='unknown').map(e=>e.turn),previouslyNotified ? [1,2] : [2,1]);
  assert.deepEqual(JSON.parse(f.sent.at(-1).message.content).events.filter(e=>e.kind!=='unknown').map(e=>e.kind),
    ['stage','question','answer','skill']);
  f.setPacket({task:{...task,turn:3,state:'running'},events:[{seq:6,kind:'unknown',turn:1,text:'repeat late turn one'}]});
  const count=f.sent.length;
  await f.timers.shift()();
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
  await f.timers.shift()();
  assert.equal(f.sent.length,1,'a no-turn snapshot still has a remembered fallback obligation');
  await f.adapter.execute({action:'status',task_id:'task-a'},f.ctx);
  f.setPacket({task,events:[{seq:1,kind:'unknown',text:'unattributed legacy terminal'}]});
  await f.timers.shift()();
  assert.equal(f.sent.length,2,'unknown-turn events remain cursor-visible');
  await f.adapter.execute({action:'status',task_id:'task-a'},f.ctx);
  f.setPacket({task,events:[]});
  await f.timers.shift()();
  assert.equal(f.sent.length,2);
});
