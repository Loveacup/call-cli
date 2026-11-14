'use strict';
const assert = require('node:assert/strict');
const {closeTarget, probeTarget} = require('./release-backend.cjs');
const target = {task:'fixture-task',turn:4,record:'11111111-1111-4111-8111-111111111111',
  acp:'fixture-acp',session:'call-fixture-task',cwd:'/fixture',agent:'omp'};
function adapter({badReadback=false,replacement=false,badBefore}={}) {
  let closed=false, closes=0;
  const runtime={
    getStatus: async()=>({acpxRecordId:badReadback&&closed?'other':target.record,
      backendSessionId:target.acp,lastRequestId:'fixture-task:4',details:{cwd:target.cwd,closed},
      ...(!closed && badBefore ? badBefore : {})}),
    close:async()=>{closed=true;closes++;},
    findSession:async()=>replacement?{acpxRecordId:'other'}:closed?undefined:
      {acpxRecordId:target.record,backendSessionId:target.acp,cwd:target.cwd,sessionKey:target.session},
    shutdown:async()=>{}
  };
  return {deps:{loadRuntime:async()=>({createAgentRegistry:()=>({}),
    createSharedAcpRuntime:()=>runtime})},closes:()=>closes};
}
(async()=>{
  const good=adapter();
  const result=await closeTarget(target,good.deps);
  assert.equal(result.verified,true);
  assert.equal(result.close.source,'shared-runtime-resolved-and-exact-record-readback');
  assert.equal(result.status.status,'no-session');
  assert.equal(result.status.binding.acpxRecordId,target.record);
  assert.equal(good.closes(),1);
  for(const options of [{badReadback:true},{replacement:true}]) {
    const bad=adapter(options);
    await assert.rejects(closeTarget(target,bad.deps));
    if(options.replacement) assert.equal(bad.closes(),0);
  }
  for(const badBefore of [{lastRequestId:'fixture-task:3'},
    {backendSessionId:'wrong-acp'}, {details:{cwd:'/wrong',closed:false}}]) {
    const bad=adapter({badBefore});
    await assert.rejects(closeTarget(target,bad.deps));
    assert.equal(bad.closes(),0);
  }
  for (const variant of ['closed','open','wrong-record','replaced']) {
    let closes = 0;
    const runtime = {
      getStatus:async()=>({acpxRecordId:variant==='wrong-record'?'other':target.record,
        backendSessionId:target.acp,lastRequestId:'fixture-task:4',
        details:{cwd:target.cwd,closed:variant!=='open'}}),
      findSession:async()=>variant==='replaced'?{acpxRecordId:'replacement'}:undefined,
      close:async()=>{closes++;throw Error('PROBE_MUST_NOT_CLOSE');},shutdown:async()=>{}
    };
    const deps = {loadRuntime:async()=>({createAgentRegistry:()=>({}),createSharedAcpRuntime:()=>runtime})};
    if (variant==='closed') assert.equal((await probeTarget(target,deps)).status,'no-session');
    else await assert.rejects(probeTarget(target,deps), error=>error.stage==='probe');
    assert.equal(closes,0);
  }
  const dual=adapter();
  const originalRuntime=dual.deps.loadRuntime;
  dual.deps.loadRuntime=async()=>{
    const sdk=await originalRuntime();
    const create=sdk.createSharedAcpRuntime;
    sdk.createSharedAcpRuntime=()=>{
      const runtime=create();
      runtime.getStatus=async()=>{throw Object.assign(Error('PRIVATE_ORIGINAL'),{code:'EIO'});};
      runtime.shutdown=async()=>{throw Error('PRIVATE_SHUTDOWN');};
      return runtime;
    };
    return sdk;
  };
  await assert.rejects(closeTarget(target,dual.deps),e=>e.stage==='resolve' && e.substep==='status_before'
    && e.code==='EIO' && e.shutdownFailure?.name==='Error');
  await assert.rejects(closeTarget(target,{loadRuntime:async()=>{throw Error('PRIVATE_LOAD');}}),
    e=>e.stage==='load' && e.substep==='load_runtime');
  console.log('STRICT_NATIVE_RELEASE_PROOF_OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
