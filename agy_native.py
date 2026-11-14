"""AGY-only ACP owner. Observes native background work without driving it."""
import json
import os
import selectors
import subprocess
import time
import hashlib

try:
    from .events import EventDecoder, safe_tool_id, safe_tool_name, safe_tool_kind
    from .transport import command, protocol_prompt
    from .skills import stage, guidance
    from .updates import device_lock
except ImportError:
    from events import EventDecoder, safe_tool_id, safe_tool_name, safe_tool_kind
    from transport import command, protocol_prompt
    from skills import stage, guidance
    from updates import device_lock


class ProtocolError(RuntimeError):
    pass


class NativeOwner:
    """One stdio connection, one outstanding request, no concurrent attachment."""
    def __init__(self, task):
        self.task = task
        self.session_id = None
        self.model_config_value = None
        self.created_session = False
        self.prompt_returned = False
        self.serial = 0
        self.buffer = b''
        self.permission_decisions = []
        self.review = None
        self.review_blocked = False
        self.available_commands = set()
        self.permission_handler = None
        self.workflow_identity = {}
        self.approval_wait_seconds = 0.0
        self.proc = subprocess.Popen(command(task['device'], task.get('shell', 'posix'),
            ['agy', 'acp', '--disable-telemetry']), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
            cwd=task['cwd'] if task['device'] == 'local' else None)
        self.poller = selectors.DefaultSelector()
        self.poller.register(self.proc.stdout, selectors.EVENT_READ)

    def send(self, value):
        self.proc.stdin.write((json.dumps(dict(jsonrpc='2.0', **value))+'\n').encode())
        self.proc.stdin.flush()

    def notify_cancel(self):
        self.send(dict(method='session/cancel', params=dict(sessionId=self.session_id)))

    def request(self, method, params, *, timeout=60, check_cancel=None, on_update=None):
        self.serial += 1
        ident = self.serial
        if method is not None:
            self.send(dict(id=ident, method=method, params=params))
        updates = []
        workflow_calls = set()
        try:
            from .agy import fatal_error
        except ImportError:
            from agy import fatal_error
        public_text = ''
        def review():
            if not cancel_sent and not getattr(self, 'review_blocked', False) and self.review is not None:
                self.review()
        deadline = time.monotonic()+timeout if timeout is not None else None
        cancel_sent = False
        self.request_cancel_sent = False
        while True:
            if method is None and params['command'] in self.available_commands:
                return {}, updates
            if check_cancel and check_cancel() and not cancel_sent:
                self.notify_cancel()
                cancel_sent = True
                self.request_cancel_sent = True
                deadline = time.monotonic()+25
            if deadline is not None and time.monotonic() >= deadline:
                if method is None:
                    return {}, updates
                raise ProtocolError('Native ACP control did not settle; effects unknown')
            if b'\n' not in self.buffer:
                if not self.poller.select(.1):
                    review()
                    continue
                chunk = os.read(self.proc.stdout.fileno(), 65536)
                if not chunk:
                    raise ProtocolError('Native ACP disconnected; effects unknown')
                self.buffer += chunk
                continue
            line, self.buffer = self.buffer.split(b'\n', 1)
            try:
                frame = json.loads(line)
            except (ValueError, UnicodeError):
                raise ProtocolError('Invalid native ACP frame') from None
            if frame.get('method') and 'id' in frame:
                if method is None:
                    self.send(dict(id=frame['id'],result=dict(outcome=dict(outcome='cancelled'))))
                    raise ProtocolError('Unexpected callback during capability observation; no prompt')
                waited = getattr(self,'approval_wait_seconds',0.0)
                self._callback(frame)
                if deadline is not None:
                    deadline += getattr(self,'approval_wait_seconds',0.0)-waited
            elif frame.get('method') == 'session/update':
                if self.session_id and frame.get('params', {}).get('sessionId') != self.session_id:
                    raise ProtocolError('Native update session mismatch')
                raw = frame.get('params', {}).get('update', {})
                if (self.task.get('agy_mode')=='dynamic' and getattr(self,'close_attempted',False)
                        and raw.get('sessionUpdate')!='available_commands_update'):
                    raise ProtocolError('Dynamic activity during unload; retain HOLD')
                if raw.get('sessionUpdate') == 'available_commands_update':
                    self.available_commands = {entry['name'] for entry in raw.get('availableCommands', [])
                        if isinstance(entry, dict) and isinstance(entry.get('name'), str)}
                if self.task.get('agy_mode')=='dynamic':
                    self.validate_workflow_update(raw, workflow_calls)
                update = normalize(frame.get('params', {}).get('update', {}))
                if update is not None:
                    updates.append(update)
                    if update['sessionUpdate'] == 'agent_message_chunk':
                        public_text = (public_text+update['content']['text'])[-64000:]
                        if fatal_error(public_text):
                            # Suppress ordinary reminders without publishing unverified load history.
                            self.review_blocked = True
                    if on_update is not None:
                        on_update(update)
            elif frame.get('id') == ident and not frame.get('method'):
                if 'error' in frame:
                    error = frame['error']
                    self.last_rpc_error = error
                    # Exact structured not-found is used only as close readback.
                    if (error.get('code') == -32602 and error.get('message') in
                            {'Session not found', 'Session not found: '+str(self.session_id)}):
                        raise ProtocolError('session_not_found')
                    raise ProtocolError('Native ACP request rejected: '+method)
                if not isinstance(frame.get('result'), dict):
                    raise ProtocolError('Native ACP omitted structured result')
                if method == 'session/prompt':
                    self.prompt_returned = True
                return frame['result'], updates
            review()

    def validate_workflow_update(self, raw, calls):
        if raw.get('sessionUpdate') not in {'tool_call','tool_call_update'}:
            return
        ident = safe_tool_id(raw.get('toolCallId'))
        title = raw.get('name') or raw.get('title')
        known = self.workflow_identity.get(ident)
        if title!='run_workflow' and known is None:
            return
        if not ident or (title is not None and title!='run_workflow'):
            raise ProtocolError('Dynamic workflow identity conflict; retain HOLD')
        if raw['sessionUpdate']=='tool_call':
            if ident in calls:
                raise ProtocolError('Dynamic repeated workflow creation; retain HOLD')
            calls.add(ident)
        hashes = []
        for key in ('args','rawInput'):
            if key not in raw:
                continue
            value = raw[key]
            if not isinstance(value,dict):
                raise ProtocolError('Dynamic argument evidence type unsupported; script identity unverified, retain HOLD')
            if 'Script' in value:
                if not isinstance(value['Script'],str):
                    raise ProtocolError('Dynamic Script type conflict; retain HOLD')
                hashes.append(hashlib.sha256(value['Script'].encode()).hexdigest())
        if len(set(hashes))>1 or (known is not None and hashes and known!=hashes[0]):
            raise ProtocolError('Dynamic raw Script changed or conflicted; retain HOLD')
        if hashes:
            self.workflow_identity[ident] = hashes[0]

    def _callback(self, frame):
        if frame['method'] != 'session/request_permission':
            self.send(dict(id=frame['id'], error=dict(code=-32601, message='Client operation unavailable')))
            return
        params = frame.get('params', {})
        root = params.get('sessionId') == self.session_id
        tool = params.get('toolCall', {})
        if self.task.get('agy_mode')=='dynamic' and getattr(self,'close_attempted',False):
            self.send(dict(id=frame['id'],result=dict(outcome=dict(outcome='cancelled'))))
            raise ProtocolError('Dynamic permission during unload rejected; retain HOLD')
        if (root and self.task.get('agy_mode') == 'dynamic' and self.permission_handler is not None
                and (tool.get('name') or tool.get('title'))=='run_workflow'):
            self.permission_handler(frame)
            return
        allow = root and (self.task.get('agy_approval') == 'approve-all' or tool.get('kind') in {'read', 'search'})
        wanted = 'allow_once' if allow else 'reject_once'
        kinds = {'allow_once','allow_always','reject_once','reject_always'}
        option_kinds = sorted({o.get('kind') for o in params.get('options', []) if o.get('kind') in kinds})
        option = next((o.get('optionId') for o in params.get('options', []) if o.get('kind') == wanted), None) if root else None
        outcome = dict(outcome='selected', optionId=option) if option else dict(outcome='cancelled')
        diagnostic = dict(rpc_id=frame['id'] if type(frame.get('id')) is int else None,
            scope='root' if root else 'foreign', tool=safe_tool_id(tool.get('toolCallId')),
            kind=safe_tool_kind(tool.get('kind')), option_kinds=option_kinds, allowed=allow and bool(option),
            choice=wanted if option else 'cancelled', reason=('foreign_session' if not root else
                'selected' if option else wanted+'_unavailable'), response_sent_at=None)
        self.permission_decisions.append(diagnostic)
        try:
            self.send(dict(id=frame['id'], result=dict(outcome=outcome)))
            diagnostic['response_sent_at']=time.time()
        except (OSError, ValueError):
            diagnostic['send_error']='permission_response_failed'
            raise ProtocolError('Permission response send failed; effects unknown') from None
        if not root:
            raise ProtocolError('Permission session mismatch')

    def initialize(self):
        info, _ = self.request('initialize', dict(protocolVersion=1,
            clientCapabilities=dict(fs=dict(readTextFile=False, writeTextFile=False), terminal=False),
            clientInfo=dict(name='call-cli-native-owner', version='1')))
        caps = info.get('agentCapabilities', {})
        if not caps.get('loadSession') or 'close' not in caps.get('sessionCapabilities', {}):
            raise ProtocolError('Native ACP load/close capability required')
        return info

    def new(self):
        result, _ = self.request('session/new', self.workspace())
        sid = result.get('sessionId')
        if not isinstance(sid, str) or not sid:
            raise ProtocolError('Native ACP session identity missing')
        self.session_id = sid
        self.created_session = True
        self.model_config_value = next((o.get('currentValue') for o in result.get('configOptions', []) if o['id'] == 'model'), None)
        return result

    def workspace(self):
        return dict(cwd=self.task['cwd'], mcpServers=[])

    def load(self):
        result, updates = self.request('session/load', dict(sessionId=self.session_id, **self.workspace()))
        self.last_load_updates = updates
        if result.get('sessionId') != self.session_id:
            raise ProtocolError('Native load identity mismatch')
        return result, snapshot(updates)

    def select(self, metadata):
        config = {o['id']: o for o in metadata.get('configOptions', [])}
        catalog = {o.get('modelId') for o in metadata.get('models', {}).get('availableModels', [])}
        families = {o.get('value') for o in config.get('model', {}).get('options', [])}
        efforts = {o.get('value') for o in config.get('thought_level', {}).get('options', [])}
        current_effort = config.get('thought_level', {}).get('currentValue')
        def resolve(requested, intent, effort=None):
            if requested in catalog:
                if requested in families:
                    if effort is not None:
                        raise ProtocolError('Standalone advertised model has no effort mapping; no prompt')
                    return requested, requested, None
                pairs = [(family, level) for family in families for level in efforts
                         if family+'-'+level == requested]
                if len(pairs) != 1:
                    raise ProtocolError('Exact model has no unambiguous advertised config mapping')
                family, level = pairs[0]
                if effort is not None and level != effort:
                    raise ProtocolError('Explicit model and effort disagree')
                return requested, family, level
            family = requested if requested is not None else intent
            level = effort if effort is not None else current_effort
            if family not in families or level not in efforts:
                raise ProtocolError('Model family or effort is not advertised; no fallback or prompt')
            matches = [exact for exact in catalog if exact == family+'-'+str(level)]
            if len(matches) != 1:
                raise ProtocolError('Model family cannot resolve uniquely in advertised catalog')
            exact = matches[0]
            pairs = [(candidate, effort_value) for candidate in families for effort_value in efforts
                     if candidate+'-'+effort_value == exact]
            if pairs != [(family, level)]:
                raise ProtocolError('Exact model has no unambiguous advertised config mapping')
            return exact, family, level
        requested = {key:self.task.get(key) for key in ('model', 'effort', 'agy_worker_model')}
        coordinator = self.task.get('model')
        if not efforts and not (coordinator in catalog and coordinator in families):
            if coordinator in catalog:
                candidates = [candidate for candidate in families if coordinator.startswith(candidate+'-')]
                if len(candidates) != 1:
                    raise ProtocolError('Coordinator exact model has ambiguous advertised config family; no prompt')
                coordinator_family = candidates[0]
            else:
                coordinator_family = coordinator if coordinator is not None else 'gemini-4-carbon'
            if coordinator_family not in families:
                raise ProtocolError('Coordinator family is not advertised; no fallback or prompt')
            self.model_config_value = coordinator_family
            self.request('session/set_config_option', dict(sessionId=self.session_id, configId='model', value=coordinator_family))
            coordinator_metadata, _ = self.load()
            coordinator_config = {option['id']: option for option in coordinator_metadata.get('configOptions', [])}
            if coordinator_config.get('model', {}).get('currentValue') != coordinator_family:
                raise ProtocolError('Coordinator catalog configuration readback mismatch; no prompt')
            efforts = {option.get('value') for option in coordinator_config.get('thought_level', {}).get('options', [])}
            current_effort = coordinator_config.get('thought_level', {}).get('currentValue')
        model, family, effort = resolve(self.task.get('model'), 'gemini-4-carbon', self.task.get('effort'))
        if not efforts and self.task.get('agy_mode') != 'dynamic':
            worker_requested=self.task.get('agy_worker_model')
            if worker_requested in catalog and worker_requested not in families:
                candidates=[f for f in families if worker_requested.startswith(f+'-')]
                if len(candidates)!=1:
                    raise ProtocolError('Worker exact model has ambiguous advertised config family; no prompt')
                worker_family=candidates[0]
            else:
                worker_family=worker_requested or 'gemini-4-hydrogen'
            if worker_family not in families:
                raise ProtocolError('Worker family is not advertised; no fallback or prompt')
            self.model_config_value=worker_family
            self.request('session/set_config_option',dict(sessionId=self.session_id,configId='model',value=worker_family))
            worker_metadata, _ = self.load()
            worker_config={o['id']:o for o in worker_metadata.get('configOptions',[])}
            if worker_config.get('model',{}).get('currentValue')!=worker_family:
                raise ProtocolError('Worker catalog configuration readback mismatch; no prompt')
            efforts={o.get('value') for o in worker_config.get('thought_level',{}).get('options',[])}
            current_effort=worker_config.get('thought_level',{}).get('currentValue')
        worker = None if self.task.get('agy_mode') == 'dynamic' else resolve(self.task.get('agy_worker_model'), 'gemini-4-hydrogen')[0]
        self.model_config_value = family
        self.request('session/set_config_option', dict(sessionId=self.session_id, configId='model', value=family))
        if effort is not None:
            self.request('session/set_config_option', dict(sessionId=self.session_id, configId='thought_level', value=effort))
        readback, _ = self.load()
        actual = {o['id']: o for o in readback.get('configOptions', [])}
        if (readback.get('models', {}).get('currentModelId') != model
                or actual.get('model', {}).get('currentValue') != family
                or (effort is not None and actual.get('thought_level', {}).get('currentValue') != effort)):
            raise ProtocolError('Native exact coordinator selection readback mismatch')
        self.task.update(model=model, effort=effort, agy_worker_model=worker)
        if self.task.get('agy_mode') == 'dynamic':
            return dict(model=model, effort=effort, native_selection=dict(requested=requested,
                resolved=dict(model=model, effort=effort), readback_verified=True))
        return dict(model=model, effort=effort, agy_worker_model=worker,
                    native_selection=dict(requested=requested,
                        resolved=dict(model=model, effort=effort, agy_worker_model=worker), readback_verified=True))

    def close_session(self):
        if getattr(self,'close_attempted',False):
            raise ProtocolError('Native unload already attempted; reconcile existing evidence, no close replay')
        self.close_attempted=True
        self.request('session/close', dict(sessionId=self.session_id))
        try:
            self.request('session/set_config_option', dict(sessionId=self.session_id, configId='model', value=self.model_config_value))
        except ProtocolError as exc:
            if str(exc) == 'session_not_found':
                return True
            raise
        raise ProtocolError('Native session unload not independently confirmed')

    def stop(self, *, force=True):
        self.poller.close()
        self.proc.stdin.close()
        kind = 'natural'
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            if not force:
                self.proc.stdout.close()
                return dict(kind='stdio-closed-exit-unverified',returncode=self.proc.returncode,
                    pid=self.proc.pid,forced=False,recovery='unsupported-exit-unproven-no-owner',worker_fate='unknown')
            kind = 'terminate-fallback'
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                kind = 'kill-fallback'
                self.proc.kill()
                self.proc.wait(timeout=2)
        self.proc.stdout.close()
        if not force:
            return dict(kind=kind,returncode=self.proc.returncode,pid=self.proc.pid,
                forced=False,recovery='unsupported-owner-exited',worker_fate='unknown')
        return dict(kind=kind, returncode=self.proc.returncode)


def normalize(update):
    kind = update.get('sessionUpdate')
    if kind in {'user_message_chunk', 'agent_message_chunk'}:
        content = update.get('content', {})
        if content.get('type') != 'text':
            raise ProtocolError('Non-text history requires reconciliation')
        return dict(sessionUpdate=kind, content=dict(type='text', text=content.get('text', '')))
    if kind not in {'tool_call', 'tool_call_update'}:
        return None
    value = dict(sessionUpdate='tool_call', toolCallId=safe_tool_id(update.get('toolCallId')))
    value['tool_event'] = kind
    if 'name' in update or 'title' in update:
        value['title'] = safe_tool_name(update.get('name') or update.get('title'))
    for key in ('kind', 'status'):
        if key in update:
            value[key] = update[key]
    args = update.get('args', update.get('rawInput', {}))
    if value.get('title')=='run_workflow' and isinstance(args,dict) and isinstance(args.get('Script'),str):
        value['workflow_script_sha256'] = hashlib.sha256(args['Script'].encode()).hexdigest()
    if value.get('title')=='swarm' or kind=='tool_call_update':
        value['status_args_invalid'] = (not isinstance(args,dict) or
            any(k not in {'action','name'} or not isinstance(v,str) for k,v in args.items()))
    if value.get('title') == 'swarm' and isinstance(args, dict):
        value['swarm'] = {k: args[k] for k in ('action', 'name', 'model') if isinstance(args.get(k), str)}
    elif kind == 'tool_call_update' and isinstance(args, dict):
        value['swarm_update'] = {k: args[k] for k in ('action', 'name', 'model') if isinstance(args.get(k), str)}
    return value


def snapshot(updates):
    result, tools = [], {}
    for value in updates:
        if value['sessionUpdate'] == 'tool_call':
            key = value['toolCallId']
            if not key:
                raise ProtocolError('Tool history omitted identity')
            if key in tools:
                old=tools[key]
                if 'kind' in value and 'kind' in old and value['kind']!=old['kind']:
                    raise ProtocolError('Native tool identity changed; reconciliation required')
                identity=value.get('swarm',value.get('swarm_update',{}) if 'swarm' in old else {})
                if any(k in old.get('swarm',{}) and v!=old['swarm'][k] for k,v in identity.items()):
                    raise ProtocolError('Native tool identity changed; reconciliation required')
                if identity:
                    old['swarm']=dict(old.get('swarm',{}),**identity)
                old.update({k:v for k,v in value.items() if k not in {'tool_event','swarm','swarm_update','status_args_invalid'}})
            else:
                tools[key] = {k:v for k,v in value.items() if k not in {'tool_event','swarm_update','status_args_invalid'}}
                result.append(tools[key])
        elif result and result[-1]['sessionUpdate'] == value['sessionUpdate']:
            result[-1]['content']['text'] += value['content']['text']
        else:
            result.append(json.loads(json.dumps(value)))
    return result


def additions(previous, current):
    if len(current) < len(previous):
        raise ProtocolError('Native history shrank; reconciliation required')
    changed = []
    for index, old in enumerate(previous):
        new = current[index]
        if old == new:
            continue
        if old['sessionUpdate'] == new['sessionUpdate'] == 'tool_call' and old['toolCallId'] == new['toolCallId']:
            if any(old.get(k) != new.get(k) for k in ('kind', 'swarm')):
                raise ProtocolError('Native tool history changed; reconciliation required')
            if old.get('status') != new.get('status'):
                changed.append(new)
        elif (index == len(previous)-1 and old['sessionUpdate'] == new['sessionUpdate']
              and old['sessionUpdate'] in {'agent_message_chunk', 'user_message_chunk'}
              and new['content']['text'].startswith(old['content']['text'])):
            changed.append(dict(sessionUpdate=new['sessionUpdate'], content=dict(type='text',
                text=new['content']['text'][len(old['content']['text']):])))
        else:
            raise ProtocolError('Native history prefix changed; reconciliation required')
    return changed+current[len(previous):]


def first_replay(published, replay):
    """Live deltas and persisted replay have different chunk/tool metadata shapes."""
    tools = {e['toolCallId']: e for e in published if e['sessionUpdate'] == 'tool_call'}
    messages = [e['content']['text'] for e in published
                if e['sessionUpdate'] == 'agent_message_chunk' and e['content']['text']]
    message_index, message_offset = 0, 0
    fresh = []
    for entry in replay:
        if entry['sessionUpdate'] == 'tool_call' and entry['toolCallId'] in tools:
            old = tools[entry['toolCallId']]
            merged = dict(old, **entry)
            if any(merged.get(key) != old.get(key) for key in ('status', 'kind', 'swarm')):
                fresh.append(merged)
            continue
        if entry['sessionUpdate'] == 'agent_message_chunk':
            text = entry['content']['text']
            consumed = 0
            while consumed < len(text) and message_index < len(messages):
                old = messages[message_index]
                count = min(len(text)-consumed, len(old)-message_offset)
                if text[consumed:consumed+count] != old[message_offset:message_offset+count]:
                    raise ProtocolError('Native live message history changed; reconciliation required')
                consumed += count
                message_offset += count
                if message_offset == len(old):
                    message_index += 1
                    message_offset = 0
            if consumed == len(text):
                continue
            entry = dict(sessionUpdate=entry['sessionUpdate'], content=dict(type='text', text=text[consumed:]))
        fresh.append(entry)
    if message_index != len(messages):
        raise ProtocolError('Native live message history changed; reconciliation required')
    return fresh


def completion(history, task, record_result=None, *, owner=None, baseline=None):
    if task.get('questions') or task.get('question_id'):
        return None
    workers, killed = {}, set()
    envelope = None
    legacy_attributed = False
    fresh_first_submission = (owner is not None and baseline == [] and task.get('turn') == 1
        and owner.task.get('id') == task['id'] and owner.task.get('turn') == 1
        and bool(task.get('native_session_id')) and owner.session_id == task['native_session_id']
        and owner.created_session and owner.prompt_returned)
    for entry in history:
        if entry['sessionUpdate'] == 'tool_call':
            action = entry.get('swarm', {})
            if action.get('action') == 'add':
                workers[action.get('name')] = (action.get('model'), entry.get('status'))
                killed.discard(action.get('name'))
            if action.get('action') == 'kill' and entry.get('status') == 'completed':
                if action.get('name') in workers:
                    killed.add(action['name'])
        elif entry['sessionUpdate'] == 'agent_message_chunk':
            for line in entry['content']['text'].splitlines():
                if line.startswith('CALLCLI_RESULT '):
                    try:
                        candidate = json.loads(line[len('CALLCLI_RESULT '):])
                        if isinstance(candidate, dict):
                            explicit = candidate.get('turn') == task.get('turn')
                            legacy = 'turn' not in candidate and fresh_first_submission
                            if explicit or legacy:
                                if task.get('interaction_seq', 0) and candidate.get('interaction_seq') != task['interaction_seq']:
                                    continue
                                envelope = candidate
                                legacy_attributed = legacy
                    except ValueError:
                        return None  # Last background message may still be streaming.
    if envelope is None:
        return None
    if (not isinstance(envelope, dict) or envelope.get('task') != task['id'] or envelope.get('status') != 'completed'
            or not isinstance(envelope.get('result'), str) or not envelope['result'].strip()
            or not isinstance(envelope.get('workers'), list)
            or any(not isinstance(name, str) or not name for name in envelope['workers'])
            or set(envelope['workers']) != set(workers)):
        raise ProtocolError('Task completion envelope does not reconcile with native roster')
    if record_result is not None:
        result = (dict(envelope, turn=task['turn'], turn_attribution='fresh-owner-first-submission',
                       original_envelope=envelope) if legacy_attributed else envelope)
        record_result(result)
    if (not workers or None in workers or any(model != task['agy_worker_model'] or status != 'completed'
                           for model, status in workers.values()) or set(workers) != killed):
        raise ProtocolError('Final result lacks matching completed native worker add/kill/model events; outcome unknown')
    return sorted(workers)


def pending_question(decoder):
    line = decoder.pending.lstrip()
    marker = 'CALLCLI_QUESTION '
    return bool(line) and (line.startswith(marker.rstrip()) or marker.startswith(line))


def brief(task, policy):
    return ('/swarm\n'+protocol_prompt(policy+guidance(task)+
        'Use native swarm, not another scheduler. You coordinate; explicitly select '+task['agy_worker_model']+
        ' for execution workers (verify against native swarm models before adding). Do not silently substitute models. '
        'Assign disjoint ownership and scoped acceptance; workers must report to @coordinator, without recursive spawning. '
        'Native board mentions wake you automatically; no external polling prompts will arrive. '
        'Land and verify authorized results before removing workers. Collect native kill receipts for every worker; waiting is not stopped. '
        'Only after acceptance and cleanup emit one line CALLCLI_RESULT with JSON fields task="'+task['id']+
        '", turn='+str(task.get('turn', 1))+', status="completed", result=<actual result>, workers=<complete worker name list>. '
        'The envelope is not independent cleanup proof. If blocked, report a numbered question or actual failure.\n\n'+task['prompt']))


def cancellation_workers(known, history, model, *, require_kills=True):
    known_adds = {e['toolCallId'] for e in known if e.get('swarm', {}).get('action')=='add'}
    projected=snapshot(history)
    status_ids={e['toolCallId'] for e in projected if e.get('swarm',{}).get('action')=='status'}
    completed_kills = set()
    seen_tools = set()
    for entry in history:
        if entry.get('sessionUpdate')=='tool_call':
            ident=entry['toolCallId']
            if ident in status_ids and entry.get('status_args_invalid'):
                raise ProtocolError('Native swarm status parameters invalid; retain HOLD')
            if entry.get('tool_event')=='tool_call_update':
                if ident not in seen_tools and (entry.get('swarm') or entry.get('swarm_update')):
                    raise ProtocolError('Cancellation identity update precedes tool call; unsupported ordering, retain HOLD')
            else:
                seen_tools.add(ident)
        action = entry.get('swarm', {})
        name = action.get('name')
        if action.get('action')=='kill' and entry.get('status')=='completed':
            completed_kills.add(name)
        elif (action.get('action')=='add' and name in completed_kills
                and entry.get('tool_event')!='tool_call_update'):
            raise ProtocolError('Cancellation re-add generation ambiguous after kill; retain HOLD')
    workers, killed = {}, set()
    for entry in projected:
        action = entry.get('swarm', {})
        name = action.get('name')
        if action.get('action') == 'add':
            if (entry['toolCallId'] not in known_adds or not name or name in workers
                    or entry.get('status')!='completed' or action.get('model')!=model):
                raise ProtocolError('Cancellation roster changed or add/model incomplete; retain HOLD')
            workers[name] = entry['toolCallId']
        elif action.get('action') == 'kill':
            if not name or name not in workers or entry.get('status')!='completed':
                raise ProtocolError('Cancellation kill missing matching completed worker; retain HOLD')
            killed.add(name)
    if not workers or (require_kills and set(workers)!=killed):
        raise ProtocolError('Cancellation lacks matching completed worker kills; retain HOLD')
    return sorted(workers)


def run_native(store, task, emit, policy):
    try:
        from .backends import TurnResult
    except ImportError:
        from backends import TurnResult
    try:
        from .updates import ensure_current
    except ImportError:
        from updates import ensure_current
    try:
        from .agy import fatal_error
    except ImportError:
        from agy import fatal_error
    if 'version_check' not in task:
        checked = ensure_current(store, task)
        emit('version', json.dumps(checked))
    owner = None
    submitted = False
    cleanup_token = None
    baseline = None
    foreground_ended = False
    cancellation_unverified = False
    try:
        with device_lock(task):
            if store.get(task['id'])['state'] == 'cancelling':
                return TurnResult('cancelled', 0, 'cancelled', 'Cancelled before native owner launch')
            owner = NativeOwner(task)
            binding = (task['id'], task['turn'], task['launch_id'], task.get('control_epoch', 1))
            cleanup_token = store.native_cleanup_owner(*binding)
            def unload(context):
                if getattr(owner,'close_attempted',False):
                    raise ProtocolError('Native unload already attempted; retain existing observation, no replay')
                observation=dict(task=task['id'],session_id=owner.session_id,turn=task['turn'],
                    launch_id=task['launch_id'],epoch=task.get('control_epoch',1),context=context,
                    attempted=True,verified=False,reason_code='unload_in_progress')
                if not store.native_owner_diagnostics(*binding,native_unload_observation=observation):
                    raise ProtocolError('Native unload owner identity stale; retain HOLD')
                try:
                    verified=owner.close_session()
                except ProtocolError:
                    observation['reason_code']='unload_unverified'
                    store.native_owner_diagnostics(*binding,native_unload_observation=observation)
                    raise
                observation.update(verified=verified is True,reason_code='unload_verified' if verified is True else 'unload_unverified')
                store.native_owner_diagnostics(*binding,native_unload_observation=observation)
                return verified
            def check_cancel():
                if store.get(task['id'])['state']=='cancelling':
                    store.native_cancel_seal(*binding)
                    return True
                return False
            def cancel_cleanup(known, notify=True, first=False):
                nonlocal cancellation_unverified
                cancellation_unverified = True
                current = store.native_cancel_seal(*binding)
                if notify:
                    owner.notify_cancel()
                if baseline:
                    raise ProtocolError('Cancellation prior-session roster provenance unverified; retain HOLD')
                _, final = owner.load()
                if first:
                    first_replay(known, final)
                    replay_tools={e['toolCallId']:e for e in final if e['sessionUpdate']=='tool_call'}
                    if any(e['toolCallId'] not in replay_tools or any(e.get(key)!=replay_tools[e['toolCallId']].get(key)
                            for key in ('kind','swarm') if key in e)
                           for e in known if e['sessionUpdate']=='tool_call'):
                        raise ProtocolError('Native cancellation live tool history changed; retain HOLD')
                else:
                    additions(known, final)
                if any(c.get('kind')=='dialogue' and c.get('status') in {'claimed','send_started','unknown'}
                       for c in current.get('commands', {}).values()):
                    raise ProtocolError('Cancellation dialogue send outcome uncertain; retain HOLD')
                workers = cancellation_workers(known, owner.last_load_updates, task['agy_worker_model'], require_kills=False)
                killed={e.get('swarm',{}).get('name') for e in final
                    if e.get('swarm',{}).get('action')=='kill' and e.get('status')=='completed'}
                if set(workers)!=killed:
                    if not foreground_ended or any(e.get('status') in {'pending','in_progress'} for e in final
                            if e['sessionUpdate']=='tool_call'):
                        raise ProtocolError('Cancellation cleanup requires end_turn and no active tools; retain HOLD')
                    cancel_id=current['native_cancel_id']
                    header=dict(task=task['id'],session_id=owner.session_id,turn=task['turn'],
                        launch_id=task['launch_id'],epoch=task.get('control_epoch',1),cancel_id=cancel_id,workers=workers)
                    body=('CALLCLI_CANCEL_CLEANUP '+json.dumps(header)+'\n'
                        'Revoke all earlier business instructions, including any instruction not to remove workers. '
                        'Stop business now. Kill the exact known workers listed above using native swarm kill; '
                        'native swarm status is allowed only to observe cleanup. '
                        'Do not add, re-add, replace, spawn, or start new business. Do not repeat /swarm. '
                        'Do not access files, shell or network. Preserve existing approval policy. '
                        'Report cleanup failure honestly; no business result or CALLCLI_RESULT can settle cancellation.')
                    store.native_cancel_cleanup_state(*binding,cancel_id,'send_started',workers)
                    prior_adds={e['toolCallId']:sum(1 for item in owner.last_load_updates
                        if item.get('swarm',{}).get('action')=='add' and item['toolCallId']==e['toolCallId']
                        and item.get('tool_event')!='tool_call_update')
                        for e in final if e.get('swarm',{}).get('action')=='add'}
                    permission_start=len(owner.permission_decisions)
                    try:
                        response, _ = owner.request('session/prompt',dict(sessionId=owner.session_id,
                            prompt=[dict(type='text',text=body)]),timeout=60)
                        if response.get('stopReason')!='end_turn':
                            raise ProtocolError('Cancellation cleanup did not return end_turn; retain HOLD, no replay')
                        if any(not decision.get('allowed') for decision in owner.permission_decisions[permission_start:]):
                            raise ProtocolError('Cancellation cleanup permission refused; retain HOLD')
                        store.native_cancel_cleanup_state(*binding,cancel_id,'returned',workers)
                    except (ProtocolError,OSError) as exc:
                        store.native_cancel_cleanup_state(*binding,cancel_id,'unknown',workers)
                        if isinstance(exc,ProtocolError): raise
                        raise ProtocolError('Cancellation cleanup send failed; effects unknown, no replay') from None
                    _, cleaned = owner.load()
                    for ident,count in prior_adds.items():
                        if sum(1 for e in owner.last_load_updates if e.get('swarm',{}).get('action')=='add'
                                and e['toolCallId']==ident and e.get('tool_event')!='tool_call_update')!=count:
                            raise ProtocolError('Cancellation cleanup add/re-add generation changed; retain HOLD')
                    delta=additions(final,cleaned)
                    status_ids=set()
                    seen_ids=set()
                    for raw in owner.last_load_updates:
                        if raw['sessionUpdate']!='tool_call' or raw['toolCallId'] in seen_ids:
                            continue
                        seen_ids.add(raw['toolCallId'])
                        action=raw.get('swarm',{})
                        if (raw.get('tool_event','tool_call')=='tool_call' and raw.get('title')=='swarm'
                                and safe_tool_id(raw['toolCallId'])==raw['toolCallId'] and raw['toolCallId']
                                and action.get('action')=='status' and set(action)<= {'action','name'}
                                and ('name' not in action or action['name'] in workers)):
                            status_ids.add(raw['toolCallId'])
                    if any(e['sessionUpdate']=='tool_call' and not (
                            (e.get('swarm',{}).get('action')=='kill' and e['swarm'].get('name') in workers)
                            or (e['toolCallId'] in status_ids and e.get('swarm',{}).get('action')=='status'
                                and set(e['swarm'])<= {'action','name'}
                                and ('name' not in e['swarm'] or e['swarm']['name'] in workers)
                                and e.get('kind') in {'other','read'} and e.get('status')=='completed'))
                            for e in delta):
                        raise ProtocolError('Cancellation cleanup performed unverified work; retain HOLD')
                    final=cleaned
                workers = cancellation_workers(known, owner.last_load_updates, task['agy_worker_model'])
                store.native_cancel_seal(*binding)
                unloaded = unload('cancellation')
                store.update(task['id'], native_cancel=dict(session_unloaded=unloaded, workers=workers))
                receipt = dict(kind='verified-cancellation', task=task['id'], session_id=owner.session_id,
                    turn=task['turn'], launch_id=task['launch_id'], epoch=task.get('control_epoch',1),
                    cancel_id=current['native_cancel_id'], workers=workers, session_unloaded=True,
                    worker_events=[e for e in snapshot(owner.last_load_updates) if e.get('swarm',{}).get('action') in {'add','kill'}],
                    all_descendants_proven_stopped=False, evidence='complete-native-add-kill-and-unload')
                store.native_cleanup_commit(*binding, cleanup_token, receipt)
                cancellation_unverified = False
                return TurnResult('cancelled', 0, 'cancelled', 'Cancellation cleanup verified; business result not claimed')
            info = owner.initialize()
            store.update(task['id'], native_agent=info.get('agentInfo', {}), native_phase='initializing')
            if task.get('native_session_id'):
                owner.session_id = task['native_session_id']
                metadata, baseline = owner.load()
            else:
                store.update(task['id'], session_creation_attempted=True)
                metadata = owner.new()
                store.update(task['id'], native_session_id=owner.session_id, needs_session=False)
                _, baseline = owner.load()
            if task.get('agy_mode') == 'dynamic' and 'run-workflow' not in owner.available_commands:
                owner.request(None, dict(command='run-workflow'), timeout=1)
            if task.get('agy_mode') == 'dynamic' and 'run-workflow' not in owner.available_commands:
                raise ProtocolError('Dynamic run-workflow command not advertised; no prompt or fallback')
            selected = owner.select(metadata)
            task = store.update(task['id'], **selected)
            if owner.created_session:
                _, baseline = owner.load()
            emit('model', json.dumps(selected))
            task = stage(store, task, emit)
            binding = (task['id'], task['turn'], task['launch_id'], task.get('control_epoch', 1))
            store.native_input_open(*binding)
            current_interaction = 0
            public_emit = emit
            def emit(kind, text, **extra):
                interaction = extra.pop('interaction_seq', current_interaction)
                public_emit(kind, text, interaction_seq=interaction, **extra)
            if not store.native_prompt_begin(*binding):
                store.native_cancel_seal(*binding)
                unload('preprompt-cancellation')
                if not owner.created_session or baseline != []:
                    raise ProtocolError('Preprompt cancellation prior roster unverified; retain HOLD')
                store.native_cleanup_commit(*binding,cleanup_token,dict(kind='fresh-no-prompt',task=task['id'],
                    session_id=owner.session_id,turn=task['turn'],launch_id=task['launch_id'],epoch=task.get('control_epoch',1),
                    fresh=True,baseline_empty=True,no_prompt=True,workers=[],session_unloaded=True,
                    all_descendants_proven_stopped=False))
                return TurnResult('cancelled',0,'cancelled')
            if task.get('agy_mode') == 'dynamic':
                try:
                    from .agy_dynamic import run_dynamic
                except ImportError:
                    from agy_dynamic import run_dynamic
                submitted = True
                cancellation_unverified = True
                return run_dynamic(store, task, owner, emit, policy, binding, cleanup_token, baseline, unload, check_cancel)
            decoder = EventDecoder()
            live_updates = []
            def publish_update(entry):
                live_updates.append(entry)
                for kind, text in decoder.feed(dict(params=dict(update=entry))):
                    emit(kind, text)
            last_notice = time.monotonic()
            def review():
                nonlocal last_notice
                now = time.monotonic()
                if (not owner.review_blocked and now-last_notice >= 600
                        and store.get(task['id'])['state'] == 'running'):
                    emit('stage', 'Coordinator review requested: native work pending; no cancellation or prompt sent.', quiet_notice=True)
                    last_notice = now
            # One cooldown across foreground, dialogue and passive observation requests.
            owner.review = review
            submitted = True
            response, _ = owner.request('session/prompt', dict(sessionId=owner.session_id,
                prompt=[dict(type='text', text=brief(task, policy))]), timeout=None,
                check_cancel=check_cancel, on_update=publish_update)
            foreground_ended = response.get('stopReason')=='end_turn' and not owner.request_cancel_sent
            if check_cancel():
                return cancel_cleanup(snapshot(live_updates), notify=not owner.request_cancel_sent, first=True)
            if response.get('stopReason') != 'end_turn':
                for kind, text in decoder.finish():
                    emit(kind, text)
                unloaded = unload('foreground-abnormal')
                store.update(task['id'], native_cancel=dict(foreground=response.get('stopReason'),
                             session_unloaded=unloaded, workers='unknown'))
                raise ProtocolError('Native foreground settled '+str(response.get('stopReason'))+'; whole-swarm fate unknown')
            store.update(task['id'], native_phase='background_pending', native_foreground_stop='end_turn')
            for kind, text in decoder.finish():
                emit(kind, text)
            if fatal_error(decoder.text):
                raise ProtocolError('AGY reported a fatal execution error; outcome unknown, no automatic replay')
            previous_public = snapshot([e for e in live_updates if e['sessionUpdate'] != 'user_message_chunk'])
            previous = None
            last_semantic_change = None
            dialogue_public = None
            deferred_questions = []
            blocker_notice = None
            while True:
                if check_cancel():
                    return cancel_cleanup(previous if previous is not None else snapshot(live_updates), first=previous is None)
                _, history = owner.load()
                if check_cancel():
                    return cancel_cleanup(previous if previous is not None else snapshot(live_updates), first=previous is None)
                comparison = previous
                recovered = []
                if previous is not None and deferred_questions:
                    comparison = list(previous)
                    for deferred in list(deferred_questions):
                        index = deferred['index']
                        if (index >= len(history) or history[index]['sessionUpdate'] != 'agent_message_chunk'
                                or not history[index]['content']['text'].startswith(deferred['text'])):
                            raise ProtocolError('Deferred question history ambiguous; retain HOLD and inspect originating interaction '+str(deferred['interaction_seq']))
                        value = history[index]['content']['text']
                        suffix = value[len(deferred['text']):]
                        recovered.append((deferred, suffix, bool(suffix) and index+1 < len(history)
                            and history[index+1]['sessionUpdate'] in {'tool_call', 'user_message_chunk'}))
                        comparison[index] = history[index]
                turn_history = additions(baseline, history)
                public_history = turn_history
                fresh = first_replay(previous_public, public_history) if previous is None else additions(comparison, history)
                if dialogue_public is not None:
                    fresh = first_replay(dialogue_public, fresh)
                    dialogue_public = None
                for deferred, suffix, boundary in recovered:
                    update = dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text=suffix))
                    for kind, text in deferred['decoder'].feed(dict(params=dict(update=update))):
                        emit(kind, text, interaction_seq=deferred['interaction_seq'])
                    if boundary:
                        for kind, text in deferred['decoder'].finish():
                            emit(kind, text, interaction_seq=deferred['interaction_seq'])
                    deferred['text'] = history[deferred['index']]['content']['text']
                    if not deferred['decoder'].pending:
                        deferred_questions.remove(deferred)
                appended = history[len(previous if previous is not None else baseline):]
                for entry in fresh:
                    for kind, text in decoder.feed(dict(params=dict(update=entry)), text_boundary=entry in appended):
                        emit(kind, text)
                previous = history
                now = time.time()
                if fresh or any(suffix or boundary for _, suffix, boundary in recovered) or last_semantic_change is None:
                    last_semantic_change = now
                # Persist only reconciled successful loads, never partial RPC updates.
                store.update(task['id'], native_snapshot=[e for e in history if e['sessionUpdate'] != 'user_message_chunk'],
                    native_permission_decisions=owner.permission_decisions,
                    native_observation=dict(turn=task['turn'], last_success_at=now,
                        last_semantic_change_at=last_semantic_change,
                        pending_tools=[dict(id=safe_tool_id(e['toolCallId']), name=safe_tool_name(e.get('title')), kind=safe_tool_kind(e.get('kind')),
                            status=e['status']) for e in history if e['sessionUpdate']=='tool_call'
                            and e.get('status') in {'pending', 'in_progress'}],
                        question_ids=store.get(task['id']).get('questions', []), input_available=True,
                        question_blockers=[dict(interaction_seq=d['interaction_seq'], reason='incomplete_question',
                            available_actions=['observe', 'dialogue', 'cancel'], blocks_completion=True)
                            for d in deferred_questions]+([dict(interaction_seq=current_interaction,
                                reason='incomplete_question', available_actions=['observe', 'dialogue', 'cancel'],
                                blocks_completion=True)] if pending_question(decoder) else [])))
                blockers = store.get(task['id'])['native_observation']['question_blockers']
                signature = tuple(b['interaction_seq'] for b in blockers)
                if signature and signature != blocker_notice:
                    public_emit('stage', 'Native question fragment incomplete; observe this owner for a complete boundary, '
                        'or explicitly dialogue/cancel after review. Completion remains blocked; no partial question published.',
                        interaction_seq=signature[0], notice_kind='incomplete_question', question_blockers=blockers)
                blocker_notice = signature
                current_text = '\n'.join(e['content']['text'] for e in turn_history if e['sessionUpdate'] == 'agent_message_chunk')
                if fatal_error(current_text):
                    raise ProtocolError('AGY reported a fatal execution error; outcome unknown, no automatic replay')
                entry = store.native_dialogue_claim(*binding)
                if entry is not None:
                    if pending_question(decoder):
                        # An explicit input is not evidence that a background tail finished.
                        index = len(history)-1
                        if index < 0 or history[index]['sessionUpdate'] != 'agent_message_chunk':
                            raise ProtocolError('Deferred question location ambiguous; retain HOLD and inspect current interaction')
                        deferred = EventDecoder()
                        deferred.pending, deferred.pending_overflow = decoder.pending, decoder.pending_overflow
                        deferred_questions.append(dict(interaction_seq=current_interaction, index=index,
                            text=history[index]['content']['text'], decoder=deferred))
                        decoder.pending = ''
                        decoder.pending_overflow = False
                    else:
                        for kind, text in decoder.finish():
                            emit(kind, text)
                    current_interaction = entry['interaction_seq']
                    dialogue_live = []
                    def publish_dialogue(update):
                        dialogue_live.append(update)
                        for kind, text in decoder.feed(dict(params=dict(update=update))):
                            emit(kind, text)
                    emit('stage', 'Dialogue '+entry['command_id']+' claimed; not yet sent.')
                    body_header = 'CALLCLI_DIALOGUE '+json.dumps(dict(task=task['id'], turn=task['turn'],
                        interaction_seq=current_interaction, command_id=entry['command_id'],
                        question_id=entry.get('question_id'), question=entry.get('question')))
                    body_header += (' Keep the current session and roster; do not repeat /swarm or replace workers. '
                        'Report dispatch separately from coordinator-reported ACK and artifact evidence. '
                        'Any final CALLCLI_RESULT must include this task, turn and interaction_seq; resolve remaining questions first.')
                    store.native_dialogue_state(*binding, entry['command_id'], 'send_started')
                    foreground_ended = False
                    response, _ = owner.request('session/prompt', dict(sessionId=owner.session_id,
                        prompt=[dict(type='text', text=body_header+'\n\n'+entry['prompt'])]), timeout=None,
                        check_cancel=check_cancel, on_update=publish_dialogue)
                    foreground_ended = response.get('stopReason')=='end_turn' and not owner.request_cancel_sent
                    if response.get('stopReason') != 'end_turn':
                        if check_cancel():
                            if response.get('stopReason')=='cancelled':
                                store.native_dialogue_state(*binding,entry['command_id'],'cancelled')
                            return cancel_cleanup(history)
                        raise ProtocolError('Native dialogue did not return end_turn; effects unknown')
                    for kind, text in decoder.finish():
                        emit(kind, text)
                    dialogue_public = snapshot([e for e in dialogue_live if e['sessionUpdate']!='user_message_chunk'])
                    store.native_dialogue_state(*binding, entry['command_id'], 'returned')
                    emit('stage', 'Dialogue '+entry['command_id']+' returned; worker adoption requires public ACK/artifact evidence.')
                    continue
                workers = (None if deferred_questions or pending_question(decoder) else
                    completion(turn_history, store.get(task['id']), owner=owner, baseline=baseline))
                if workers is not None:
                    if not store.native_input_seal(*binding, current_interaction):
                        continue
                    completion(turn_history, store.get(task['id']), lambda result: store.update(task['id'], native_result=result),
                               owner=owner, baseline=baseline)
                    unload('completion')
                    store.update(task['id'], native_cleanup=dict(turn=task['turn'], workers=workers,
                        evidence='native-add-kill-completed-and-session-unload', session_unloaded=True,
                        all_descendants_proven_stopped=False), native_phase='settled')
                    for kind, text in decoder.finish():
                        emit(kind, text)
                    return TurnResult('completed', 0, 'end_turn')
                review()
                # Observation interval, never an execution deadline or generation prompt.
                for _ in range(20):
                    current = store.get(task['id'])
                    if current['state'] == 'cancelling' or any(c.get('kind')=='dialogue' and c.get('status')=='accepted'
                            for c in current.get('commands', {}).values()):
                        break
                    time.sleep(.1)
    except ProtocolError as exc:
        if 'deferred_questions' in locals() and deferred_questions:
            observation = store.get(task['id']).get('native_observation', {})
            reason = ('history_ambiguous' if str(exc).startswith('Deferred question history ambiguous;')
                      else 'unresolved_question')
            observation['question_blockers'] = [dict(interaction_seq=d['interaction_seq'], reason=reason,
                available_actions=['inspect_origin', 'reconcile_hold'], blocks_completion=True) for d in deferred_questions]
            store.update(task['id'], native_observation=observation)
        if owner is not None and owner.session_id and not submitted:
            try:
                unload('preprompt-failure')
                if owner.created_session and baseline == [] and cleanup_token:
                    store.native_cleanup_commit(*binding, cleanup_token, dict(kind='fresh-no-prompt', task=task['id'],
                        session_id=owner.session_id, turn=task['turn'], launch_id=task['launch_id'],
                        epoch=task.get('control_epoch',1), workers=[], no_prompt=True, fresh=True, baseline_empty=True,
                        session_unloaded=True, all_descendants_proven_stopped=False))
            except ProtocolError:
                pass  # Original failure remains authoritative; cleanup unproven.
        store.update(task['id'], native_phase='unknown', native_workers='unknown' if submitted else 'not_submitted')
        return TurnResult('unknown', None, None, str(exc))
    finally:
        if 'binding' in locals():
            try:
                store.native_input_end(*binding)
            except ValueError:
                pass  # A stale owner cannot mutate its successor or release fence.
        if owner is not None:
            exit_receipt = owner.stop(force=False) if cancellation_unverified else owner.stop()
            try:
                store.native_owner_diagnostics(*binding,native_owner_exit=exit_receipt,
                                               native_permission_decisions=owner.permission_decisions)
            except ValueError:
                pass  # Closed/replaced identity cannot be reopened by final diagnostics.
