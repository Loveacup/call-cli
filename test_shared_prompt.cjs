const test = require('node:test');
const assert = require('node:assert/strict');
const { run } = require('./shared-prompt.cjs');
const options = {cwd: '/test/work', sessionId: '11111111-1111-4111-8111-111111111111', sessionName: 'call-owned', requestId: 'owned:1', write: false, timeoutMs: 12000};

test('shared owner result, not colliding callback error, settles the request', async () => {
  const frames = [], calls = [];
  const runtime = {
    startTurn(input) {
      calls.push(input);
      return {events: (async function* () {
        yield {id: 2, error: {code: -32603, message: 'PRIVATE_CALLBACK'}};
        yield {type: 'text_delta', stream: 'thought', tag: 'agent_thought_chunk', text: 'PRIVATE_THOUGHT'};
        yield {type: 'text_delta', stream: 'output', tag: 'agent_message_chunk', text: 'CALLCLI_STAGE checked\nOK\n'};
        yield {type: 'tool_call', toolCallId: 'read', status: 'completed', rawOutput: 'PRIVATE_TOOL'};
      })(), result: Promise.resolve({status: 'completed', stopReason: 'end_turn'})};
    },
    async shutdown() {calls.push('shutdown');}
  };
  let runtimeOptions;
  const code = await run(options, 'SECRET_PROMPT', {
    loadAuth: async () => ({authCredentials: {}, authPolicy: 'skip'}),
    loadRuntime: async () => ({createSharedAcpRuntime: opts => {runtimeOptions = opts; return runtime;}}),
    send: async frame => frames.push(frame)
  });
  assert.equal(code, 0);
  assert.equal(runtimeOptions.permissionMode, 'approve-reads');
  assert.equal(runtimeOptions.nonInteractivePermissions, 'deny');
  assert.equal(calls[0].requestId, options.requestId);
  assert.equal(calls[0].handle.acpxRecordId, options.sessionId);
  assert.equal(calls[0].handle.backend, 'acpx-shared');
  assert.equal(calls[0].mode, 'prompt');
  assert.equal(calls[0].text, 'SECRET_PROMPT');
  assert.equal(calls.at(-1), 'shutdown');
  assert.deepEqual(frames.at(-1), {type: 'callcli.shared.result', requestId: options.requestId, status: 'completed', stopReason: 'end_turn'});
  assert.equal(frames.length, 3);
  assert.doesNotMatch(JSON.stringify(frames), /PRIVATE|SECRET_PROMPT/);
});

for (const [name, result, expectedCode] of [
  ['prompt failure', {status: 'failed', error: {message: 'PRIVATE_PROVIDER'}}, 1],
  ['cancelled', {status: 'cancelled', stopReason: 'cancelled'}, 0],
  ['queued is not completion', {status: 'queued'}, 1],
  ['missing result', undefined, 1],
  ['contradictory success/error', {status: 'completed', stopReason: 'end_turn', error: {message: 'PRIVATE_ERROR'}}, 1],
]) {
  test(name, async () => {
    const frames = [];
    let starts = 0, shutdowns = 0;
    const code = await run(options, 'SECRET_PROMPT', {
      loadAuth: async () => ({authCredentials: {}, authPolicy: 'skip'}),
    loadRuntime: async () => ({createSharedAcpRuntime: () => ({
        startTurn() {starts++; return {events: (async function* () {})(), result: Promise.resolve(result)};},
        async shutdown() {shutdowns++;}
      })}),
      send: async frame => frames.push(frame)
    });
    assert.equal(code, expectedCode);
    assert.equal(starts, 1);
    assert.equal(shutdowns, 1);
    assert.equal(frames.length, 1);
    assert.equal(frames[0].status, expectedCode ? 'failed' : 'cancelled');
    assert.doesNotMatch(JSON.stringify(frames), /PRIVATE|SECRET_PROMPT/);
  });
}

test('output observer failure never reports success or replays work', async () => {
  let starts = 0, shutdowns = 0;
  const frames = [];
  const code = await run(options, 'task', {
    loadAuth: async () => ({authCredentials: {}, authPolicy: 'skip'}),
    loadRuntime: async () => ({createSharedAcpRuntime: () => ({
      startTurn() {starts++; return {events: (async function* () {
        yield {type: 'text_delta', stream: 'output', tag: 'agent_message_chunk', text: 'text'};
      })(), result: Promise.resolve({status: 'completed', stopReason: 'end_turn'})};},
      async shutdown() {shutdowns++;}
    })}),
    send: async frame => {if (frame.type === 'callcli.shared.event') throw Error('PRIVATE_OUTPUT_FAILURE'); frames.push(frame);}
  });
  assert.equal(code, 1);
  assert.equal(starts, 1);
  assert.equal(shutdowns, 1);
  assert.equal(frames.at(-1).status, 'failed');
  assert.doesNotMatch(JSON.stringify(frames), /PRIVATE/);
});

test('malformed native handle is rejected before SDK or agent invocation', async () => {
  let loads = 0;
  const frames = [];
  const code = await run({...options, sessionId: 'invalid-do-not-repair'}, 'task', {
    loadAuth: async () => ({authCredentials: {}, authPolicy: 'skip'}),
    loadRuntime: async () => {loads++; throw Error('must not load');},
    send: async frame => frames.push(frame)
  });
  assert.equal(code, 1);
  assert.equal(loads, 0);
  assert.equal(frames[0].status, 'failed');
});

test('execution-device auth and policy reach native shared owner without leaking to frames', async () => {
  const frames = [];
  const synthetic = {authCredentials: {'fixture-only': '  SYNTHETIC_CREDENTIAL  '}, authPolicy: 'fail'};
  let received;
  const code = await run(options, 'task', {
    loadAuth: async cwd => {assert.equal(cwd, options.cwd); return synthetic;},
    loadRuntime: async () => ({createSharedAcpRuntime: config => {
      received = config;
      return {startTurn: () => ({events: (async function* () {})(), result: Promise.resolve({status: 'completed', stopReason: 'end_turn'})}), shutdown: async () => {}};
    }}),
    send: async frame => frames.push(frame)
  });
  assert.equal(code, 0);
  assert.deepEqual(received.authCredentials, synthetic.authCredentials);
  assert.equal(received.authPolicy, 'fail');
  assert.doesNotMatch(JSON.stringify(frames), /SYNTHETIC_CREDENTIAL|fixture-only/);
});

test('auth loader matches native global/project precedence and preserves literal credentials', async () => {
  const fs = require('node:fs'), path = require('node:path'), os = require('node:os');
  const {loadAuth} = require('./shared-prompt.cjs');
  const root = fs.mkdtempSync(path.join(os.homedir(), '.hermes/cache/scratch/shared-auth-'));
  try {
    const home = path.join(root, 'home'), cwd = path.join(root, 'work');
    fs.mkdirSync(path.join(home, '.acpx'), {recursive: true}); fs.mkdirSync(cwd);
    const globalFile = path.join(home, '.acpx/config.json'), projectFile = path.join(cwd, '.acpxrc.json');
    fs.writeFileSync(globalFile, JSON.stringify({authPolicy: 'fail', auth: {shared: 'GLOBAL_FAKE', unchanged: '  PRESERVE_FAKE  '}}));
    fs.writeFileSync(projectFile, JSON.stringify({authPolicy: 'skip', auth: {shared: 'PROJECT_FAKE'}}));
    let loaded = await loadAuth(cwd, home);
    assert.deepEqual({...loaded.authCredentials}, {shared: 'PROJECT_FAKE', unchanged: '  PRESERVE_FAKE  '});
    assert.equal(loaded.authPolicy, 'skip');
    fs.writeFileSync(globalFile, JSON.stringify({authPolicy: 'invalid-overridden', auth: {}}));
    fs.writeFileSync(projectFile, JSON.stringify({authPolicy: 'fail'}));
    assert.equal((await loadAuth(cwd, home)).authPolicy, 'fail');
    fs.writeFileSync(projectFile, JSON.stringify({authPolicy: false}));
    await assert.rejects(loadAuth(cwd, home));
    fs.writeFileSync(projectFile, JSON.stringify({auth: {bad: '   '}}));
    await assert.rejects(loadAuth(cwd, home));
  } finally {fs.rmSync(root, {recursive: true, force: true});}
});

test('credential-loader exceptions fail before native submission without leaking details', async () => {
  const frames = []; let starts = 0;
  const code = await run(options, 'task', {
    loadRuntime: async () => ({createSharedAcpRuntime: () => {starts++;}}),
    loadAuth: async () => {throw Error('SYNTHETIC_PRIVATE_CREDENTIAL');},
    send: async frame => frames.push(frame)
  });
  assert.equal(code, 1); assert.equal(starts, 0);
  assert.equal(frames.at(-1).status, 'failed');
  assert.doesNotMatch(JSON.stringify(frames), /SYNTHETIC_PRIVATE_CREDENTIAL/);
});
