import test from 'node:test';
import assert from 'node:assert/strict';
import {createAdapter} from './omp-extension.mjs';

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
