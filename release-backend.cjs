'use strict';
// Public Shared Runtime only. Never load authentication or submit/resume a turn.
const {loadRuntime} = require('./shared-prompt.cjs');

const MATCH_FIELDS = ['record_lookup_found','record_id_match','backend_session_match','cwd_match',
  'request_match','closed_expected','closed_observed','name_lookup_found','name_scope_match'];
function matchDiagnostic(value) {
  return Object.fromEntries(MATCH_FIELDS.filter(key => typeof value?.[key] === 'boolean').map(key => [key,value[key]]));
}
function observeStatus(context, status, target, closed) {
  for (const key of ['record_id_match','backend_session_match','cwd_match','request_match','closed_expected','closed_observed']) delete context[key];
  context.record_lookup_found = status !== undefined && status !== null;
  if (!context.record_lookup_found) return;
  context.record_id_match = status?.acpxRecordId === target.record;
  // A fallback-resolved foreign record cannot describe the bound target's fields.
  if (!context.record_id_match) return;
  if (typeof target.acp === 'string') context.backend_session_match = status.backendSessionId === target.acp;
  context.cwd_match = status.details?.cwd === target.cwd;
  context.request_match = status.lastRequestId === (target.request_id ?? `${target.task}:${target.turn}`);
  if (typeof closed === 'boolean') context.closed_expected = closed;
  if (typeof status.details?.closed === 'boolean') context.closed_observed = status.details.closed;
}
function observeLookup(context, selected, target) {
  context.name_lookup_found = selected !== undefined && selected !== null;
  if (context.name_lookup_found) context.name_scope_match = selected.acpxRecordId === target.record &&
    selected.backendSessionId === target.acp && selected.cwd === target.cwd && selected.sessionKey === target.session;
  else delete context.name_scope_match;
}
async function getStatus(runtime, handle, context) {
  for (const key of MATCH_FIELDS.filter(key => !key.startsWith('name_'))) delete context[key];
  try {return await runtime.getStatus({handle});}
  catch (error) {
    if (error?.name === 'SessionNotFoundError') context.record_lookup_found = false;
    throw error;
  }
}
async function withRuntime(target, deps, operation, body) {
  let runtime, primary;
  const context = {operation, stage:'load', substep:'load_runtime'};
  try {
    const sdk = await (deps.loadRuntime ?? loadRuntime)('0.22.0');
    context.stage='registry'; context.substep='create_registry';
    const agentRegistry=sdk.createAgentRegistry({overrides:{omp:'omp acp'}});
    context.stage='create'; context.substep='create_runtime';
    runtime=sdk.createSharedAcpRuntime({cwd:target.cwd,agentRegistry,
      permissionMode:'approve-reads',nonInteractivePermissions:'deny',authCredentials:{},authPolicy:'skip',timeoutMs:15000});
    return await body(runtime, context);
  } catch (error) {
    primary=error instanceof Error ? error : Error('BACKEND_FAILURE');
    Object.assign(primary,context);
    throw primary;
  } finally {
    if (runtime) {
      try {await runtime.shutdown();}
      catch (error) {
        const failure={name:error instanceof Error ? error.name : 'Error'};
        if(primary) primary.shutdownFailure=failure;
        else throw Object.assign(Error('SHUTDOWN_FAILED'),{operation,stage:'shutdown',substep:'shutdown',name:failure.name});
      }
    }
  }
}
async function closeTarget(target, deps = {}) {
  return withRuntime(target,deps,'close',async(runtime,context)=>{
  const handle = {backend: 'acpx-shared', acpxRecordId: target.record,
    runtimeSessionName: target.record, sessionKey: target.session, cwd: target.cwd};
  function exact(status, closed) {
    observeStatus(context, status, target, closed);
    if (status?.acpxRecordId !== target.record || status.backendSessionId !== target.acp ||
        status.details?.cwd !== target.cwd || status.details.closed !== closed ||
        status.lastRequestId !== (target.request_id ?? `${target.task}:${target.turn}`)) throw Error('RECORD_BINDING_MISMATCH');
  }
  context.stage='resolve'; context.substep='status_before';
    const before = await getStatus(runtime, handle, context);
    exact(before, false);
    context.substep='find_before';
    const selected = await runtime.findSession({sessionKey: target.session, agent: target.agent, cwd: target.cwd});
    observeLookup(context, selected, target);
    if (selected?.acpxRecordId !== target.record || selected.backendSessionId !== target.acp
        || selected.cwd !== target.cwd || selected.sessionKey !== target.session) throw Error('NAME_SCOPE_MISMATCH');
    context.stage='close'; context.substep='close';
    await runtime.close({handle, reason: 'operator-release-only'});
    context.stage='verify'; context.substep='status_after';
    const after = await getStatus(runtime, handle, context);
    exact(after, true);
    context.substep='find_after';
    const active = await runtime.findSession({sessionKey: target.session, agent: target.agent, cwd: target.cwd});
    observeLookup(context, active, target);
    if (active !== undefined) throw Error('NAME_SCOPE_REPLACED_OR_REOPENED');
    context.substep='status_confirm';
    exact(await getStatus(runtime, handle, context), true);
    const binding = {acpxRecordId: target.record, acpxSessionId: target.acp,
      session: target.session, agent: target.agent, cwd: target.cwd};
    return {verified: true,
      close: {action: 'session_closed', ...binding,
        source: 'shared-runtime-resolved-and-exact-record-readback'},
      status: {action: 'status_snapshot', status: 'no-session', binding,
        source: 'shared-runtime-findSession-undefined'},
      execution: {source: 'supported-record-close-and-session-turn-lock',
        scope: 'acpx-managed-execution', arbitraryDetachedDescendantsProven: false}};
  });
}
async function probeTarget(target, deps = {}, resolve = false) {
  return withRuntime(target,deps,resolve ? 'resolve' : 'probe',async(runtime,context)=>{
  const handle = {backend:'acpx-shared',acpxRecordId:target.record,
    runtimeSessionName:target.record,sessionKey:target.session,cwd:target.cwd};
  function exact(status) {
    observeStatus(context, status, target, true);
    if (status?.acpxRecordId !== target.record || status.backendSessionId !== target.acp ||
        status.details?.cwd !== target.cwd || status.details.closed !== true ||
        status.lastRequestId !== (target.request_id ?? `${target.task}:${target.turn}`)) throw Error('RECORD_BINDING_MISMATCH');
  }
  context.stage=resolve ? 'resolve' : 'probe'; context.substep='status';
    if (resolve) {
      const status = await getStatus(runtime, handle, context);
      observeStatus(context, status, target);
      if (status?.acpxRecordId !== target.record || status.details?.cwd !== target.cwd ||
          status.lastRequestId !== (target.request_id ?? `${target.task}:${target.turn}`) ||
          typeof status.backendSessionId !== 'string' || !status.backendSessionId) throw Error('RECORD_BINDING_MISMATCH');
      return {acp:status.backendSessionId};
    }
    exact(await getStatus(runtime, handle, context));
    context.substep='find_session';
    const selected = await runtime.findSession({sessionKey:target.session,agent:target.agent,cwd:target.cwd});
    observeLookup(context, selected, target);
    if (selected !== undefined) throw Error('NAME_SCOPE_REPLACED_OR_REOPENED');
    context.substep='status_confirm';
    exact(await getStatus(runtime, handle, context));
    return {verified:true,binding:{acpxRecordId:target.record,acpxSessionId:target.acp,
      session:target.session,agent:target.agent,cwd:target.cwd},
      record:{closed:true,lastRequestId:(target.request_id ?? `${target.task}:${target.turn}`)},status:'no-session',
      source:'shared-runtime-exact-record-probe',arbitraryDetachedDescendantsProven:false};
  });
}
// Strict diagnostic vocabulary; raw messages, stacks and stderr remain private.
function failureReason(error) {
  if (error?.name === 'SessionNotFoundError' && error.record_lookup_found === false) return 'record_not_found';
  if (['RECORD_BINDING_MISMATCH','NAME_SCOPE_MISMATCH','NAME_SCOPE_REPLACED_OR_REOPENED',
       'ACPX_VERSION_MISMATCH','SHARED_RUNTIME_UNAVAILABLE'].includes(error?.message))
    return error.message.toLowerCase();
  if (error?.code === 'MODULE_NOT_FOUND' &&
      error?.message?.startsWith("Cannot find module 'acpx/package.json'")) return 'acpx_package_not_found';
  if (error?.code === 'ERR_PACKAGE_PATH_NOT_EXPORTED') return 'runtime_export_unavailable';
  if (error?.syscall === 'spawnSync npm' && error?.code === 'ENOENT') return 'npm_executable_not_found';
  if (error?.syscall === 'spawnSync npm' && error?.code === 'ETIMEDOUT') return 'npm_root_timeout';
  return 'backend_unavailable';
}
module.exports = {closeTarget, probeTarget, failureReason};
if (require.main === module) {
  const fs = require('node:fs');
  const operation = process.argv[2] ?? 'close';
  const handler = operation === 'probe' ? probeTarget : operation === 'resolve' ? (t)=>probeTarget(t,{},true)
    : operation === 'close' ? closeTarget : null;
  (handler ? handler(JSON.parse(fs.readFileSync(0, 'utf8'))) : Promise.reject(Error('INVALID_OPERATION'))).then(value => {
    process.stdout.write(JSON.stringify(value) + '\n');
  }).catch(error => {
    const reason = failureReason(error);
    const stage = ['load','registry','create','resolve','close','verify','probe','shutdown'].includes(error.stage) ? error.stage : 'resolve';
    const safe=value=>typeof value==='string' && /^[A-Za-z0-9_]{1,80}$/.test(value) ? value : undefined;
    process.stdout.write(JSON.stringify({action:'release_unknown',operation,stage,reason_code:reason,
      substep:safe(error.substep),error_name:safe(error.name),error_code:safe(error.code),
      shutdown_failure:error.shutdownFailure ? {error_name:safe(error.shutdownFailure.name)} : undefined,
      ...matchDiagnostic(error)})+'\n');
    process.exitCode = 1;
  });
}
