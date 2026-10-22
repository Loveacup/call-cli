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
    return {type: 'tool_call', toolCallId: typeof event.toolCallId === 'string' ? event.toolCallId : '',
      status: typeof event.status === 'string' ? event.status : ''};
  }
}

function sendFrame(frame) {
  return new Promise((resolve, reject) => process.stdout.write(JSON.stringify(frame) + '\n', error => error ? reject(error) : resolve()));
}

async function run(options, prompt, deps = {}) {
  const send = deps.send ?? sendFrame;
  let runtime, code = 1;
  const requestId = options?.requestId;
  try {
    if (!options || typeof requestId !== 'string' || !requestId || typeof prompt !== 'string'
        || typeof options.write !== 'boolean' || !path.isAbsolute(options.cwd)
        || !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(options.sessionId)
        || typeof options.sessionName !== 'string' || !options.sessionName
        || !Number.isFinite(options.timeoutMs) || options.timeoutMs <= 0) throw Error('INVALID_INPUT');
    const sdk = await (deps.loadRuntime ?? loadRuntime)(options.expectedVersion);
    const auth = await (deps.loadAuth ?? loadAuth)(options.cwd);
    runtime = sdk.createSharedAcpRuntime({cwd: options.cwd,
      permissionMode: options.write ? 'approve-all' : 'approve-reads',
      nonInteractivePermissions: 'deny', timeoutMs: options.timeoutMs,
      authCredentials: auth.authCredentials, authPolicy: auth.authPolicy});
    // This serializable public handle is restored from the coordinator's native
    // session-created record, not from a model-supplied provider session ID.
    const handle = {backend: 'acpx-shared', acpxRecordId: options.sessionId,
      sessionKey: options.sessionName, runtimeSessionName: options.sessionId, cwd: options.cwd};
    const turn = runtime.startTurn({handle, text: prompt, mode: 'prompt', requestId,
      timeoutMs: options.timeoutMs});
    for await (const event of turn.events) {
      const projected = publicEvent(event);
      if (projected) await send({type: 'callcli.shared.event', requestId, event: projected});
    }
    const result = await turn.result;
    const valid = result && !result.error &&
      ((result.status === 'completed' && result.stopReason === 'end_turn') ||
       (result.status === 'cancelled' && result.stopReason === 'cancelled'));
    await send({type: 'callcli.shared.result', requestId,
      status: valid ? result.status : 'failed', stopReason: valid ? result.stopReason : null});
    code = valid ? 0 : 1;
  } catch {
    // Never serialize exceptions: they can include private prompts/configuration.
    try {await send({type: 'callcli.shared.result', requestId: requestId ?? null, status: 'failed', stopReason: null});} catch {}
  } finally {
    // shutdown detaches this client; it is NOT cancellation of accepted work.
    if (runtime) {try {await runtime.shutdown();} catch {code = 1;}}
  }
  return code;
}

module.exports = {run, loadRuntime, loadAuth};
