const test = require('node:test');
const assert = require('node:assert/strict');
const { run, control, resolveConfiguredAgentArgv } = require('./shared-prompt.cjs');
const options = {cwd: '/test/work', sessionId: '11111111-1111-4111-8111-111111111111', sessionName: 'call-owned', requestId: 'owned:1', write: false, timeoutMs: 12000};

test('omitted deadline allows a turn to remain active until explicit cancellation', async () => {
  const input = {...options}; delete input.timeoutMs;
  const frames = []; let resolveResult, started, cancel;
  const accepted = new Promise(resolve => {started = resolve;});
  const running = run(input, 'task', {
    loadAuth:async () => ({authCredentials:{}, authPolicy:'skip'}),
    loadRuntime:async () => ({createSharedAcpRuntime: config => {
      assert.equal(Object.hasOwn(config, 'timeoutMs'), false);
      return {
        startTurn(turnInput) {
          assert.equal(Object.hasOwn(turnInput, 'timeoutMs'), false);
          const result = new Promise(resolve => {resolveResult = resolve;});
          cancel = () => resolveResult({status:'cancelled', stopReason:'cancelled'});
          started();
          return {events:(async function* () {await result;})(), result};
        }, shutdown:async () => {}
      };
    }}), send:async frame => frames.push(frame)
  });
  const admitted = await Promise.race([accepted.then(() => true), running.then(() => false)]);
  assert.equal(admitted, true);
  cancel();
  assert.equal(await running, 0);
  assert.equal(frames.at(-1).status, 'cancelled');
});

test('prompt acceptance is published once only after the public signal resolves', async () => {
  let accept, finish;
  const frames = [];
  const promptStarted = new Promise(resolve => {accept = resolve;});
  const result = new Promise(resolve => {finish = resolve;});
  const running = run(options, 'task', {
    loadAuth:async () => ({authCredentials:{}, authPolicy:'skip'}),
    loadRuntime:async () => ({createSharedAcpRuntime:() => ({
      startTurn:() => ({promptStarted, events:(async function* () {await result;})(), result}),
      shutdown:async () => {}
    })}), send:async frame => frames.push(frame)
  });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(frames, []);
  accept();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(frames, [{type:'callcli.shared.event', requestId:options.requestId, event:{type:'prompt_started'}}]);
  finish({status:'cancelled', stopReason:'cancelled'});
  assert.equal(await running, 0);
  assert.equal(frames.filter(frame => frame.event?.type === 'prompt_started').length, 1);
});

test('invalid explicit deadlines are refused before submitting any prompt', async () => {
  for (const timeoutMs of [0, -1, NaN, Infinity, null, '1200']) {
    let starts = 0;
    const frames = [];
    assert.equal(await run({...options, timeoutMs}, 'task', {
      loadAuth:async () => ({authCredentials:{}, authPolicy:'skip'}),
      loadRuntime:async () => ({createSharedAcpRuntime:() => {starts++; throw Error('unexpected');}}),
      send:async frame => frames.push(frame)
    }), 1);
    assert.equal(starts, 0);
    assert.equal(frames.at(-1).status, 'failed');
  }
});

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
  assert.deepEqual(frames, [{type:'callcli.shared.result', requestId:options.requestId,
    status:'failed', stopReason:null, stage:'input_validate', reasonCode:'input_validate_failed'}]);
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

test('safe tool identifiers survive projection but tool bodies and command titles do not', async () => {
  const frames = [];
  await run({...options, permissionMode: 'approve-all'}, 'task', {
    loadAuth: async () => ({authCredentials: {}, authPolicy: 'skip'}),
    loadRuntime: async () => ({createSharedAcpRuntime: () => ({
      startTurn: () => ({events: (async function* () {
        yield {type:'tool_call', toolCallId:'shell1', title:'run_command', kind:'execute', status:'failed',
          text:'PRIVATE_SUMMARY', rawInput:'PRIVATE_INPUT', rawOutput:'PRIVATE_OUTPUT', content:['PRIVATE_CONTENT']};
        yield {type:'tool_call', toolCallId:'shell2', title:'printf PRIVATE_ARG', kind:'execute', status:'completed'};
        yield {type:'tool_call', toolCallId:'123', title:'private/path', status:'failed'};
      })(), result:Promise.resolve({status:'completed', stopReason:'end_turn'})}),
      shutdown: async () => {}
    })}),
    send: async frame => frames.push(frame)
  });
  assert.deepEqual(frames[0].event, {type:'tool_call', toolCallId:'shell1', title:'run_command', kind:'execute', status:'failed'});
  assert.equal(frames[2].event.toolCallId, '123');
  assert.equal(frames[2].event.title, undefined);
  assert.doesNotMatch(JSON.stringify(frames), /PRIVATE/);
});

test('explicit approval wins over write while legacy mapping stays supported', async () => {
  for (const [input, expected] of [
    [{...options, permissionMode:'approve-all'}, 'approve-all'],
    [{...options, write:true, permissionMode:'approve-reads'}, 'approve-reads'],
    [{...options, write:true}, 'approve-all']
  ]) {
    let policy;
    const code = await run(input, 'task', {
      loadAuth: async () => ({authCredentials:{}, authPolicy:'skip'}),
      loadRuntime: async () => ({createSharedAcpRuntime: config => {
        policy = config.permissionMode;
        return {startTurn: () => ({events:(async function* () {})(), result:Promise.resolve({status:'completed',stopReason:'end_turn'})}), shutdown:async () => {}};
      }}), send:async () => {}
    });
    assert.equal(code, 0);
    assert.equal(policy, expected);
  }
});

test('only a failed turn AUTH_REQUIRED detail yields a safe task-bound diagnostic', async () => {
  for (const [status, error, expected] of [
    ['failed', {code:'RUNTIME', detailCode:'AUTH_REQUIRED', message:'PRIVATE_MESSAGE', retryable:true}, 'auth_required'],
    ['failed', {code:'AUTH_REQUIRED', detailCode:'PRIVATE_DETAIL', message:'PRIVATE_MESSAGE'}, undefined],
    ['failed', {detailCode:'AUTH_REQUIRED_PRIVATE', message:'PRIVATE_MESSAGE'}, undefined],
    ['failed', {detailCode:401, message:'UNAUTHENTICATED PRIVATE_MESSAGE'}, undefined],
    ['failed', undefined, undefined],
    ['completed', {detailCode:'AUTH_REQUIRED', message:'PRIVATE_MESSAGE'}, undefined]
  ]) {
    const frames = [];
    let starts = 0;
    const code = await run(options, 'PRIVATE_PROMPT', {
      loadAuth: async () => ({authCredentials:{}, authPolicy:'skip'}),
      loadRuntime: async () => ({createSharedAcpRuntime: () => ({
        startTurn() {
          starts++;
          return {events:(async function* () {})(), result:Promise.resolve({status, error, stopReason:'end_turn'})};
        },
        shutdown:async () => {}
      })}),
      send:async frame => frames.push(frame)
    });
    assert.equal(code, 1);
    assert.equal(starts, 1);
    assert.deepEqual(frames, [{type:'callcli.shared.result', requestId:options.requestId,
      status:'failed', stopReason:null, stage:'prompt_result', reasonCode:status === 'failed' ? 'turn_failed' : 'invalid_result',
      ...(expected ? {errorCategory:expected} : {})}]);
    assert.doesNotMatch(JSON.stringify(frames), /PRIVATE|UNAUTHENTICATED|retryable/);
  }
});

test('only fixed failed-turn codes project deadline and disconnect diagnostics without replay', async () => {
  for (const [result, diagnostic] of [
    [{status:'failed', error:{code:'TIMEOUT', detailCode:'QUEUE_RUNTIME_PROMPT_FAILED'}}, {errorCategory:'timeout', errorSource:'turn_result_code'}],
    [{status:'failed', error:{code:'RUNTIME', detailCode:'AGENT_DISCONNECTED'}}, {errorCategory:'connection_lost', errorSource:'turn_result_detail'}],
    [{status:'failed', error:{code:'TIMEOUT', detailCode:'AUTH_REQUIRED'}}, {errorCategory:'auth_required'}],
    [{status:'failed', error:{code:'RUNTIME', detailCode:'QUEUE_RUNTIME_PROMPT_FAILED'}}, {}],
    [{status:'failed', error:{code:'NO_SESSION', detailCode:'TIMEOUT'}}, {}],
    [{status:'failed', error:{code:'AGENT_DISCONNECTED', detailCode:'PRIVATE_DETAIL'}}, {}],
    [{status:'failed', error:{code:'TIMEOUT_PRIVATE', detailCode:'AGENT_DISCONNECTED_PRIVATE'}}, {}],
    [{status:'completed', stopReason:'end_turn'}, {}],
    [{status:'cancelled', stopReason:'cancelled'}, {}],
    [{status:'completed', stopReason:'end_turn', error:{code:'TIMEOUT'}}, {}],
    [{status:'cancelled', stopReason:'cancelled', error:{detailCode:'AGENT_DISCONNECTED'}}, {}]
  ]) {
    const frames = []; let starts = 0;
    if (result.error) Object.assign(result.error, {message:'PRIVATE_MESSAGE', retryable:true, acp:{secret:'PRIVATE_ACP'}});
    const code = await run(options, 'PRIVATE_PROMPT', {
      loadAuth:async () => ({authCredentials:{}, authPolicy:'skip'}),
      loadRuntime:async () => ({createSharedAcpRuntime:() => ({
        startTurn() {starts++; return {events:(async function* () {})(), result:Promise.resolve(result)};},
        shutdown:async () => {}
      })}), send:async frame => frames.push(frame)
    });
    const valid = !result.error && ['completed','cancelled'].includes(result.status);
    assert.equal(code, valid ? 0 : 1);
    assert.equal(starts, 1);
    assert.deepEqual(frames, [{type:'callcli.shared.result', requestId:options.requestId,
      status:valid ? result.status : 'failed', stopReason:valid ? result.stopReason : null,
      ...(!valid ? {stage:'prompt_result', reasonCode:result.status === 'failed' ? 'turn_failed' : 'invalid_result'} : {}), ...diagnostic}]);
    assert.doesNotMatch(JSON.stringify(frames), /PRIVATE|retryable|QUEUE_RUNTIME|AGENT_DISCONNECTED|TIMEOUT/);
  }
});

test('failed queued turns recover only unambiguous structured prompt auth from passive watch', async () => {
  const started = {type:'turn_started', requestId:options.requestId};
  const message = value => ({type:'message', requestId:options.requestId, message:value});
  const prompt = message({id:2, method:'session/prompt', params:{prompt:'PRIVATE_PROMPT'}});
  const auth = message({id:2, error:{code:-32000, message:'PRIVATE_ERROR', data:{authRequired:true, token:'PRIVATE_TOKEN'}}});
  const end = {type:'turn_result', requestId:options.requestId, result:{status:'failed'}};
  for (const [events, expected] of [
    [[started, prompt, auth, end], true],
    [[started, prompt, message({id:2, error:{code:-32000, message:'authentication required PRIVATE_ERROR'}}), end], false],
    [[started, prompt, message({id:2, error:{code:-32000, data:{authRequired:'true'}}}), end], false],
    [[started, prompt, message({id:2, error:{code:401, data:{authRequired:true}}}), end], false],
    [[started, prompt, {...auth, requestId:'foreign:1'}, end], false],
    [[started, prompt, message({id:2, method:'fs/read_text_file'}), auth, end], false],
    [[started, prompt, auth, auth, end], false],
    [[started, prompt, prompt, auth, end], false],
    [[started, prompt, auth], false],
    [[prompt, auth, end], false],
    [[started, prompt, auth, {...end, result:{status:'completed'}}], false],
    [[...Array(1500).fill({type:'message', requestId:'foreign:1'}), started, prompt, auth, end], true],
    [[{type:'turn_started',requestId:'owned:0'}, {...prompt,requestId:'owned:0'},
      {...auth,requestId:'owned:0'}, {...end,requestId:'owned:0'}, started, prompt,
      message({id:2,error:{code:-32000,data:{authRequired:false}}}), end], false]
  ]) {
    const frames = []; let starts = 0, watchSignal, watchHandle;
    await run(options, 'PRIVATE_PROMPT', {
      loadAuth:async () => ({authCredentials:{}, authPolicy:'skip'}),
      loadRuntime:async () => ({createSharedAcpRuntime: () => ({
        startTurn() {starts++; return {events:(async function* () {})(), result:Promise.resolve({
          status:'failed', error:{detailCode:'QUEUE_RUNTIME_PROMPT_FAILED', message:'PRIVATE_ERROR'}})};},
        async *watchSession({handle, signal}) {watchSignal=signal;watchHandle=handle;yield* events;},
        shutdown:async () => {}
      })}), send:async frame => frames.push(frame)
    });
    assert.equal(frames.at(-1).errorCategory, expected ? 'auth_required' : undefined);
    assert.equal(frames.at(-1).errorSource, expected ? 'session_watch' : undefined);
    assert.equal(starts, 1);
    assert.equal(watchHandle.acpxRecordId, options.sessionId);
    assert.equal(watchSignal.aborted, true);
    assert.doesNotMatch(JSON.stringify(frames), /PRIVATE|authRequired|QUEUE_RUNTIME/);
  }
});

test('passive-watch errors and unavailable journals cannot replace the failed result', async () => {
  for (const watchSession of [undefined, async function* () {throw Error('PRIVATE_WATCH');},
    async function* ({signal}) {await new Promise(resolve => signal.addEventListener('abort',resolve,{once:true}));}]) {
    const frames=[];
    const code=await run(options, 'task', {
      loadAuth:async () => ({authCredentials:{}, authPolicy:'skip'}),
      loadRuntime:async () => ({createSharedAcpRuntime: () => ({
        startTurn:() => ({events:(async function* () {})(),result:Promise.resolve({status:'failed'})}),
        watchSession, shutdown:async () => {}
      })}),send:async frame => frames.push(frame)
    });
    assert.equal(code,1);
    assert.deepEqual(frames,[{type:'callcli.shared.result',requestId:options.requestId,status:'failed',stopReason:null,
      stage:'prompt_result',reasonCode:'turn_failed'}]);
  }
});

test('watch deadline publishes failure before noncooperative next settles and cleans late work', async () => {
  for (const rejectLate of [false, true]) {
    const frames = [];
    let release, entered, cleaned = 0, returned = 0, shutdown = false, signal;
    const pending = new Promise(resolve => {release = resolve;});
    const started = new Promise(resolve => {entered = resolve;});
    const runPromise = run(options, 'task', {
      loadAuth:async () => ({authCredentials:{}, authPolicy:'skip'}),
      loadRuntime:async () => ({createSharedAcpRuntime: () => ({
        startTurn:() => ({events:(async function* () {})(), result:Promise.resolve({status:'failed'})}),
        watchSession(input) {
          signal = input.signal;
          const iterator = (async function* () {
            try {
              entered();
              await pending;
              if (rejectLate) throw Error('PRIVATE_LATE_REJECTION');
              yield {type:'turn_started',requestId:options.requestId};
            } finally {cleaned++;}
          })();
          const close = iterator.return.bind(iterator);
          iterator.return = (...args) => {returned++; return close(...args);};
          return iterator;
        },
        shutdown:async () => {shutdown = true;}
      })}), send:async frame => frames.push(frame)
    });
    await started;
    let early;
    try {
      early = await Promise.race([runPromise.then(code => ({code})),
        new Promise(resolve => setTimeout(() => resolve(null), 1300))]);
    } finally {release(); await runPromise;}
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(early, {code:1}, 'diagnostic I/O must not delay the original failure');
    assert.equal(signal.aborted, true);
    assert.equal(shutdown, true);
    assert.equal(returned, 1);
    assert.equal(cleaned, 1);
    assert.deepEqual(frames, [{type:'callcli.shared.result',requestId:options.requestId,status:'failed',stopReason:null,
      stage:'prompt_result',reasonCode:'turn_failed'}]);
  }
});

test('iterator return rejection or pending cleanup cannot delay the failed frame', async () => {
  for (const close of [() => Promise.reject(Error('PRIVATE_CLOSE')),
    () => new Promise(() => {}), () => {throw Error('PRIVATE_CLOSE');}]) {
    const frames = []; let returned = 0;
    const code = await run(options, 'task', {
      loadAuth:async () => ({authCredentials:{}, authPolicy:'skip'}),
      loadRuntime:async () => ({createSharedAcpRuntime: () => ({
        startTurn:() => ({events:(async function* () {})(),result:Promise.resolve({status:'failed'})}),
        watchSession:() => ({[Symbol.asyncIterator]() {return this;},
          next:async () => ({done:true}), return() {returned++;return close();}}),
        shutdown:async () => {}
      })}),send:async frame => frames.push(frame)
    });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(code, 1);
    assert.equal(returned, 1);
    assert.deepEqual(frames,[{type:'callcli.shared.result',requestId:options.requestId,status:'failed',stopReason:null,
      stage:'prompt_result',reasonCode:'turn_failed'}]);
  }
});

test('agentEnv sets allowed INITIAL_AGENT_MODE during runtime creation and restores process.env cleanly', async () => {
  const prev = Object.hasOwn(process.env, 'INITIAL_AGENT_MODE') ? process.env.INITIAL_AGENT_MODE : undefined;
  delete process.env.INITIAL_AGENT_MODE;
  try {
    let observedEnv;
    const frames = [];
    const code = await run({...options, permissionMode: 'approve-all', agentEnv: {INITIAL_AGENT_MODE: 'agent-full-access'}}, 'task', {
      loadAuth: async () => ({authCredentials: {}, authPolicy: 'skip'}),
      loadRuntime: async () => ({createSharedAcpRuntime: () => {
        observedEnv = process.env.INITIAL_AGENT_MODE;
        return {
          startTurn: () => ({events: (async function* () {})(), result: Promise.resolve({status: 'completed', stopReason: 'end_turn'})}),
          shutdown: async () => {}
        };
      }}),
      send: async frame => frames.push(frame)
    });
    assert.equal(code, 0);
    assert.equal(observedEnv, 'agent-full-access');
    assert.equal(Object.hasOwn(process.env, 'INITIAL_AGENT_MODE'), false);
    for (const badEnv of [null, [], 'str', {OTHER: 'agent-full-access'}, {INITIAL_AGENT_MODE: 'plan'}]) {
      let loads = 0;
      const errFrames = [];
      const badCode = await run({...options, agentEnv: badEnv}, 'task', {
        loadAuth: async () => ({authCredentials: {}, authPolicy: 'skip'}),
        loadRuntime: async () => {loads++; throw Error('unexpected');},
        send: async frame => errFrames.push(frame)
      });
      assert.equal(badCode, 1);
      assert.equal(loads, 0);
      assert.equal(errFrames.at(-1).status, 'failed');
    }
  } finally {
    if (prev === undefined) delete process.env.INITIAL_AGENT_MODE;
    else process.env.INITIAL_AGENT_MODE = prev;
  }
});

test('resolveConfiguredAgentArgv merges configured executable/argv with native-skip flags without mutating config files', async () => {
  const fs = require('node:fs'), path = require('node:path'), os = require('node:os');
  const root = fs.mkdtempSync(path.join(os.homedir(), '.hermes/cache/scratch/shared-argv-'));
  try {
    const home = path.join(root, 'home'), cwd = path.join(root, 'work');
    fs.mkdirSync(path.join(home, '.acpx'), {recursive: true}); fs.mkdirSync(cwd);
    const globalFile = path.join(home, '.acpx/config.json'), projectFile = path.join(cwd, '.acpxrc.json');
    const globalRaw = JSON.stringify({agents: {omp: {argv: ['C:\\Users\\example\\AppData\\Local\\omp\\omp.exe', 'acp']}}});
    fs.writeFileSync(globalFile, globalRaw);
    assert.deepEqual(
      await resolveConfiguredAgentArgv('omp', ['omp', 'acp', '--auto-approve', '--allow-home'], cwd, home),
      ['C:\\Users\\example\\AppData\\Local\\omp\\omp.exe', 'acp', '--auto-approve', '--allow-home']
    );
    fs.writeFileSync(projectFile, JSON.stringify({agents: {omp: {command: '"/opt/Custom App/omp" acp --auto-approve'}}}));
    assert.deepEqual(
      await resolveConfiguredAgentArgv('omp', ['omp', 'acp', '--auto-approve', '--allow-home'], cwd, home),
      ['/opt/Custom App/omp', 'acp', '--auto-approve', '--allow-home']
    );
    assert.equal(fs.readFileSync(globalFile, 'utf8'), globalRaw);
  } finally {fs.rmSync(root, {recursive: true, force: true});}
});

test('control executes structured-argv lifecycle actions via SharedAcpRuntime with idle fallback and secret-free failure', async () => {
  const calls = [];
  let closedOld = false, shutdownCount = 0, embeddedShutdownCount = 0, existingPresent = true;
  const oldId = '22222222-2222-4222-8222-222222222222';
  const newId = '33333333-3333-4333-8333-333333333333';
  const records = {
    [oldId]: {acpxRecordId: oldId, acpSessionId: 'acp-old', agentSessionId: 'ag-old', cwd:'C:/work', name:'call-1', agentArgv:['C:\\omp.exe','acp']},
    [newId]: {acpxRecordId: newId, acpSessionId: 'acp-new', agentSessionId: 'ag-new', pid: process.pid, cwd:'C:/work', name:'call-1', agentArgv:['C:\\omp.exe','acp','--auto-approve','--allow-home']}
  };
  for (const record of Object.values(records)) record.agentCommand = require('./shared-prompt.cjs').renderArgvIdentity(record.agentArgv);
  const makeSdk = () => ({
    ACPX_BACKEND_ID: 'acpx',
    encodeAcpxRuntimeHandleState: s => `acpx:v2:${Buffer.from(JSON.stringify(s)).toString('base64url')}`,
    createAgentRegistry: ({overrides}) => {calls.push(['registry', overrides]); return {overrides,resolve:agent=>overrides[agent]};} ,
    createSharedAcpRuntime: cfg => {
      calls.push(['shared', cfg]);
      return {
        findSession: async ({agent}) => existingPresent
          ? {backend: 'acpx-shared', acpxRecordId: existingPresent === 'old' ? oldId : newId,
             runtimeSessionName: existingPresent === 'old' ? oldId : newId,
             backendSessionId: existingPresent === 'old' ? 'acp-old' : 'acp-new', sessionKey: 'call-1', cwd: cfg.cwd, agent}
          : undefined,
        close: async ({handle}) => {
          calls.push(['close', handle.acpxRecordId]);
          if (handle.acpxRecordId === oldId) {closedOld = true; existingPresent = false;}
          else existingPresent = false;
        },
        ensureSession: async input => {
          calls.push(['ensure', input]);
          existingPresent = true;
          return {backend: 'acpx-shared', acpxRecordId: newId, runtimeSessionName: newId,
            backendSessionId: 'acp-new', sessionKey: input.sessionKey, cwd: input.cwd};
        },
        getStatus: async () => ({acpxRecordId: newId, backendSessionId: 'acp-new', summary: 'session=' + newId + ' open'}),
        setModel: async () => {throw Object.assign(Error('No running owner'), {code:'ACP_BACKEND_UNAVAILABLE'});},
        setConfigOption: async input => {calls.push(['shared_cfg', input]);},
        setMode: async input => {calls.push(['shared_mode', input]);},
        cancel: async ({handle}) => {calls.push(['cancel', handle.acpxRecordId]);},
        shutdown: async () => {shutdownCount++;}
      };
    },
    createAcpRuntime: cfg => {
      calls.push(['embedded', cfg.permissionMode]);
      return {
        ensureSession: async input => {
          calls.push(['embedded_restore', input]);
          return {acpxRecordId:input.sessionKey,backendSessionId:input.resumeSessionId};
        },
        setModel: async input => {calls.push(['embedded_model', input.model, input.handle.acpxRecordId]);},
        shutdown: async () => {embeddedShutdownCount++;}
      };
    }
  });
  const base = {cwd: 'C:/work', session: 'call-1', agent: 'omp',
    agentArgv: ['omp', 'acp', '--auto-approve', '--allow-home'], permissionMode: 'approve-all', timeoutMs: 20000};
  const sharedDeps = out => ({
    loadRuntime: async () => makeSdk(),
    loadAuth: async () => ({authCredentials: {}, authPolicy: 'skip'}),
    resolveConfiguredAgentArgv: async (_a, req) => ['C:\\omp.exe', ...req.slice(1)],
    controlStatus: async record => ({status:'idle',acpxRecordId:record.acpxRecordId,acpxSessionId:record.acpSessionId}),
    createSessionStore: () => ({load: async id => records[id]}),
    send: async frame => out.push(frame)
  });

  existingPresent = 'old';
  let frames = [];
  assert.equal(await control({...base, action: 'new', model: 'm-1'}, sharedDeps(frames)), 0);
  assert.equal(closedOld, true);
  assert.deepEqual(calls.find(c => c[0] === 'registry')[1], {omp: ['C:\\omp.exe', 'acp', '--auto-approve', '--allow-home']});
  assert.equal(frames[0].action, 'session_ensured');
  assert.equal(frames[0].replacedSessionId, oldId);

  frames = [];
  assert.equal(await control({...base, action: 'show'}, sharedDeps(frames)), 0);
  assert.equal(frames[0].acpxRecordId, newId);

  frames = [];
  assert.equal(await control({...base, action: 'status'}, sharedDeps(frames)), 0);
  assert.equal(frames[0].action, 'status_snapshot');
  assert.equal(frames[0].status, 'alive');

  frames = [];
  assert.equal(await control({...base, action: 'model', value: 'gpt-x'}, sharedDeps(frames)), 0);
  assert.deepEqual(frames[0], {action: 'model_set', acpxRecordId: newId, acpxSessionId: 'acp-new', agentSessionId: 'ag-new'});
  assert.ok(calls.some(c => c[0] === 'embedded_model' && c[1] === 'gpt-x' && c[2] === newId));
  assert.equal(embeddedShutdownCount, 1);

  frames = [];
  assert.equal(await control({...base, action: 'cancel'}, sharedDeps(frames)), 0);
  assert.equal(frames[0].cancelled, true);

  frames = [];
  assert.equal(await control({...base, action: 'close'}, sharedDeps(frames)), 0);
  assert.equal(frames[0].action, 'session_closed');

  frames = [];
  assert.equal(await control({...base, action: 'status'}, sharedDeps(frames)), 0);
  assert.equal(frames[0].status, 'no-session');
  assert.equal(shutdownCount, 7);

  // Secret-free failure returns code 1 and emits nothing on failure.
  frames = [];
  assert.equal(await control({...base, action: 'new'}, {
    ...sharedDeps(frames),
    loadRuntime: async () => {throw Error('SECRET_TOKEN_LEAK');}
  }), 1);
  assert.equal(frames.length, 0);
});
