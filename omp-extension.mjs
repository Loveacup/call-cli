import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';

const actions = new Set(['start', 'list', 'status', 'events', 'reply', 'dialogue', 'permission', 'intervene', 'cancel', 'close']);
const fields = new Set(['action', 'task_id', 'agent', 'device', 'cwd', 'prompt', 'write', 'resource', 'clear_timeout', 'model', 'effort', 'agy_approval', 'agy_swarm', 'agy_worker_model', 'agy_mode', 'remote_shell', 'skills', 'question_id', 'command_id', 'expected_turn', 'after', 'permission_id', 'script_sha256', 'decision']);
const terminal = new Set(['completed', 'cancelled', 'unknown', 'closed']);
const visible = new Set(['stage', 'question', 'permission', 'answer', 'completed', 'waiting_reply', 'cancelled', 'unknown', 'skill']);

function bridgeRequest(request) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.env.CALL_CLI_PYTHON || 'python3', ['-B', fileURLToPath(new URL('./omp_bridge.py', import.meta.url))], {stdio: ['pipe', 'pipe', 'ignore']});
    let output = '';
    const timer = setTimeout(() => child.kill('SIGTERM'), 55000);
    child.on('error', () => {clearTimeout(timer); reject(new Error('OMP bridge unavailable'));});
    child.stdin.on('error', () => {});
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', data => {output += data;});
    child.on('close', code => {
      clearTimeout(timer);
      try {
        const value = JSON.parse(output);
        if (value.error === 'ModelSelectionError' && typeof value.message === 'string') reject(new Error(value.message));
        else if (code || value.error) reject(new Error(`OMP bridge action failed (${value.error || 'process_exit'}); reconcile exact task`));
        else resolve(value);
      } catch {reject(new Error('OMP bridge response unavailable; do not replay start'));}
    });
    child.stdin.end(JSON.stringify(request));
  });
}

export function createAdapter(pi, {request = bridgeRequest, db = process.env.CALL_CLI_DB, pollMs = 1000} = {}) {
  const bindings = new Map();
  const pendingBindings = new Map();
  let generation = 0;
  const identity = ctx => ({session_id: ctx.sessionManager.getSessionId(), agent_id: ctx.agent.id});
  const key = ctx => JSON.stringify(identity(ctx));
  const live = (binding, ctx) => generation === binding.generation && key(ctx) === binding.key && bindings.get(binding.key) === binding;
  const call = (binding, args) => request({db, host: binding.host, capability: binding.capability, args});

  function schedule(binding, ctx) {
    if (!live(binding, ctx) || binding.timer || binding.reading || ![...binding.tasks.values()].some(track => track.active)) return;
    binding.timer = ctx.setTimeout(async () => {
      binding.timer = undefined;
      binding.reading = true;
      try {
        for (const [taskId, track] of binding.tasks) {
          if (!track.active) continue;
          const revision = track.revision;
          try {
            const packet = await call(binding, {action: 'wait', task_id: taskId, after: track.cursor, timeout: 0});
            if (!live(binding, ctx)) return;
            if (binding.tasks.get(taskId) !== track || track.revision !== revision) continue;
            const events = packet.events.filter(event => event.seq > track.cursor);
            const answers = [...track.answers, ...events.filter(event => event.kind === 'answer')];
            const stage = events.findLast(event => event.kind === 'stage' && !event.quiet_notice);
            const meaningful = events.filter(event => visible.has(event.kind) && event.kind !== 'answer'
              && (event.kind !== 'stage' || event.quiet_notice === true || (event === stage && event.text !== track.lastStage))
              && !(event.kind === 'unknown' && (event.turn ?? packet.task?.turn) !== undefined
                && track.attentionTurns.has(event.turn ?? packet.task?.turn)));
            if (events.some(event => terminal.has(event.kind) || event.kind === 'waiting_reply')) {
              meaningful.push(...answers);
              meaningful.sort((a, b) => a.seq - b.seq);
            }
            if (packet.task?.id === taskId && ['agy', 'omp'].includes(packet.task.agent) && packet.task.state === 'unknown'
              && !events.some(event => event.kind === 'unknown' && (event.turn ?? packet.task.turn) === packet.task.turn)
              && !track.attentionTurns.has(packet.task.turn)) {
              meaningful.push({kind: 'unknown', turn: packet.task.turn, attention_required: true,
                notification_scope: 'originating-session',
                source: 'task_snapshot', text: packet.task.settlement_note || `${packet.task.agent.toUpperCase()} outcome unknown`,
                next_action: 'Inspect this exact task and artifacts now; preserve HOLD. Do not replay automatically; reconcile before continuation.',
                delivery: 'origin follow-up queued; actual model consumption unverified'});
            }
            if (meaningful.length) {
              pi.sendMessage({customType: 'org.callcli.events', attribution: 'agent', display: true,
                content: JSON.stringify({task_id: taskId, events: meaningful})},
              {deliverAs: 'followUp', triggerTurn: true});
              for (const notified of meaningful) {
                if (notified.kind !== 'unknown') continue;
                const turn = notified.turn ?? packet.task?.turn;
                track.attentionTurns.add(turn);
              }
            }
            if (stage) track.lastStage = stage.text;
            track.answers = events.some(event => terminal.has(event.kind) || event.kind === 'waiting_reply') ? [] : answers;
            // Advance only the batch admitted by this live host, never a stdout tail.
            if (events.length) track.cursor = events.at(-1).seq;
            // Source cancellation can settle after advance; the next wait starts the authorized target.
            if (packet.task && (terminal.has(packet.task.state) || packet.task.state === 'waiting_reply')
              && !(packet.task.state === 'cancelled' && packet.task.intervention?.status === 'pending')) track.active = false;
          } catch (error) {
            if (!live(binding, ctx)) return;
            if (binding.tasks.get(taskId) !== track || track.revision !== revision) continue;
            track.active = false;
            pi.sendMessage({customType: 'org.callcli.error', attribution: 'agent', display: true,
              content: `Call CLI event read failed for ${taskId} (${error instanceof TypeError ? 'invalid_response' : error instanceof Error && error.message.startsWith('OMP bridge') ? error.message : 'delivery_error'}). Only this task's delivery is paused; no task was cancelled or replayed. Use exact task status to reconcile.`},
            {deliverAs: 'followUp', triggerTurn: true});
          }
        }
      } finally {
        binding.reading = false;
        schedule(binding, ctx);
      }
    }, pollMs);
  }

  return {
    async execute(args, ctx) {
      if (args && Object.hasOwn(args, 'timeout')) throw new Error('call_cli_omp does not accept timeout; omit it. Explicit execution budgets remain available through CLI/Python start.');
      if (args && Object.hasOwn(args, 'clear_timeout') && !['reply', 'intervene'].includes(args.action)) throw new Error('clear_timeout requires reply or intervene');
      if (!args || Object.keys(args).some(name => !fields.has(name)) || !actions.has(args.action)) throw new Error('Unsupported action or host field');
      if (args.action === 'dialogue' && (!args.command_id || !Number.isInteger(args.expected_turn) || args.expected_turn < 1)) throw new Error('dialogue requires expected_turn and stable command_id');
      const bindingKey = key(ctx);
      let binding = bindings.get(bindingKey);
      if (!binding) {
        if (!['start', 'list'].includes(args.action)) throw new Error('Current host session is not bound');
        let pending = pendingBindings.get(bindingKey);
        if (!pending) {
          const expectedGeneration = generation;
          const host = identity(ctx);
          pending = (async () => {
            const result = await request({db, action: 'bind', host});
            if (expectedGeneration !== generation || key(ctx) !== bindingKey) throw new Error('Host session changed during binding');
            const bound = {key: bindingKey, host, capability: result.capability, generation, ctx, tasks: new Map(), timer: undefined};
            bindings.set(bindingKey, bound);
            return bound;
          })();
          pendingBindings.set(bindingKey, pending);
        }
        try {binding = await pending;}
        finally {if (pendingBindings.get(bindingKey) === pending) pendingBindings.delete(bindingKey);}
      }
      const result = await call(binding, args);
      if (!live(binding, ctx)) throw new Error('Host session changed; action outcome must be reconciled, not replayed');
      if (result.task) {
        const id = result.task.id;
        if (args.action === 'close') {
          binding.tasks.delete(id);
          if (binding.timer && ![...binding.tasks.values()].some(track => track.active)) {
            binding.ctx.clearTimer(binding.timer);
            binding.timer = undefined;
          }
        }
        else if (['start', 'reply', 'dialogue', 'permission', 'intervene', 'cancel', 'status'].includes(args.action)) {
          const track = binding.tasks.get(id) || {cursor: 0, active: false, answers: [], revision: 0, attentionTurns: new Set()};
          track.revision++;
          track.active = true;
          binding.tasks.set(id, track);
        }
      }
      if (args.action === 'events' && binding.tasks.has(args.task_id)) {
        const track = binding.tasks.get(args.task_id);
        if ((args.after || 0) === track.cursor && result.events.length) track.cursor = result.events.at(-1).seq;
      }
      schedule(binding, ctx);
      return result;
    },
    detach() {
      generation++;
      for (const binding of bindings.values()) {
        if (binding.timer) binding.ctx?.clearTimer(binding.timer);
        binding.tasks.clear();
      }
      bindings.clear();
      pendingBindings.clear();
    },
  };
}

export default function register(pi) {
  const adapter = createAdapter(pi);
  const z = pi.zod;
  pi.registerTool({name: 'call_cli_omp', label: 'Call CLI', description: 'Coordinate an exact task from this OMP host session. Host identity is injected, never model supplied. Events return as queued follow-ups; not steer.',
    parameters: z.object({action: z.enum([...actions]), task_id: z.string().optional(), agent: z.enum(['omp', 'codex', 'pi', 'agy']).optional(),
      device: z.string().optional(), cwd: z.string().optional(), prompt: z.string().optional(), write: z.boolean().optional(), resource: z.string().optional(),
      clear_timeout: z.boolean().optional().describe('reply/intervene only: clear an inherited execution limit on the next settled continuation; queued clearing is refused'),
      model: z.string().optional().describe('AGY oc/oa/4c/4a/4h: bare=off, s=native, t=unsupported; omitted=ocs, never task-selected. Advertised IDs supported; shorthand/agy_swarm conflict rejects'), effort: z.string().optional(), agy_approval: z.enum(['approve-all', 'approve-reads']).optional(), remote_shell: z.enum(['posix', 'powershell']).optional(),
      agy_swarm: z.enum(['native', 'off']).optional().describe('AGY route override for advertised IDs; must match shorthand: bare=off, s=native'), agy_worker_model: z.string().optional(),
      agy_mode: z.enum(['single', 'swarm', 'dynamic']).optional().describe('Explicit execution mode; Dynamic uses advertised model IDs and exact script approval'),
      permission_id: z.string().optional(), script_sha256: z.string().optional(), decision: z.enum(['allow_once', 'reject_once']).optional(),
      skills: z.array(z.object({path: z.string(), mode: z.enum(['required', 'optional', 'reference']), location: z.enum(['origin', 'target']).optional()})).optional(),
      question_id: z.string().optional(), command_id: z.string().optional().describe('Stable intent ID; required for dialogue, reuse on retry'), expected_turn: z.number().int().positive().optional().describe('Observed turn; required for dialogue'), after: z.number().optional()}).strict(),
    async execute(_id, args, _signal, _onUpdate, ctx) {
      const result = await adapter.execute(args, ctx);
      return {content: [{type: 'text', text: JSON.stringify(result)}]};
    }});
  for (const event of ['session_before_switch', 'session_before_branch', 'session_before_tree', 'session_shutdown']) pi.on(event, () => adapter.detach());
}
