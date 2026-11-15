'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const native = {skip:process.env.RUN_NATIVE_ACPX_TESTS !== '1'};
const {control, loadRuntime} = require('./shared-prompt.cjs');
const id = 'a1111111-1111-4111-8111-111111111111';
const other = 'b1111111-1111-4111-8111-111111111111';
const auth = async () => ({authCredentials: {}, authPolicy: 'skip'});

test('saved legacy argv record remains the exact target despite configuration and same-name new scope', native, async () => {
  const home = await fs.mkdtemp(path.join(process.env.TMPDIR ?? require('node:os').tmpdir(), 'control-identity-'));
  const sdk = await loadRuntime('0.22.0');
  const priorHome = process.env.HOME;
  process.env.HOME = home;
  const store = sdk.createFileSessionStore({stateDir: path.join(home, '.acpx')});
  const now = new Date().toISOString();
  const record = {schema:'acpx.session.v1', acpxRecordId:id, acpSessionId:'synthetic-old',
    agentCommand:'omp acp', agentArgv:['omp','acp'], cwd:home, name:'old-win-skip',
    createdAt:now, lastUsedAt:now, lastSeq:0, closed:false, messages:[], updated_at:now, acpx:{}};
  await store.save(record);
  const unboundFrames=[];
  const unbound={cwd:home,home,session:record.name,agent:'omp',agentArgv:['omp','acp','--auto-approve','--allow-home'],permissionMode:'approve-all'};
  assert.equal(await control({...unbound,action:'show'},{loadRuntime:async()=>sdk,loadAuth:auth,
    resolveConfiguredAgentArgv:async(_a,argv)=>argv,send:async f=>unboundFrames.push(f)}),0);
  assert.equal(unboundFrames[0].acpxRecordId,id);
  await store.save({...record, acpxRecordId:other, acpSessionId:'synthetic-other',
    agentCommand:'omp acp --auto-approve --allow-home', agentArgv:['omp','acp','--auto-approve','--allow-home']});
  const base = {cwd:home, home, session:record.name, agent:'omp', recordId:id,
    backendSessionId:record.acpSessionId, agentArgv:['omp','acp','--auto-approve','--allow-home'], permissionMode:'approve-all'};
  const invoke = async opts => {
    const frames=[];
    const rc=await control({...base,...opts},{loadRuntime:async()=>sdk,loadAuth:auth,
      resolveConfiguredAgentArgv:async()=>['changed-launcher','acp'],send:async f=>frames.push(f)});
    return {rc,frames};
  };
  try {
    const status=await invoke({action:'status'});
    assert.equal(status.rc,0); assert.equal(status.frames[0].acpxRecordId,id);
    assert.equal((await invoke({action:'show'})).frames[0].agentCommand,'omp acp');
    const ambiguous=[];
    assert.equal(await control({...base,recordId:undefined,backendSessionId:undefined,action:'show'},
      {loadRuntime:async()=>sdk,loadAuth:auth,resolveConfiguredAgentArgv:async(_a,a)=>a,send:async f=>ambiguous.push(f)}),1);
    assert.deepEqual(ambiguous,[]);
    for (const bad of [{session:'foreign'}, {backendSessionId:'foreign'}, {recordId:other}]) {
      const result=await invoke({action:'close',...bad});
      assert.equal(result.rc,1); assert.deepEqual(result.frames,[]);
    }
    assert.equal((await invoke({action:'cancel'})).rc,0);
    assert.equal((await invoke({action:'close'})).rc,0);
    assert.equal((await store.load(id)).closed,true);
    assert.equal((await store.load(other)).closed,false);
    assert.equal((await invoke({action:'status'})).frames[0].status,'no-session');
  } finally {process.env.HOME = priorHome; await fs.rm(home,{recursive:true,force:true});}
});
for (const action of ['model','mode','effort']) {
  test(`${action}: rejected, timed-out and uncertain owner controls never connect embedded or report success`,async()=>{
    for (const code of ['ACP_CONTROL_REJECTED','TIMEOUT','ACK_LOST']) {
      let embedded=0, dispatches=0;
      const frames=[];
      const handle={backend:'acpx-shared',acpxRecordId:id,backendSessionId:'synthetic',cwd:'/synthetic',sessionKey:'owned'};
      const reject=async()=>{dispatches++;throw Object.assign(Error('PRIVATE_FAILURE'),{code});};
      const rc=await control({action,value:'requested',cwd:'/synthetic',session:'owned',agent:'omp',
        agentArgv:['omp','acp'],permissionMode:'approve-all'}, {
        loadAuth:auth,resolveConfiguredAgentArgv:async(_a,argv)=>argv,
        createSessionStore:()=>({load:async()=>({acpSessionId:'synthetic'})}),
        loadRuntime:async()=>({createAgentRegistry:()=>({}),createSharedAcpRuntime:()=>({
          findSession:async()=>handle,setModel:reject,setMode:reject,setConfigOption:reject,shutdown:async()=>{}}),
          createAcpRuntime:()=>{embedded++;return {setModel:async()=>{},setMode:async()=>{},setConfigOption:async()=>{},shutdown:async()=>{}};}}),
        send:async f=>frames.push(f)});
      assert.equal(rc,1,code);assert.equal(embedded,0,code);assert.equal(dispatches,1,code);assert.deepEqual(frames,[],code);
    }
  });
}

test('unavailable owner with alive, dead or mismatched idle status never opens direct control', async () => {
  for (const status of ['alive','dead','wrong-idle']) {
    let embedded=0;
    const record={acpxRecordId:id,acpSessionId:'saved',cwd:'/synthetic',name:'owned',agentArgv:['omp','acp']};
    const frames=[];
    const rc=await control({action:'mode',value:'agent-full-access',cwd:record.cwd,session:record.name,
      recordId:id,backendSessionId:'saved',agent:'omp',agentArgv:['omp','acp'],permissionMode:'approve-all'}, {
      loadAuth:auth,createSessionStore:()=>({load:async()=>record}),
      controlStatus:async()=>({status:status==='wrong-idle'?'idle':status,acpxRecordId:status==='wrong-idle'?other:id,acpxSessionId:'saved'}),
      loadRuntime:async()=>({createAgentRegistry:()=>({}),createSharedAcpRuntime:()=>({
        setMode:async()=>{throw Object.assign(Error('unavailable'),{code:'ACP_BACKEND_UNAVAILABLE'});},shutdown:async()=>{}}),
        createAcpRuntime:()=>{embedded++;throw Error('Unexpected direct connection');}}),send:async f=>frames.push(f)});
    assert.equal(rc,1);assert.equal(embedded,0);assert.deepEqual(frames,[]);
  }
});

test('Windows path spelling is normalized without accepting another directory', async () => {
  const record={acpxRecordId:id,acpSessionId:'saved',cwd:'C:\\Work',name:'owned',agentArgv:['omp','acp']};
  for(const [cwd,expected] of [['c:/work/.',0],['C:/elsewhere',1]]) {
    const frames=[];
    const rc=await control({action:'show',cwd,session:'owned',recordId:id,agent:'omp',
      agentArgv:['omp','acp'],permissionMode:'approve-all'}, {
      platform:'win32',loadAuth:auth,createSessionStore:()=>({load:async()=>record}),
      loadRuntime:async()=>({createAgentRegistry:()=>({}),createSharedAcpRuntime:()=>({shutdown:async()=>{}})}),
      send:async f=>frames.push(f)});
    assert.equal(rc,expected);assert.equal(frames.length,expected===0?1:0);
  }
});

test('real SDK idle mode/model, first and continued turns, and strict load failure never create another session', native, async () => {
  const home = await fs.mkdtemp(path.join(process.env.TMPDIR ?? require('node:os').tmpdir(),'control-lifecycle-'));
  const priorHome=process.env.HOME; process.env.HOME=home;
  const sdk=await loadRuntime('0.22.0');
  const store=sdk.createFileSessionStore({stateDir:path.join(home,'.acpx')});
  const trace=path.join(home,'trace.jsonl');
  const argv=[process.env.CALLCLI_TEST_PYTHON ?? 'python3','-B',path.join(__dirname,'fixtures/control_agent.py'),trace,'ok'];
  const registry=sdk.createAgentRegistry({overrides:{omp:argv}});
  const owner=sdk.createSharedAcpRuntime({cwd:home,permissionMode:'approve-all',agentRegistry:registry});
  const base={cwd:home,home,agent:'omp',agentArgv:argv,session:'fixture',permissionMode:'approve-all',timeoutMs:5000};
  const invoke=async opts=>{
    const frames=[];
    const rc=await control({...base,...opts},{loadRuntime:async()=>sdk,loadAuth:auth,
      resolveConfiguredAgentArgv:async(_a,a)=>a,send:async f=>frames.push(f)});
    return {rc,frames};
  };
  let handle;
  try {
    const made=await invoke({action:'new'}); assert.equal(made.rc,0);
    base.recordId=made.frames[0].acpxRecordId; base.backendSessionId=made.frames[0].acpxSessionId;
    const initial=await store.load(base.recordId);
    await store.save({...initial,acpx:{...initial.acpx,reset_on_next_ensure:true}});
    const beforeRestore=(await fs.readFile(trace,'utf8')).trim().split('\n').length;
    const refused=await invoke({action:'mode',value:'agent-full-access'});
    assert.equal(refused.rc,1);assert.deepEqual(refused.frames,[]);
    const restoreMethods=(await fs.readFile(trace,'utf8')).trim().split('\n').slice(beforeRestore).map(x=>JSON.parse(x).method);
    assert.ok(!restoreMethods.includes('session/new'));
    await store.save(initial);
    assert.equal((await invoke({action:'mode',value:'agent-full-access'})).rc,0);
    assert.equal((await store.load(base.recordId)).acpx.desired_mode_id,'agent-full-access');
    assert.equal((await invoke({action:'model',value:'two'})).rc,0);
    assert.equal((await store.load(base.recordId)).acpx.current_model_id,'two');
    handle={backend:'acpx-shared',acpxRecordId:base.recordId,runtimeSessionName:base.recordId,cwd:home,sessionKey:'fixture'};
    for (const turn of [1,2]) {
      const t=owner.startTurn({handle,text:'synthetic',mode:'prompt',requestId:`fixture:${turn}`});
      for await(const event of t.events) {}
      assert.equal((await t.result).status,'completed');
    }
    assert.equal((await invoke({action:'model',value:'one'})).rc,0);
    assert.equal((await invoke({action:'close'})).rc,0);
    const saved=await store.load(base.recordId);
    await store.save({...saved,closed:false,pid:undefined,agentArgv:[...argv.slice(0,-1),'fail-load'],
      agentCommand:require('./shared-prompt.cjs').renderArgvIdentity([...argv.slice(0,-1),'fail-load'])});
    const before=(await fs.readFile(trace,'utf8')).trim().split('\n').length;
    const failure=await invoke({action:'mode',value:'agent-full-access'});
    assert.equal(failure.rc,1); assert.deepEqual(failure.frames,[]);
    const methods=(await fs.readFile(trace,'utf8')).trim().split('\n').slice(before).map(x=>JSON.parse(x).method);
    assert.ok(methods.includes('session/load')); assert.ok(!methods.includes('session/new'));
    assert.equal((await store.load(base.recordId)).acpSessionId,base.backendSessionId);
  } finally {
    if(handle) await owner.close({handle});
    await owner.shutdown(); process.env.HOME=priorHome; await fs.rm(home,{recursive:true,force:true});
  }
});
