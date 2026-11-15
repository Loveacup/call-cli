'use strict';
const assert = require('node:assert/strict');
const {closeTarget, probeTarget, failureReason} = require('./release-backend.cjs');
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
  // Same pre-close lookup miss followed by an open-record probe, without retrying close.
  let missedCloses = 0;
  const missed = {loadRuntime:async()=>({createAgentRegistry:()=>({}),createSharedAcpRuntime:()=>({
    getStatus:async()=>({acpxRecordId:target.record,backendSessionId:target.acp,
      lastRequestId:'fixture-task:4',details:{cwd:target.cwd,closed:false}}),
    findSession:async()=>undefined,close:async()=>{missedCloses++;},shutdown:async()=>{}
  })})};
  await assert.rejects(closeTarget(target,missed), error => failureReason(error)==='name_scope_mismatch'
    && error.name_lookup_found === false && error.record_id_match === true);
  await assert.rejects(probeTarget(target,missed), error => failureReason(error)==='record_binding_mismatch'
    && error.closed_observed === false && error.closed_expected === true
    && error.record_id_match === true && error.backend_session_match === true
    && error.cwd_match === true && error.request_match === true);
  assert.equal(missedCloses,0);
  for (const name of ['SessionNotFoundError','Error']) {
    let closes = 0;
    const deps = {loadRuntime:async()=>({createAgentRegistry:()=>({}),createSharedAcpRuntime:()=>({
      getStatus:async()=>{throw Object.assign(Error('PRIVATE_CANARY'),{name});},
      close:async()=>{closes++;},shutdown:async()=>{}
    })})};
    await assert.rejects(probeTarget(target,deps), error => {
      assert.equal(failureReason(error),name === 'SessionNotFoundError' ? 'record_not_found' : 'backend_unavailable');
      assert.equal(error.record_lookup_found,name === 'SessionNotFoundError' ? false : undefined);
      assert.equal(error.record_id_match,undefined);
      assert.equal(error.closed_observed,undefined);
      return true;
    });
    assert.equal(closes,0);
  }
  for (const status of [undefined, {acpxRecordId:'PRIVATE_CANARY',backendSessionId:'PRIVATE_CANARY',
      details:{cwd:'/PRIVATE_CANARY',closed:false}},
    {acpxRecordId:target.record,backendSessionId:target.acp,lastRequestId:'fixture-task:4',
      details:{cwd:target.cwd,closed:'PRIVATE_CANARY'}}]) {
    const deps = {loadRuntime:async()=>({createAgentRegistry:()=>({}),createSharedAcpRuntime:()=>({
      getStatus:async()=>status,findSession:async()=>undefined,close:async()=>{throw Error('MUST_NOT_CLOSE');},shutdown:async()=>{}
    })})};
    await assert.rejects(probeTarget(target,deps), error => {
      assert.equal(failureReason(error),'record_binding_mismatch');
      assert.equal(error.record_lookup_found,status !== undefined);
      if (status?.acpxRecordId !== target.record) {
        assert.equal(error.record_id_match,status === undefined ? undefined : false);
        assert.equal(error.backend_session_match,undefined);
        assert.equal(error.closed_observed,undefined);
        assert.equal(error.cwd_match,undefined);
      } else assert.equal(error.closed_observed,undefined);
      assert.doesNotMatch(JSON.stringify(error),/PRIVATE_CANARY/);
      return true;
    });
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
  for (const [error, reason] of [
    [Error('ACPX_VERSION_MISMATCH'), 'acpx_version_mismatch'],
    [Error('SHARED_RUNTIME_UNAVAILABLE'), 'shared_runtime_unavailable'],
    [{code:'MODULE_NOT_FOUND',message:"Cannot find module 'acpx/package.json'"}, 'acpx_package_not_found'],
    [{code:'ERR_PACKAGE_PATH_NOT_EXPORTED'}, 'runtime_export_unavailable'],
    [{syscall:'spawnSync npm',code:'ENOENT'}, 'npm_executable_not_found'],
    [{syscall:'spawnSync npm',code:'ETIMEDOUT'}, 'npm_root_timeout'],
    [{message:'PRIVATE_SENTINEL',stack:'PRIVATE_SENTINEL'}, 'backend_unavailable']
  ]) assert.equal(failureReason(error), reason);

  // Multi-scope lookup finds native-skip sessions created with full agent command flags.
  let closedSkip = false, skipCloses = 0, resolvedAgents = [];
  const skipTarget = {...target, agent: 'omp'};
  const skipDeps = {loadRuntime: async () => ({
    createAgentRegistry: () => ({resolve: name => `resolved:${name}`, list: () => ['omp']}),
    createSharedAcpRuntime: ({agentRegistry}) => {
      assert.equal(agentRegistry.resolve('omp acp --auto-approve --allow-home'), 'omp acp --auto-approve --allow-home');
      assert.equal(agentRegistry.resolve('omp'), 'resolved:omp');
      return {
        getStatus: async () => ({acpxRecordId: skipTarget.record, backendSessionId: skipTarget.acp,
          lastRequestId: 'fixture-task:4', details: {cwd: skipTarget.cwd, closed: closedSkip}}),
        findSession: async ({agent}) => {
          resolvedAgents.push(agent);
          return (!closedSkip && agent === 'omp acp --auto-approve --allow-home')
            ? {acpxRecordId: skipTarget.record, backendSessionId: skipTarget.acp, cwd: skipTarget.cwd, sessionKey: skipTarget.session}
            : undefined;
        },
        close: async () => {closedSkip = true; skipCloses++;},
        shutdown: async () => {}
      };
    }
  })};
  const skipRes = await closeTarget(skipTarget, skipDeps);
  assert.equal(skipRes.verified, true);
  assert.equal(skipCloses, 1);
  assert.ok(resolvedAgents.includes('omp acp --auto-approve --allow-home'));

  // probeTarget checks all candidate scopes including AGY skip-permissions.
  const agyReopened = {loadRuntime: async () => ({
    createAgentRegistry: () => ({resolve: name => `resolved:${name}`}),
    createSharedAcpRuntime: () => ({
      getStatus: async () => ({acpxRecordId: target.record, backendSessionId: target.acp,
        lastRequestId: 'fixture-task:4', details: {cwd: target.cwd, closed: true}}),
      findSession: async ({agent}) => agent === 'agy acp --dangerously-skip-permissions'
        ? {acpxRecordId: 'reopened'} : undefined,
      close: async () => {throw Error('PROBE_MUST_NOT_CLOSE');},
      shutdown: async () => {}
    })
  })};
  await assert.rejects(probeTarget({...target, agent: 'agy'}, agyReopened),
    error => failureReason(error) === 'name_scope_replaced_or_reopened');

  // Configured Windows-style executable path argv scope is discovered and passed cleanly on win32.
  let winClosed = false, winResolved = [];
  const winDeps = {
    platform: 'win32',
    resolveConfiguredAgentArgv: async () => ['C:\\Users\\example\\AppData\\Local\\omp\\omp.exe', 'acp', '--auto-approve', '--allow-home'],
    loadRuntime: async () => ({
      createAgentRegistry: ({overrides}) => {
        assert.deepEqual(overrides.omp, ['omp', 'acp']);
        return {resolve: name => `resolved:${name}`};
      },
      createSharedAcpRuntime: ({agentRegistry}) => {
        const customScope = '"C:\\\\Users\\\\example\\\\AppData\\\\Local\\\\omp\\\\omp.exe" acp --auto-approve --allow-home';
        assert.deepEqual(agentRegistry.resolve(customScope), ['C:\\Users\\example\\AppData\\Local\\omp\\omp.exe', 'acp', '--auto-approve', '--allow-home']);
        assert.deepEqual(agentRegistry.resolve('omp acp --auto-approve --allow-home'), ['omp', 'acp', '--auto-approve', '--allow-home']);
        return {
          getStatus: async () => ({acpxRecordId: target.record, backendSessionId: target.acp,
            lastRequestId: 'fixture-task:4', details: {cwd: target.cwd, closed: winClosed}}),
          findSession: async ({agent}) => {
            winResolved.push(agent);
            return (!winClosed && agent === customScope)
              ? {acpxRecordId: target.record, backendSessionId: target.acp, cwd: target.cwd, sessionKey: target.session}
              : undefined;
          },
          close: async () => {winClosed = true;},
          shutdown: async () => {}
        };
      }
    })
  };
  assert.equal((await closeTarget({...target, agent: 'omp'}, winDeps)).verified, true);
  assert.ok(winResolved.some(a => a.includes('omp.exe')));

  console.log('STRICT_NATIVE_RELEASE_PROOF_OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
