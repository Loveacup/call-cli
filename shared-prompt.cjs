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

async function loadAuth(cwd, home = require('node:os').homedir()) {
  // Match ACPX CLI precedence on this device; never send credentials via the controller.
  const configs = [];
  for (const file of [path.join(home, '.acpx', 'config.json'), path.join(cwd, '.acpxrc.json')]) {
    let config = {};
    try {config = JSON.parse(await fs.promises.readFile(file, 'utf8'));} catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
    if (!config || typeof config !== 'object' || Array.isArray(config)) throw Error('INVALID_AUTH_CONFIG');
    configs.push(config);
  }
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
    const permissionMode = options.permissionMode ?? (options.write ? 'approve-all' : 'approve-reads');
    if (!['approve-all', 'approve-reads'].includes(permissionMode)) throw Error('INVALID_PERMISSION_MODE');
    const timeout = options.timeoutMs === undefined ? {} : {timeoutMs:options.timeoutMs};
    stage = 'runtime_load';
    const sdk = await (deps.loadRuntime ?? loadRuntime)(options.expectedVersion);
    stage = 'auth_load';
    const auth = await (deps.loadAuth ?? loadAuth)(options.cwd);
    stage = 'runtime_create';
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
    try {await send(terminal);} catch {code = 1;}
    // shutdown detaches this client; it is NOT cancellation of accepted work.
    if (runtime) {try {await runtime.shutdown();} catch {code = 1;}}
  }
  return code;
}

module.exports = {run, loadRuntime, loadAuth};
