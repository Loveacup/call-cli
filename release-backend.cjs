'use strict';
// Public Shared Runtime only. Never load authentication or submit/resume a turn.
const {loadRuntime} = require('./shared-prompt.cjs');

async function closeTarget(target, deps = {}) {
  const sdk = await (deps.loadRuntime ?? loadRuntime)('0.22.0');
  // Shared Runtime does not load CLI agents configuration. Match the documented
  // OMP entry without reading auth/config or weakening the exact scoped lookup.
  const agentRegistry = sdk.createAgentRegistry({overrides: {omp: 'omp acp'}});
  const runtime = sdk.createSharedAcpRuntime({cwd: target.cwd, agentRegistry,
    permissionMode: 'approve-reads', nonInteractivePermissions: 'deny',
    authCredentials: {}, authPolicy: 'skip', timeoutMs: 15000});
  const handle = {backend: 'acpx-shared', acpxRecordId: target.record,
    runtimeSessionName: target.record, sessionKey: target.session, cwd: target.cwd};
  function exact(status, closed) {
    if (status?.acpxRecordId !== target.record || status.backendSessionId !== target.acp ||
        status.details?.cwd !== target.cwd || status.details.closed !== closed ||
        status.lastRequestId !== (target.request_id ?? `${target.task}:${target.turn}`)) throw Error('RECORD_BINDING_MISMATCH');
  }
  let stage = 'resolve';
  try {
    const before = await runtime.getStatus({handle});
    exact(before, false);
    const selected = await runtime.findSession({sessionKey: target.session, agent: target.agent, cwd: target.cwd});
    if (selected?.acpxRecordId !== target.record || selected.backendSessionId !== target.acp
        || selected.cwd !== target.cwd || selected.sessionKey !== target.session) throw Error('NAME_SCOPE_MISMATCH');
    stage = 'close';
    await runtime.close({handle, reason: 'operator-release-only'});
    stage = 'verify';
    const after = await runtime.getStatus({handle});
    exact(after, true);
    const active = await runtime.findSession({sessionKey: target.session, agent: target.agent, cwd: target.cwd});
    if (active !== undefined) throw Error('NAME_SCOPE_REPLACED_OR_REOPENED');
    exact(await runtime.getStatus({handle}), true);
    const binding = {acpxRecordId: target.record, acpxSessionId: target.acp,
      session: target.session, agent: target.agent, cwd: target.cwd};
    return {verified: true,
      close: {action: 'session_closed', ...binding,
        source: 'shared-runtime-resolved-and-exact-record-readback'},
      status: {action: 'status_snapshot', status: 'no-session', binding,
        source: 'shared-runtime-findSession-undefined'},
      execution: {source: 'supported-record-close-and-session-turn-lock',
        scope: 'acpx-managed-execution', arbitraryDetachedDescendantsProven: false}};
  } catch (error) {
    error.stage = stage;
    throw error;
  } finally {await runtime.shutdown();}
}
async function probeTarget(target, deps = {}, resolve = false) {
  const sdk = await (deps.loadRuntime ?? loadRuntime)('0.22.0');
  const agentRegistry = sdk.createAgentRegistry({overrides:{omp:'omp acp'}});
  const runtime = sdk.createSharedAcpRuntime({cwd:target.cwd,agentRegistry,
    permissionMode:'approve-reads',nonInteractivePermissions:'deny',
    authCredentials:{},authPolicy:'skip',timeoutMs:15000});
  const handle = {backend:'acpx-shared',acpxRecordId:target.record,
    runtimeSessionName:target.record,sessionKey:target.session,cwd:target.cwd};
  function exact(status) {
    if (status?.acpxRecordId !== target.record || status.backendSessionId !== target.acp ||
        status.details?.cwd !== target.cwd || status.details.closed !== true ||
        status.lastRequestId !== (target.request_id ?? `${target.task}:${target.turn}`)) throw Error('RECORD_BINDING_MISMATCH');
  }
  try {
    if (resolve) {
      const status = await runtime.getStatus({handle});
      if (status?.acpxRecordId !== target.record || status.details?.cwd !== target.cwd ||
          status.lastRequestId !== (target.request_id ?? `${target.task}:${target.turn}`) ||
          typeof status.backendSessionId !== 'string' || !status.backendSessionId) throw Error('RECORD_BINDING_MISMATCH');
      return {acp:status.backendSessionId};
    }
    exact(await runtime.getStatus({handle}));
    const selected = await runtime.findSession({sessionKey:target.session,agent:target.agent,cwd:target.cwd});
    if (selected !== undefined) throw Error('NAME_SCOPE_REPLACED_OR_REOPENED');
    exact(await runtime.getStatus({handle}));
    return {verified:true,binding:{acpxRecordId:target.record,acpxSessionId:target.acp,
      session:target.session,agent:target.agent,cwd:target.cwd},
      record:{closed:true,lastRequestId:(target.request_id ?? `${target.task}:${target.turn}`)},status:'no-session',
      source:'shared-runtime-exact-record-probe',arbitraryDetachedDescendantsProven:false};
  } catch (error) {
    error.stage = resolve ? 'resolve' : 'probe';
    throw error;
  } finally {await runtime.shutdown();}
}
module.exports = {closeTarget, probeTarget};
if (require.main === module) {
  const fs = require('node:fs');
  const operation = process.argv[2] ?? 'close';
  const handler = operation === 'probe' ? probeTarget : operation === 'resolve' ? (t)=>probeTarget(t,{},true)
    : operation === 'close' ? closeTarget : null;
  (handler ? handler(JSON.parse(fs.readFileSync(0, 'utf8'))) : Promise.reject(Error('INVALID_OPERATION'))).then(value => {
    process.stdout.write(JSON.stringify(value) + '\n');
  }).catch(error => {
    const reason = ['RECORD_BINDING_MISMATCH','NAME_SCOPE_MISMATCH','NAME_SCOPE_REPLACED_OR_REOPENED'].includes(error.message)
      ? error.message.toLowerCase() : 'backend_unavailable';
    const stage = ['resolve','close','verify','probe'].includes(error.stage) ? error.stage : 'resolve';
    process.stdout.write(JSON.stringify({action:'release_unknown',stage,reason_code:reason})+'\n');
    process.exitCode = 1;
  });
}
