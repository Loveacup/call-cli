"use strict";
// A short-lived client of ACPX's public Shared Runtime; never a direct ACP owner.
const fs = require('node:fs');
const path = require('node:path');
const cp = require('node:child_process');
const { pathToFileURL } = require('node:url');

async function loadRuntime(expectedVersion) {
  const win = process.platform === 'win32';
  const root = cp.execFileSync(win ? 'cmd.exe' : 'npm', win ? ['/d', '/s', '/c', 'npm root -g'] : ['root', '-g'],
    {encoding: 'utf8', timeout: 15000, stdio: ['ignore', 'pipe', 'pipe']}).trim();
  const paths = [path.dirname(root)];
  const packageFile = require.resolve('acpx/package.json', {paths});
  const pkg = JSON.parse(fs.readFileSync(packageFile, 'utf8'));
  if (pkg.name !== 'acpx' || (expectedVersion && pkg.version !== expectedVersion)) throw Error('ACPX_VERSION_MISMATCH');
  const sdk = await import(pathToFileURL(require.resolve('acpx/runtime', {paths})).href);
  if (typeof sdk.createSharedAcpRuntime !== 'function') throw Error('SHARED_RUNTIME_UNAVAILABLE');
  return sdk;
}

async function readAcpxConfigs(cwd, home = require('node:os').homedir()) {
  const configs = [];
  for (const file of [path.join(home, '.acpx', 'config.json'), path.join(cwd, '.acpxrc.json')]) {
    let config = {};
    try {config = JSON.parse(await fs.promises.readFile(file, 'utf8'));} catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
    if (!config || typeof config !== 'object' || Array.isArray(config)) throw Error('INVALID_AUTH_CONFIG');
    configs.push(config);
  }
  return configs;
}

async function loadAuth(cwd, home = require('node:os').homedir()) {
  // Match ACPX CLI precedence on this device; never send credentials via the controller.
  const configs = await readAcpxConfigs(cwd, home);
  const authCredentials = Object.create(null);
  for (const config of configs) {
    if (config.auth == null) continue;
    if (typeof config.auth !== 'object' || Array.isArray(config.auth)) throw Error('INVALID_AUTH_CONFIG');
    for (const [method, credential] of Object.entries(config.auth)) {
      if (typeof credential !== 'string' || !credential.trim()) throw Error('INVALID_AUTH_CONFIG');
      authCredentials[method] = credential;
    }
  }
  const authPolicy = configs[1].authPolicy ?? configs[0].authPolicy ?? 'skip';
  if (!['skip', 'fail'].includes(authPolicy)) throw Error('INVALID_AUTH_POLICY');
  return {authCredentials, authPolicy};
}

function splitCommandLine(value) {
  const parts = [];
  let current = '', quote = null, escaping = false, hasPart = false;
  for (const ch of String(value)) {
    if (escaping) {current += ch; escaping = false; hasPart = true;}
    else if (ch === '\\' && quote !== "'") {escaping = true;}
    else if (quote) {if (ch === quote) quote = null; else current += ch; hasPart = true;}
    else if (ch === "'" || ch === '"') {quote = ch; hasPart = true;}
    else if (/\s/.test(ch)) {if (hasPart) {parts.push(current); current = ''; hasPart = false;}}
    else {current += ch; hasPart = true;}
  }
  if (escaping) {current += '\\'; hasPart = true;}
  if (quote) throw Error('INVALID_AGENT_COMMAND');
  if (hasPart) parts.push(current);
  return parts;
}

function renderArgvIdentity(argv) {
  const safe = /^[A-Za-z0-9_@%+=:,./^~-]+$/u;
  return argv.map(arg => safe.test(arg) ? arg : JSON.stringify(arg)).join(' ');
}

async function resolveConfiguredAgentArgv(agent, requestedArgv, cwd, home = require('node:os').homedir()) {
  const configs = await readAcpxConfigs(cwd, home);
  let entry;
  for (const config of configs) {
    if (config.agents == null) continue;
    if (typeof config.agents !== 'object' || Array.isArray(config.agents)) throw Error('INVALID_AGENT_CONFIG');
    if (Object.hasOwn(config.agents, agent)) entry = config.agents[agent];
  }
  let base;
  if (Array.isArray(entry?.argv) && entry.argv.length > 0 && entry.argv.every(x => typeof x === 'string' && x.length > 0)) {
    base = [...entry.argv];
  } else if (typeof entry?.command === 'string' && entry.command.trim()) {
    base = splitCommandLine(entry.command);
  } else {
    return [...requestedArgv];
  }
  let i = 1;
  while (i < base.length && i < requestedArgv.length && base[i] === requestedArgv[i]) i++;
  return [...base, ...requestedArgv.slice(i)];
}

function publicEvent(event) {
  if (event?.type === 'text_delta' && event.stream === 'output' && event.tag === 'agent_message_chunk' && typeof event.text === 'string') {
    return {type: event.type, stream: 'output', tag: event.tag, text: event.text};
  }
  if (event?.type === 'tool_call') {
    const identifier = value => typeof value === 'string' && /^[A-Za-z_][A-Za-z0-9_.:-]{0,79}$/.test(value) ? value : '';
    const toolId = typeof event.toolCallId === 'string' && /^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,159}$/.test(event.toolCallId) ? event.toolCallId : '';
    const projected = {type: 'tool_call', toolCallId: toolId,
      status: ['pending', 'in_progress', 'completed', 'failed'].includes(event.status) ? event.status : ''};
    if (identifier(event.title)) projected.title = event.title;
    if (['read', 'edit', 'delete', 'move', 'search', 'execute', 'think', 'fetch', 'switch_mode', 'other'].includes(event.kind)) projected.kind = event.kind;
    return projected;
  }
}

async function watchedAuth(runtime, handle, requestId) {
  if (typeof runtime.watchSession !== 'function') return false;
  const abort = new AbortController();
  const expired = Symbol('watch deadline');
  let timer, iterator, pending;
  const deadline = new Promise(resolve => {
    timer = setTimeout(() => {resolve(expired); abort.abort();}, 1000);
  });
  let active = false, promptId, prompts = 0, responses = 0, ambiguous = false, auth = false, total = 0, matched = 0;
  try {
    // Passive public replay only. Never export messages or infer from error text.
    iterator = runtime.watchSession({handle, signal:abort.signal})[Symbol.asyncIterator]();
    for (;;) {
      pending = Promise.resolve(iterator.next());
      const next = await Promise.race([pending, deadline]);
      if (next === expired) return false;
      pending = undefined;
      if (next.done) return false;
      const event = next.value;
      if (abort.signal.aborted || ++total > 10000) return false;
      if (event.requestId !== requestId) continue;
      if (++matched > 1000) return false;
      if (event.type === 'turn_started') {
        if (active) return false;
        active = true;
      } else if (active && event.type === 'turn_result') {
        return event.result?.status === 'failed' && prompts === 1 && responses === 1 && !ambiguous && auth;
      } else if (active && event.type === 'message') {
        const message = event.message;
        if (message?.method === 'session/prompt') {
          prompts++;
          promptId = message.id;
          if (!(typeof promptId === 'string' || typeof promptId === 'number')) ambiguous = true;
        } else if (prompts && message?.id === promptId) {
          // The journal omits direction; a callback ID collision is not proof of a prompt error.
          if (message.method) ambiguous = true;
          else if ('error' in message || 'result' in message) {
            responses++;
            auth = message.error?.code === -32000 && message.error?.data?.authRequired === true;
          }
        }
      }
    }
  } catch {
    // Missing/expired journals and unsupported watches leave the cause unknown.
  } finally {
    clearTimeout(timer);
    abort.abort();
    // Async-generator return queues behind next; neither may hold the failure frame.
    const close = () => {
      try {Promise.resolve(iterator?.return?.()).catch(() => {});} catch {}
    };
    Promise.resolve(pending).then(close, close);
  }
  return false;
}

function sendFrame(frame) {
  return new Promise((resolve, reject) => process.stdout.write(JSON.stringify(frame) + '\n', error => error ? reject(error) : resolve()));
}

async function run(options, prompt, deps = {}) {
  const send = deps.send ?? sendFrame;
  let runtime, terminal, code = 1;
  const requestId = options?.requestId;
  let stage = 'input_validate';
  try {
    if (!options || typeof requestId !== 'string' || !requestId || typeof prompt !== 'string'
        || typeof options.write !== 'boolean' || !path.isAbsolute(options.cwd)
        || !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(options.sessionId)
        || typeof options.sessionName !== 'string' || !options.sessionName
        || (options.timeoutMs !== undefined && (!Number.isFinite(options.timeoutMs) || options.timeoutMs <= 0))) throw Error('INVALID_INPUT');
    const ALLOWED_AGENT_ENV = {INITIAL_AGENT_MODE: new Set(['agent-full-access'])};
    const agentEnv = {};
    if (options.agentEnv !== undefined) {
      if (!options.agentEnv || typeof options.agentEnv !== 'object' || Array.isArray(options.agentEnv)) throw Error('INVALID_AGENT_ENV');
      for (const [k, v] of Object.entries(options.agentEnv)) {
        if (!Object.prototype.hasOwnProperty.call(ALLOWED_AGENT_ENV, k) || typeof v !== 'string' || !ALLOWED_AGENT_ENV[k].has(v)) throw Error('INVALID_AGENT_ENV');
        agentEnv[k] = v;
      }
    }
    const permissionMode = options.permissionMode ?? (options.write ? 'approve-all' : 'approve-reads');
    if (!['approve-all', 'approve-reads'].includes(permissionMode)) throw Error('INVALID_PERMISSION_MODE');
    const timeout = options.timeoutMs === undefined ? {} : {timeoutMs:options.timeoutMs};
    stage = 'runtime_load';
    const sdk = await (deps.loadRuntime ?? loadRuntime)(options.expectedVersion);
    stage = 'auth_load';
    const auth = await (deps.loadAuth ?? loadAuth)(options.cwd);
    stage = 'runtime_create';
    var savedEnv = {};
    for (const [k, v] of Object.entries(agentEnv)) {
      savedEnv[k] = Object.prototype.hasOwnProperty.call(process.env, k) ? process.env[k] : undefined;
      process.env[k] = v;
    }
    runtime = sdk.createSharedAcpRuntime({cwd: options.cwd,
      permissionMode,
      nonInteractivePermissions: 'deny', ...timeout,
      authCredentials: auth.authCredentials, authPolicy: auth.authPolicy});
    // This serializable public handle is restored from the coordinator's native
    // session-created record, not from a model-supplied provider session ID.
    const handle = {backend: 'acpx-shared', acpxRecordId: options.sessionId,
      sessionKey: options.sessionName, runtimeSessionName: options.sessionId, cwd: options.cwd};
    // Selection is applied and read back by the runner before this transport.
    // Shared control methods require an already running owner and cannot bootstrap it.
    stage = 'prompt_submit';
    const turn = runtime.startTurn({handle, text: prompt, mode: 'prompt', requestId,
      ...timeout});
    let promptNotice, noticeFailed = false;
    if (turn.promptStarted) {
      promptNotice = turn.promptStarted.then(() =>
        send({type:'callcli.shared.event', requestId, event:{type:'prompt_started'}}), () => {});
      promptNotice.catch(() => {noticeFailed = true;});
    }
    stage = 'prompt_events';
    for await (const event of turn.events) {
      const projected = publicEvent(event);
      if (projected) await send({type: 'callcli.shared.event', requestId, event: projected});
    }
    stage = 'prompt_result';
    const result = await turn.result;
    await promptNotice;
    if (noticeFailed) throw Error('OUTPUT_FAILED');
    const valid = result && !result.error &&
      ((result.status === 'completed' && result.stopReason === 'end_turn') ||
       (result.status === 'cancelled' && result.stopReason === 'cancelled'));
    let diagnostic = {};
    if (result?.status === 'failed') {
      if (result.error?.detailCode === 'AUTH_REQUIRED') diagnostic = {errorCategory:'auth_required'};
      else if (await watchedAuth(runtime, handle, requestId)) {
        diagnostic = {errorCategory:'auth_required', errorSource:'session_watch'};
      }
      else if (result.error?.code === 'TIMEOUT') {
        diagnostic = {errorCategory:'timeout', errorSource:'turn_result_code'};
      } else if (result.error?.detailCode === 'AGENT_DISCONNECTED') {
        diagnostic = {errorCategory:'connection_lost', errorSource:'turn_result_detail'};
      }
    }
    if (!valid) diagnostic = {stage:'prompt_result', reasonCode:result?.status === 'failed' ? 'turn_failed' : 'invalid_result', ...diagnostic};
    terminal = {type: 'callcli.shared.result', requestId,
      status: valid ? result.status : 'failed', stopReason: valid ? result.stopReason : null, ...diagnostic};
    code = valid ? 0 : 1;
  } catch {
    // Never serialize exceptions: they can include private prompts/configuration.
    terminal = {type: 'callcli.shared.result', requestId: requestId ?? null, status: 'failed', stopReason: null,
      stage, reasonCode:stage+'_failed'};
  } finally {
    for (const [k, v] of Object.entries(savedEnv ?? {})) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
    try {await send(terminal);} catch {code = 1;}
    // shutdown detaches this client; it is NOT cancellation of accepted work.
    if (runtime) {try {await runtime.shutdown();} catch {code = 1;}}
  }
  return code;
}

function isAbsoluteTargetPath(value) {
  return typeof value === 'string' && (path.isAbsolute(value) || /^[A-Za-z]:[\\/]/.test(value));
}

function sameTargetPath(a, b, platform) {
  if (typeof a !== 'string' || typeof b !== 'string') return false;
  const paths = platform === 'win32' ? path.win32 : path;
  const x = paths.resolve(a), y = paths.resolve(b);
  return platform === 'win32' ? x.toLowerCase() === y.toLowerCase() : x === y;
}

function sessionRunState(status, record) {
  if (typeof status?.details?.status === 'string') return status.details.status === 'running' ? 'alive' : status.details.status;
  if (typeof record?.pid === 'number' && record.pid > 0) {
    try {
      process.kill(record.pid, 0);
      return 'alive';
    } catch (error) {
      return error?.code === 'EPERM' ? 'alive' : 'dead';
    }
  }
  if (record?.lastAgentExitSignal || (record?.lastAgentExitCode ?? 0) !== 0) return 'dead';
  return 'idle';
}

async function controlStatus(record, options) {
  const win = process.platform === 'win32';
  const root = cp.execFileSync(win ? 'cmd.exe' : 'npm', win ? ['/d', '/s', '/c', 'npm root -g'] : ['root', '-g'],
    {encoding:'utf8', timeout:15000, stdio:['ignore','pipe','pipe']}).trim();
  const cli = require.resolve('acpx', {paths:[path.dirname(root)]});
  const result = cp.execFileSync(process.execPath, [cli, '--cwd', record.cwd, '--format', 'json',
    '--agent', record.agentCommand, 'status', '-s', record.name],
    {encoding:'utf8', timeout:options.timeoutMs ?? 20000, stdio:['ignore','pipe','pipe']});
  return JSON.parse(result);
}

async function control(options, deps = {}) {
  const send = deps.send ?? sendFrame;
  let shared, embedded, ok = false;
  try {
    const validActions = new Set(['new', 'show', 'status', 'close', 'cancel', 'model', 'effort', 'mode']);
    if (!options || !validActions.has(options.action) || !isAbsoluteTargetPath(options.cwd)
        || typeof options.session !== 'string' || !options.session.trim()
        || typeof options.agent !== 'string' || !options.agent.trim()
        || !Array.isArray(options.agentArgv) || options.agentArgv.length === 0
        || !options.agentArgv.every(s => typeof s === 'string' && s.length > 0)
        || !['approve-all', 'approve-reads'].includes(options.permissionMode)
        || (options.recordId !== undefined && !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(options.recordId))
        || (options.backendSessionId !== undefined && (typeof options.backendSessionId !== 'string' || !options.backendSessionId))
        || (options.timeoutMs !== undefined && (!Number.isFinite(options.timeoutMs) || options.timeoutMs <= 0))
        || (['model', 'effort', 'mode'].includes(options.action) && (typeof options.value !== 'string' || !options.value.trim()))) {
      throw Error('INVALID_CONTROL_INPUT');
    }
    const timeout = options.timeoutMs === undefined ? {} : {timeoutMs: options.timeoutMs};
    const samePath = (a, b) => sameTargetPath(a, b, deps.platform ?? process.platform);
    const sdk = await (deps.loadRuntime ?? loadRuntime)(options.expectedVersion);
    const home = options.home ?? require('node:os').homedir();
    const auth = await (deps.loadAuth ?? loadAuth)(options.cwd, home);
    const stateDir = path.join(home, '.acpx');
    const store = (deps.createSessionStore ?? (dir => sdk.createFileSessionStore({stateDir: dir})))(stateDir);
    const bound = options.recordId === undefined ? undefined : await store.load(options.recordId);
    if (options.recordId !== undefined && (!bound || bound.acpxRecordId !== options.recordId
        || !samePath(bound.cwd, options.cwd) || bound.name !== options.session
        || typeof bound.acpSessionId !== 'string' || !bound.acpSessionId
        || (options.backendSessionId !== undefined && bound.acpSessionId !== options.backendSessionId))) {
      throw Error('RECORD_BINDING_MISMATCH');
    }
    // Existing tasks retain their creation-time launcher, including pre-flags Windows OMP.
    const effectiveArgv = bound
      ? bound.agentArgv ?? bound.agentCommand
      : await (deps.resolveConfiguredAgentArgv ?? resolveConfiguredAgentArgv)(
        options.agent, options.agentArgv, options.cwd, home);
    const legacyArgv = options.agent === 'omp' ? ['omp', 'acp'] : undefined;
    const baseRegistry = sdk.createAgentRegistry({overrides: {[options.agent]: effectiveArgv}});
    const agentRegistry = legacyArgv ? {...baseRegistry, resolve: agent =>
      agent === 'callcli-legacy-omp' ? legacyArgv : baseRegistry.resolve(agent)} : baseRegistry;
    shared = sdk.createSharedAcpRuntime({
      cwd: options.cwd, agentRegistry, permissionMode: options.permissionMode,
      nonInteractivePermissions: 'deny', ...timeout,
      authCredentials: auth.authCredentials, authPolicy: auth.authPolicy
    });
    const lookup = {sessionKey: options.session, agent: options.agent, cwd: options.cwd};
    const action = options.action;
    const findTarget = async () => {
      if (!bound) {
        const current = await shared.findSession(lookup);
        const legacy = legacyArgv ? await shared.findSession({...lookup, agent:'callcli-legacy-omp'}) : undefined;
        if (current && legacy && current.acpxRecordId !== legacy.acpxRecordId) throw Error('AMBIGUOUS_SESSION_SCOPE');
        const found = current ?? legacy;
        if (found && (!samePath(found.cwd, options.cwd) || found.sessionKey !== options.session)) throw Error('RECORD_BINDING_MISMATCH');
        return found;
      }
      if (bound.closed) return undefined;
      return {backend: 'acpx-shared', acpxRecordId: bound.acpxRecordId,
        runtimeSessionName: bound.acpxRecordId, backendSessionId: bound.acpSessionId,
        agentSessionId: bound.agentSessionId, sessionKey: bound.name, cwd: bound.cwd};
    };
    if (action === 'new') {
      const existing = await findTarget();
      if (existing) await shared.close({handle: existing});
      const created = await shared.ensureSession({
        ...lookup, mode: 'persistent',
        ...(typeof options.model === 'string' && options.model ? {sessionOptions: {model: options.model}} : {})
      });
      const record = await store.load(created.acpxRecordId);
      const agentSessionId = record?.agentSessionId ?? created.agentSessionId;
      await send({
        action: 'session_ensured', created: true,
        acpxRecordId: created.acpxRecordId,
        acpxSessionId: record?.acpSessionId ?? created.backendSessionId,
        ...(agentSessionId ? {agentSessionId} : {}),
        name: options.session,
        ...(existing ? {replacedSessionId: existing.acpxRecordId} : {})
      });
      ok = true;
    } else if (action === 'show') {
      const found = await findTarget();
      if (!found) throw Error('SESSION_NOT_FOUND');
      const record = await store.load(found.acpxRecordId);
      if (!record) throw Error('RECORD_NOT_FOUND');
      await send(record);
      ok = true;
    } else if (action === 'status') {
      const found = await findTarget();
      if (!found) {
        await send({action: 'status_snapshot', status: 'no-session', summary: 'no active session'});
        ok = true;
      } else {
        const status = await shared.getStatus({handle: found});
        const record = await store.load(found.acpxRecordId);
        const agentSessionId = record?.agentSessionId ?? found.agentSessionId;
        await send({
          action: 'status_snapshot',
          status: sessionRunState(status, record),
          summary: status?.summary ?? 'session idle',
          acpxRecordId: found.acpxRecordId,
          acpxSessionId: record?.acpSessionId ?? found.backendSessionId,
          ...(agentSessionId ? {agentSessionId} : {})
        });
        ok = true;
      }
    } else if (action === 'close') {
      const found = await findTarget();
      if (!found) throw Error('SESSION_NOT_FOUND');
      await shared.close({handle: found});
      const record = await store.load(found.acpxRecordId);
      const agentSessionId = record?.agentSessionId ?? found.agentSessionId;
      await send({
        action: 'session_closed',
        acpxRecordId: found.acpxRecordId,
        acpxSessionId: record?.acpSessionId ?? found.backendSessionId,
        ...(agentSessionId ? {agentSessionId} : {})
      });
      ok = true;
    } else if (action === 'cancel') {
      const found = await findTarget();
      if (!found) {
        await send({action: 'cancel_result', acpxRecordId: 'unknown', cancelled: false});
        ok = true;
      } else {
        await shared.cancel({handle: found});
        await send({action: 'cancel_result', acpxRecordId: found.acpxRecordId, cancelled: true});
        ok = true;
      }
    } else {
      const found = await findTarget();
      if (!found) throw Error('SESSION_NOT_FOUND');
      const applyControl = (rt, handle) => {
        if (action === 'model') return rt.setModel({handle, model: options.value});
        if (action === 'effort') return rt.setConfigOption({handle, key: 'thought_level', value: options.value});
        return rt.setMode({handle, mode: options.value});
      };
      try {
        await applyControl(shared, found);
      } catch (error) {
        if (error?.code !== 'ACP_BACKEND_UNAVAILABLE') throw error;
        const record = await store.load(found.acpxRecordId);
        if (!record || record.closed || record.acpxRecordId !== found.acpxRecordId
            || !samePath(record.cwd, options.cwd) || record.name !== options.session
            || record.acpSessionId !== found.backendSessionId) throw Error('RECORD_BINDING_MISMATCH');
        const idle = await (deps.controlStatus ?? controlStatus)(record, options);
        if (idle.status !== 'idle' || idle.acpxRecordId !== record.acpxRecordId
            || idle.acpxSessionId !== record.acpSessionId) throw Error('OWNER_NOT_IDLE');
        // 0.21.1 lacks the exact-resume guard and can recreate an unprompted session.
        // Reuse the existing version check rather than weaken old-task identity.
        await (deps.loadRuntime ?? loadRuntime)('0.22.0');
        const restoreRegistry = sdk.createAgentRegistry({overrides:{[options.agent]: record.agentArgv ?? record.agentCommand}});
        const launch = restoreRegistry.resolve(options.agent);
        const restoredArgv = Array.isArray(launch) ? launch : undefined;
        if (record.acpx?.reset_on_next_ensure === true
            || (restoredArgv ? renderArgvIdentity(restoredArgv) : launch) !== record.agentCommand
            || JSON.stringify(restoredArgv ?? []) !== JSON.stringify(record.agentArgv ?? [])) {
          throw Error('RECORD_BINDING_MISMATCH');
        }
        embedded = sdk.createAcpRuntime({
          cwd: options.cwd, sessionStore: store,
          agentRegistry: restoreRegistry,
          permissionMode: options.permissionMode, nonInteractivePermissions: 'deny', ...timeout,
          authCredentials: auth.authCredentials, authPolicy: auth.authPolicy
        });
        // Public ensure with the saved provider ID marks this record strict-resume.
        // It reuses the exact stored record; it must not create a replacement.
        const restored = await embedded.ensureSession({sessionKey: record.acpxRecordId,
          agent: options.agent, cwd: record.cwd, mode: 'persistent', resumeSessionId: record.acpSessionId});
        if (restored.acpxRecordId !== record.acpxRecordId || restored.backendSessionId !== record.acpSessionId) {
          throw Error('RECORD_BINDING_MISMATCH');
        }
        await applyControl(embedded, restored);
      }
      const updated = await store.load(found.acpxRecordId);
      const agentSessionId = updated?.agentSessionId ?? found.agentSessionId;
      await send({
        action: action === 'model' ? 'model_set' : action === 'effort' ? 'config_set' : 'mode_set',
        acpxRecordId: found.acpxRecordId,
        acpxSessionId: updated?.acpSessionId ?? found.backendSessionId,
        ...(agentSessionId ? {agentSessionId} : {})
      });
      ok = true;
    }
  } catch {
    ok = false;
  } finally {
    let cleanupFailed = false;
    if (embedded) {try {await embedded.shutdown();} catch {cleanupFailed = true;}}
    if (shared) {try {await shared.shutdown();} catch {cleanupFailed = true;}}
    if (cleanupFailed) ok = false;
  }
  return ok ? 0 : 1;
}

module.exports = {run, control, loadRuntime, loadAuth, resolveConfiguredAgentArgv, splitCommandLine, renderArgvIdentity};
