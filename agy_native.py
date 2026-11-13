"""AGY-only ACP owner. Observes native background work without driving it."""
import json
import os
import selectors
import subprocess
import time

try:
    from .events import EventDecoder, safe_tool_id, safe_tool_name
    from .transport import command, protocol_prompt
    from .skills import stage, guidance
    from .updates import device_lock
except ImportError:
    from events import EventDecoder, safe_tool_id, safe_tool_name
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
        self.send(dict(id=ident, method=method, params=params))
        updates = []
        deadline = time.monotonic()+timeout if timeout is not None else None
        cancel_sent = False
        while True:
            if check_cancel and check_cancel() and not cancel_sent:
                self.notify_cancel()
                cancel_sent = True
                deadline = time.monotonic()+25
            if deadline is not None and time.monotonic() >= deadline:
                raise ProtocolError('Native ACP control did not settle; effects unknown')
            if b'\n' not in self.buffer:
                if not self.poller.select(.1):
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
                self._callback(frame)
            elif frame.get('method') == 'session/update':
                if self.session_id and frame.get('params', {}).get('sessionId') != self.session_id:
                    raise ProtocolError('Native update session mismatch')
                update = normalize(frame.get('params', {}).get('update', {}))
                if update is not None:
                    updates.append(update)
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

    def _callback(self, frame):
        if frame['method'] != 'session/request_permission':
            self.send(dict(id=frame['id'], error=dict(code=-32601, message='Client operation unavailable')))
            return
        params = frame.get('params', {})
        if params.get('sessionId') != self.session_id:
            raise ProtocolError('Permission session mismatch')
        tool = params.get('toolCall', {})
        allow = self.task.get('agy_approval') == 'approve-all' or tool.get('kind') in {'read', 'search'}
        wanted = 'allow_once' if allow else 'reject_once'
        option = next((o.get('optionId') for o in params.get('options', []) if o.get('kind') == wanted), None)
        outcome = dict(outcome='selected', optionId=option) if option else dict(outcome='cancelled')
        self.permission_decisions.append(dict(tool=safe_tool_id(tool.get('toolCallId')), allowed=allow and bool(option)))
        self.send(dict(id=frame['id'], result=dict(outcome=outcome)))

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
            if requested is None:
                family = intent
                level = effort or current_effort
                exact = family+'-'+str(level)
                if family not in families or exact not in catalog:
                    raise ProtocolError('Default model intent cannot resolve in advertised catalog')
                return exact, family, level
            if requested not in catalog:
                raise ProtocolError('Explicit model must be an exact advertised ID; no fallback or prompt')
            pairs = [(family, level) for family in families for level in efforts
                     if family+'-'+level == requested]
            if len(pairs) != 1:
                raise ProtocolError('Exact model has no unambiguous advertised config mapping')
            family, level = pairs[0]
            if effort is not None and level != effort:
                raise ProtocolError('Explicit model and effort disagree')
            return requested, family, level
        model, family, effort = resolve(self.task.get('model'), 'gemini-4-carbon', self.task.get('effort'))
        worker, _, _ = resolve(self.task.get('agy_worker_model'), 'gemini-4-hydrogen')
        self.model_config_value = family
        self.request('session/set_config_option', dict(sessionId=self.session_id, configId='model', value=family))
        self.request('session/set_config_option', dict(sessionId=self.session_id, configId='thought_level', value=effort))
        readback, _ = self.load()
        actual = {o['id']: o for o in readback.get('configOptions', [])}
        if (readback.get('models', {}).get('currentModelId') != model
                or actual.get('model', {}).get('currentValue') != family
                or actual.get('thought_level', {}).get('currentValue') != effort):
            raise ProtocolError('Native exact coordinator selection readback mismatch')
        self.task.update(model=model, effort=effort, agy_worker_model=worker)
        return dict(model=model, effort=effort, agy_worker_model=worker)

    def close_session(self):
        self.request('session/close', dict(sessionId=self.session_id))
        try:
            self.request('session/set_config_option', dict(sessionId=self.session_id, configId='model', value=self.model_config_value))
        except ProtocolError as exc:
            if str(exc) == 'session_not_found':
                return True
            raise
        raise ProtocolError('Native session unload not independently confirmed')

    def stop(self):
        self.poller.close()
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)
        self.proc.stdout.close()


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
    if 'name' in update or 'title' in update:
        value['title'] = safe_tool_name(update.get('name') or update.get('title'))
    for key in ('kind', 'status'):
        if key in update:
            value[key] = update[key]
    args = update.get('args', update.get('rawInput', {}))
    if value.get('title') == 'swarm' and isinstance(args, dict):
        value['swarm'] = {k: args[k] for k in ('action', 'name', 'model') if isinstance(args.get(k), str)}
    return value


def snapshot(updates):
    result, tools = [], {}
    for value in updates:
        if value['sessionUpdate'] == 'tool_call':
            key = value['toolCallId']
            if not key:
                raise ProtocolError('Tool history omitted identity')
            if key in tools:
                tools[key].update(value)
            else:
                tools[key] = dict(value)
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
            if any(old.get(k) != new.get(k) for k in ('title', 'kind', 'swarm')):
                raise ProtocolError('Native tool history changed; reconciliation required')
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
    tools = {e['toolCallId'] for e in published if e['sessionUpdate'] == 'tool_call'}
    messages = [e['content']['text'] for e in published if e['sessionUpdate'] == 'agent_message_chunk']
    fresh = []
    for entry in replay:
        if entry['sessionUpdate'] == 'user_message_chunk':
            continue
        if entry['sessionUpdate'] == 'tool_call' and entry['toolCallId'] in tools:
            continue
        if entry['sessionUpdate'] == 'agent_message_chunk':
            text = entry['content']['text']
            if text in messages:
                messages.remove(text)  # One occurrence, never global text-hash dedupe.
                continue
            if messages and text.startswith(messages[0]):
                entry = dict(sessionUpdate=entry['sessionUpdate'], content=dict(type='text', text=text[len(messages.pop(0)):]))
        fresh.append(entry)
    return fresh


def completion(history, task, record_result=None, *, owner=None, baseline=None):
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
    try:
        with device_lock(task):
            if store.get(task['id'])['state'] == 'cancelling':
                return TurnResult('cancelled', 0, 'cancelled', 'Cancelled before native owner launch')
            owner = NativeOwner(task)
            info = owner.initialize()
            store.update(task['id'], native_agent=info.get('agentInfo', {}), native_phase='initializing')
            if task.get('native_session_id'):
                owner.session_id = task['native_session_id']
                metadata, baseline = owner.load()
            else:
                store.update(task['id'], session_creation_attempted=True)
                metadata = owner.new()
                store.update(task['id'], native_session_id=owner.session_id, needs_session=False)
                baseline = []
            selected = owner.select(metadata)
            task = store.update(task['id'], **selected)
            if owner.created_session:
                _, baseline = owner.load()
            emit('model', json.dumps(selected))
            task = stage(store, task, emit)
            if store.get(task['id'])['state'] == 'cancelling':
                owner.close_session()
                store.update(task['id'], native_cleanup=dict(turn=task['turn'], workers=[], session_unloaded=True, no_prompt=True))
                return TurnResult('cancelled', 0, 'cancelled')
            store.update(task['id'], native_phase='foreground', native_prompt_submitted=True)
            decoder = EventDecoder()
            live_updates = []
            def publish_update(entry):
                live_updates.append(entry)
                for kind, text in decoder.feed(dict(params=dict(update=entry))):
                    emit(kind, text)
            submitted = True
            response, _ = owner.request('session/prompt', dict(sessionId=owner.session_id,
                prompt=[dict(type='text', text=brief(task, policy))]), timeout=None,
                check_cancel=lambda: store.get(task['id'])['state'] == 'cancelling', on_update=publish_update)
            if response.get('stopReason') != 'end_turn':
                for kind, text in decoder.finish():
                    emit(kind, text)
                unloaded = owner.close_session()
                store.update(task['id'], native_cancel=dict(foreground=response.get('stopReason'),
                             session_unloaded=unloaded, workers='unknown'))
                raise ProtocolError('Native foreground settled '+str(response.get('stopReason'))+'; whole-swarm fate unknown')
            store.update(task['id'], native_phase='background_pending', native_foreground_stop='end_turn')
            for kind, text in decoder.finish():
                emit(kind, text)
            if fatal_error(decoder.text):
                raise ProtocolError('AGY reported a fatal execution error; outcome unknown, no automatic replay')
            if store.get(task['id'])['question_id']:
                raise ProtocolError('Native question recovered; worker settlement unproven; occupancy retained, no automatic continuation')
            previous_public = snapshot([e for e in live_updates if e['sessionUpdate'] != 'user_message_chunk'])
            previous = None
            last_notice = time.monotonic()
            while True:
                if store.get(task['id'])['state'] == 'cancelling':
                    owner.notify_cancel()
                    owner.close_session()
                    store.update(task['id'], native_cancel=dict(foreground='already_end_turn', session_unloaded=True, workers='unknown'))
                    raise ProtocolError('Background cancellation requested; native worker stop unproven; occupancy retained')
                _, history = owner.load()
                if baseline:
                    additions(baseline, history)
                turn_history = history[len(baseline):]
                public_history = [entry for entry in turn_history if entry['sessionUpdate'] != 'user_message_chunk']
                fresh = first_replay(previous_public, public_history) if previous is None else additions(previous, history)
                for entry in fresh:
                    for kind, text in decoder.feed(dict(params=dict(update=entry))):
                        emit(kind, text)
                previous = history
                # User material is needed only for prefix reconciliation, not public status.
                store.update(task['id'], native_snapshot=[e for e in history if e['sessionUpdate'] != 'user_message_chunk'],
                             native_permission_decisions=owner.permission_decisions)
                current_text = '\n'.join(e['content']['text'] for e in turn_history if e['sessionUpdate'] == 'agent_message_chunk')
                if fatal_error(current_text):
                    raise ProtocolError('AGY reported a fatal execution error; outcome unknown, no automatic replay')
                workers = completion(turn_history, task, lambda result: store.update(task['id'], native_result=result),
                                     owner=owner, baseline=baseline)
                if workers is not None:
                    if store.get(task['id'])['state'] == 'cancelling':
                        raise ProtocolError('Cancellation raced native completion; reconcile before release')
                    owner.close_session()
                    store.update(task['id'], native_cleanup=dict(turn=task['turn'], workers=workers,
                        evidence='native-add-kill-completed-and-session-unload', session_unloaded=True,
                        all_descendants_proven_stopped=False), native_phase='settled')
                    for kind, text in decoder.finish():
                        emit(kind, text)
                    return TurnResult('completed', 0, 'end_turn')
                if time.monotonic()-last_notice >= 600:
                    emit('stage', 'Coordinator review requested: native background pending; no cancellation or prompt sent.', quiet_notice=True)
                    last_notice = time.monotonic()
                # Observation interval, never an execution deadline or generation prompt.
                for _ in range(20):
                    if store.get(task['id'])['state'] == 'cancelling':
                        break
                    time.sleep(.1)
    except ProtocolError as exc:
        if 'decoder' in locals():
            for kind, text in decoder.finish():
                emit(kind, text)
        if owner is not None and owner.session_id and not submitted:
            try:
                owner.close_session()
                store.update(task['id'], native_cleanup=dict(turn=task['turn'], workers=[], session_unloaded=True, no_prompt=True))
            except ProtocolError:
                pass  # Original failure remains authoritative; cleanup unproven.
        store.update(task['id'], native_phase='unknown', native_workers='unknown' if submitted else 'not_submitted')
        return TurnResult('unknown', None, None, str(exc))
    finally:
        if owner is not None:
            owner.stop()
