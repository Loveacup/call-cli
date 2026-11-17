"""AGY-only ACP owner. Observes native background work without driving it."""
import json
import re
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
    def __init__(self, message, *, history_diagnostic=None):
        super().__init__(message)
        self.history_diagnostic = history_diagnostic


class NativeOwner:
    """One stdio connection, one outstanding request, no concurrent attachment."""
    def __init__(self, task):
        self.task = task
        self.session_id = None
        self.model_config_value = None
        self.created_session = False
        self.prompt_returned = False
        self.request_cancel_sent = False
        self.serial = 0
        self.buffer = b''
        self.permission_decisions = []
        self.review = None
        self.review_blocked = False
        self.available_commands = set()
        self.permission_handler = None
        self.workflow_identity = {}
        self.approval_wait_seconds = 0.0
        self.pending_drained = []
        self.drain_workflow_calls = set()
        self.drained_public_text = ''
        self.drained_line_pending = ''
        self.last_load_drained = 0
        self.last_load_retried = False
        self.pending_retry_state = None
        self.fatal_observed = False
        self.history_degraded = False
        self.last_history_diagnostic = None
        self.resumed_prompt_boundary = None
        self.last_rpc_phase = 'setup'
        skip = (task.get('execution_policy') == 'native-skip'
                or task.get('agy_approval') == 'skip-permissions')
        argv = ['agy', 'acp', '--disable-telemetry']
        if skip:
            argv.append('--dangerously-skip-permissions')
        self.proc = subprocess.Popen(command(task['device'], task.get('shell', 'posix'),
            argv), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
            cwd=task['cwd'] if task['device'] == 'local' else None)
        self.poller = selectors.DefaultSelector()
        self.poller.register(self.proc.stdout, selectors.EVENT_READ)

    def send(self, value):
        self.proc.stdin.write((json.dumps(dict(jsonrpc='2.0', **value))+'\n').encode())
        self.proc.stdin.flush()

    def notify_cancel(self):
        if getattr(self, 'request_cancel_sent', False):
            return
        # A failed send has unknown effects; never replay it on this owner.
        self.request_cancel_sent = True
        self.last_rpc_phase = 'session/cancel'
        self.send(dict(method='session/cancel', params=dict(sessionId=self.session_id)))

    def request(self, method, params, *, timeout=60, check_cancel=None, on_update=None):
        drain_mode = (method is None and params is None)
        self.last_rpc_phase = method or ('drain' if drain_mode else 'capability_observation')
        ident = None
        if method is not None:
            self.serial += 1
            ident = self.serial
            self.send(dict(id=ident, method=method, params=params))
        updates = []
        checked_updates = []
        workflow_calls = getattr(self, 'drain_workflow_calls', set()) if drain_mode else set()
        try:
            from .agy import AGY_FATAL_LINE, fatal_error
        except ImportError:
            from agy import AGY_FATAL_LINE, fatal_error
        public_text = ''
        line_pending = ''
        load_boundary = getattr(self, 'resumed_prompt_boundary', None) if method == 'session/load' else None
        load_after_boundary = (load_boundary is None)
        def review():
            if not cancel_sent and not getattr(self, 'review_blocked', False) and self.review is not None:
                self.review()
        deadline = None if drain_mode else (time.monotonic()+timeout if timeout is not None else None)
        cancel_sent = False
        while True:
            if drain_mode and len(updates) >= 4096:
                return {}, updates
            if method is None and isinstance(params, dict) and params.get('command') in self.available_commands:
                return {}, updates
            if check_cancel and check_cancel() and not cancel_sent:
                self.notify_cancel()
                cancel_sent = True
                if drain_mode:
                    return {}, updates
                deadline = time.monotonic()+25
            if deadline is not None and time.monotonic() >= deadline:
                if method is None:
                    return {}, updates
                raise ProtocolError('Native ACP control did not settle; effects unknown')
            if b'\n' not in self.buffer:
                if not self.poller.select(0 if drain_mode else .1):
                    if drain_mode:
                        return {}, updates
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
                if method is None and not drain_mode:
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
                if load_boundary is not None and raw.get('sessionUpdate') == 'user_message_chunk':
                    raw_content = raw.get('content')
                    utext = raw_content.get('text', '') if isinstance(raw_content, dict) else ''
                    if isinstance(utext, str) and (utext == load_boundary or utext.endswith(load_boundary)):
                        load_after_boundary = True
                        workflow_calls = set()
                        public_text = ''
                        line_pending = ''
                        checked_updates = []
                if self.task.get('agy_mode')=='dynamic' and load_after_boundary:
                    self.validate_workflow_update(raw, workflow_calls)
                update = normalize(frame.get('params', {}).get('update', {}))
                if update is not None:
                    updates.append(update)
                    if load_after_boundary:
                        checked_updates.append(update)
                        if update['sessionUpdate'] == 'agent_message_chunk':
                            chunk = update['content']['text']
                            public_text = (public_text+chunk)[-64000:]
                            if fatal_error(public_text):
                                # Suppress ordinary reminders without publishing unverified load history.
                                self.review_blocked = True
                            parts = (line_pending + chunk).split('\n')
                            for seg in parts[:-1]:
                                if seg.strip() == AGY_FATAL_LINE:
                                    self.review_blocked = True
                                    if method in {'session/prompt', 'session/load'}:
                                        self.fatal_observed = True
                            line_pending = parts[-1][-64000:]
                        elif update['sessionUpdate'] in {'tool_call', 'user_message_chunk'}:
                            if line_pending.strip() == AGY_FATAL_LINE:
                                self.review_blocked = True
                                if method in {'session/prompt', 'session/load'}:
                                    self.fatal_observed = True
                            line_pending = ''
                    if on_update is not None:
                        on_update(update)
            elif 'id' in frame and not frame.get('method'):
                if method is None:
                    raise ProtocolError('Unexpected native ACP response')
                if frame.get('id') == ident:
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
                    if method in {'session/prompt', 'session/load'} and load_after_boundary and (
                        line_pending.strip() == AGY_FATAL_LINE
                        or fatal_error(public_text)
                        or _has_completed_fatal_line(checked_updates, require_terminated=False)
                    ):
                        self.review_blocked = True
                        self.fatal_observed = True
                    return frame['result'], updates
            if not drain_mode:
                review()

    def drain_pending(self, *, check_cancel=None):
        try:
            from .agy import AGY_FATAL_LINE, fatal_error
        except ImportError:
            from agy import AGY_FATAL_LINE, fatal_error
        _, updates = self.request(None, None, timeout=0, check_cancel=check_cancel)
        line_pending = getattr(self, 'drained_line_pending', '')
        for update in updates:
            self.pending_drained.append(update)
            if update['sessionUpdate'] == 'agent_message_chunk':
                chunk = update['content']['text']
                self.drained_public_text = (self.drained_public_text + chunk)[-64000:]
                if fatal_error(self.drained_public_text):
                    self.review_blocked = True
                parts = (line_pending + chunk).split('\n')
                for seg in parts[:-1]:
                    if seg.strip() == AGY_FATAL_LINE:
                        self.review_blocked = True
                        self.fatal_observed = True
                line_pending = parts[-1][-64000:]
                if line_pending.strip() == AGY_FATAL_LINE:
                    self.review_blocked = True
            elif update['sessionUpdate'] in {'tool_call', 'user_message_chunk'}:
                if line_pending.strip() == AGY_FATAL_LINE:
                    self.review_blocked = True
                    self.fatal_observed = True
                line_pending = ''
        self.drained_line_pending = line_pending
        return updates

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
            if 'ScriptPath' in value:
                raise ProtocolError('Dynamic ScriptPath workflow unsupported; script identity unverified, retain HOLD')
            if 'Script' in value:
                if not isinstance(value['Script'], str):
                    raise ProtocolError('Dynamic Script type conflict; retain HOLD')
                encoded = value['Script'].encode('utf-8')
                if not encoded or len(encoded) > 128000:
                    raise ProtocolError('Dynamic Script size bounds conflict; retain HOLD')
                hashes.append(hashlib.sha256(encoded).hexdigest())
        if raw['sessionUpdate'] == 'tool_call_update' and known is None:
            raise ProtocolError('Dynamic workflow update precedes observed Script identity; retain HOLD')
        if raw['sessionUpdate'] == 'tool_call' and not hashes and known is None and raw.get('status') in {'completed', 'failed'}:
            raise ProtocolError('Dynamic terminal workflow lacks observed Script identity; retain HOLD')
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
        native_skip = (self.task.get('execution_policy') == 'native-skip'
                       or self.task.get('agy_approval') == 'skip-permissions')
        allow = root and (native_skip or self.task.get('agy_approval') == 'approve-all' or tool.get('kind') in {'read', 'search'})
        wanted = 'allow_once' if allow else 'reject_once'
        kinds = {'allow_once','allow_always','reject_once','reject_always'}
        option_kinds = sorted({o.get('kind') for o in params.get('options', []) if o.get('kind') in kinds})
        option = next((o.get('optionId') for o in params.get('options', []) if o.get('kind') == wanted), None) if root else None
        if root and native_skip and option is None:
            option = next((o.get('optionId') for o in params.get('options', []) if o.get('kind') == 'allow_always'), None)
            if option is not None:
                wanted = 'allow_always'
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
        session_caps = caps.get('sessionCapabilities', {})
        if not caps.get('loadSession') or 'close' not in session_caps:
            raise ProtocolError('Native ACP load/close capability required')
        self.session_capabilities = dict(session_caps)
        self.resume_supported = ('resume' in self.session_capabilities)
        return info

    def resume(self):
        if not getattr(self, 'resume_supported', False):
            raise ProtocolError('Native ACP resume capability required')
        result, _ = self.request('session/resume', dict(sessionId=self.session_id, **self.workspace()))
        if result.get('sessionId') != self.session_id:
            raise ProtocolError('Native resume identity mismatch')
        self.model_config_value = next(
            (o.get('currentValue') for o in result.get('configOptions', []) if o['id'] == 'model'),
            self.model_config_value,
        )
        return result

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

    def load(self, *, reconcile_stream=False, check_cancel=None):
        if not reconcile_stream:
            result, updates = self.request('session/load', dict(sessionId=self.session_id, **self.workspace()))
            self.last_load_updates = updates
            if result.get('sessionId') != self.session_id:
                raise ProtocolError('Native load identity mismatch')
            return result, snapshot(updates)
        self.last_load_retried = False
        pre_drained = self.drain_pending(check_cancel=check_cancel)
        self.last_load_drained = len(self.pending_drained)
        if check_cancel and check_cancel():
            return {}, []
        result, updates = self.request(
            'session/load', dict(sessionId=self.session_id, **self.workspace()), check_cancel=check_cancel
        )
        if result.get('sessionId') != self.session_id:
            raise ProtocolError('Native load identity mismatch')
        if check_cancel and check_cancel():
            return result, []
        prior_retry = getattr(self, 'pending_retry_state', None)
        orphan, dup = _inverted_tool_counts(updates)
        if orphan or dup:
            self.last_load_retried = True
            d1_updates = list(pre_drained) if prior_retry is not None else list(self.pending_drained)
            for entry in updates:
                if entry.get('sessionUpdate') == 'tool_call':
                    self.pending_drained.append(dict(entry, _retry_create=(entry.get('tool_event') == 'tool_call')))
            d2_updates = self.drain_pending(check_cancel=check_cancel)
            self.last_load_drained += len(d2_updates)
            if prior_retry is not None:
                if pre_drained:
                    prior_retry['d2_updates'] = list(prior_retry['d2_updates']) + list(pre_drained)
                self.pending_retry_state = dict(
                    d1_updates=[], attempt_updates=updates, d2_updates=d2_updates, prior=prior_retry,
                )
            else:
                self.pending_retry_state = dict(
                    d1_updates=d1_updates, attempt_updates=updates, d2_updates=d2_updates,
                )
            if check_cancel and check_cancel():
                return {}, []
            result, updates = self.request(
                'session/load', dict(sessionId=self.session_id, **self.workspace()), check_cancel=check_cancel
            )
            if result.get('sessionId') != self.session_id:
                raise ProtocolError('Native load identity mismatch')
            if check_cancel and check_cancel():
                return result, []
            orphan, dup = _inverted_tool_counts(updates)
            if orphan or dup:
                err_snap = snapshot_tolerant(updates)
                self.history_degraded = True
                self.last_history_diagnostic = _history_diagnostic(
                    'inverted_tool_replay', (), err_snap,
                    call_site='load', interaction_seq=None,
                    orphan_tool_updates=orphan, duplicate_tool_creates=dup,
                    load_retried=True, drained_before_load=self.last_load_drained,
                )
                if hasattr(self, 'drain_workflow_calls'):
                    self.drain_workflow_calls.clear()
                self.last_load_updates = updates
                return result, err_snap
        elif prior_retry is not None and pre_drained:
            prior_retry['d2_updates'] = list(prior_retry['d2_updates']) + list(pre_drained)
        if hasattr(self, 'drain_workflow_calls'):
            self.drain_workflow_calls.clear()
        self.last_load_updates = updates
        return result, snapshot(updates)

    def select(self, metadata, *, allow_load=True):
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
        mutated_config = False
        coordinator_family = None
        coordinator_metadata = metadata
        resume_current_model = None
        if not allow_load:
            models_meta = metadata.get('models')
            resume_current_model = models_meta.get('currentModelId') if isinstance(models_meta, dict) else None
            if not isinstance(resume_current_model, str) or not resume_current_model:
                raise ProtocolError('Native exact coordinator selection readback mismatch')
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
            mutated_config = True
            self.model_config_value = coordinator_family
            cfg_res, _ = self.request('session/set_config_option', dict(sessionId=self.session_id, configId='model', value=coordinator_family))
            coordinator_metadata = self.load()[0] if allow_load else cfg_res
            coordinator_config = {option['id']: option for option in coordinator_metadata.get('configOptions', []) if isinstance(option, dict) and 'id' in option}
            if coordinator_config.get('model', {}).get('currentValue') != coordinator_family:
                raise ProtocolError('Coordinator catalog configuration readback mismatch; no prompt')
            efforts = {option.get('value') for option in coordinator_config.get('thought_level', {}).get('options', [])}
            current_effort = coordinator_config.get('thought_level', {}).get('currentValue')
        model, family, effort = resolve(self.task.get('model'), 'gemini-4-carbon', self.task.get('effort'))
        self.model_config_value = family
        if not allow_load:
            actual = {o['id']: o for o in metadata.get('configOptions', []) if isinstance(o, dict) and 'id' in o}
            resume_matches = (
                not mutated_config
                and resume_current_model == model
                and actual.get('model', {}).get('currentValue') == family
                and (
                    (effort is None and actual.get('thought_level', {}).get('currentValue') is None)
                    or (effort is not None and actual.get('thought_level', {}).get('currentValue') == effort)
                )
            )
            if not resume_matches:
                # Re-apply coordinator selection on resumed connection without pre-prompt session/load.
                if mutated_config and family == coordinator_family:
                    readback = coordinator_metadata
                else:
                    readback, _ = self.request('session/set_config_option', dict(sessionId=self.session_id, configId='model', value=family))
                if effort is not None:
                    readback, _ = self.request('session/set_config_option', dict(sessionId=self.session_id, configId='thought_level', value=effort))
                actual = {o['id']: o for o in readback.get('configOptions', []) if isinstance(o, dict) and 'id' in o}
                rb_models = readback.get('models')
                rb_families = {o.get('value') for o in actual.get('model', {}).get('options', []) if isinstance(o, dict)} or families
                rb_efforts = {o.get('value') for o in actual.get('thought_level', {}).get('options', []) if isinstance(o, dict)} or efforts
                if (
                    (rb_models is not None and (not isinstance(rb_models, dict) or rb_models.get('currentModelId') != model))
                    or actual.get('model', {}).get('currentValue') != family
                    or family not in rb_families
                    or (effort is None and actual.get('thought_level', {}).get('currentValue') is not None)
                    or (effort is not None and (
                        actual.get('thought_level', {}).get('currentValue') != effort
                        or effort not in rb_efforts
                        or [(c, e) for c in rb_families for e in rb_efforts if c+'-'+e == model] != [(family, effort)]
                    ))
                ):
                    raise ProtocolError('Native exact coordinator selection readback mismatch')
        else:
            self.request('session/set_config_option', dict(sessionId=self.session_id, configId='model', value=family))
            if effort is not None:
                self.request('session/set_config_option', dict(sessionId=self.session_id, configId='thought_level', value=effort))
            readback, _ = self.load()
            actual = {o['id']: o for o in readback.get('configOptions', [])}
            if (readback.get('models', {}).get('currentModelId') != model
                    or actual.get('model', {}).get('currentValue') != family
                    or (effort is not None and actual.get('thought_level', {}).get('currentValue') != effort)):
                raise ProtocolError('Native exact coordinator selection readback mismatch')
        self.task.update(model=model, effort=effort)
        return dict(model=model, effort=effort,
                    native_selection=dict(requested=requested,
                        resolved=dict(model=model, effort=effort), readback_verified=True))

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

    def stop(self, *, force=True, wait_timeout=2):
        try:
            self.poller.close()
        except OSError:
            pass
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        kind = 'natural'
        try:
            self.proc.wait(timeout=wait_timeout)
        except subprocess.TimeoutExpired:
            if not force:
                try:
                    self.proc.stdout.close()
                except OSError:
                    pass
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
        try:
            self.proc.stdout.close()
        except OSError:
            pass
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
    if value.get('title') == 'run_workflow':
        for source in (update.get('args'), update.get('rawInput')):
            if isinstance(source, dict) and 'ScriptPath' not in source and isinstance(source.get('Script'), str):
                encoded = source['Script'].encode('utf-8')
                if 0 < len(encoded) <= 128000:
                    value['workflow_script_sha256'] = hashlib.sha256(encoded).hexdigest()
                    break
    if value.get('title') == 'swarm' and isinstance(args, dict):
        value['swarm'] = {k: args[k] for k in ('action', 'name', 'model') if isinstance(args.get(k), str)}
        output = update.get('rawOutput')
        if args.get('action') == 'status' and isinstance(output,dict) and isinstance(output.get('workers'),list):
            states = [row.get('status') for row in output['workers'] if isinstance(row,dict)]
            value['native_work_pending'] = any(isinstance(state,str) and state in {'working','running','pending','in_progress'} for state in states)
    elif kind == 'tool_call_update' and isinstance(args, dict):
        value['swarm_update'] = {k: args[k] for k in ('action', 'name', 'model') if isinstance(args.get(k), str)}
        output = update.get('rawOutput')
        if isinstance(output, dict) and isinstance(output.get('workers'), list):
            # Resolve sparse output against this ID's metadata in snapshot, never guess a new ID.
            states = [row.get('status') for row in output['workers'] if isinstance(row, dict)]
            value['status_output_pending'] = any(isinstance(state, str) and
                state in {'working','running','pending','in_progress'} for state in states)
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
                if ('status_output_pending' in value and old.get('title') == 'swarm'
                        and old.get('swarm', {}).get('action') == 'status'):
                    old['native_work_pending'] = value['status_output_pending']
                old.update({k:v for k,v in value.items() if k not in {'tool_event','swarm','swarm_update','status_args_invalid','status_output_pending'}})
            else:
                tools[key] = {k:v for k,v in value.items() if k not in {'tool_event','swarm_update','status_args_invalid','status_output_pending'}}
                result.append(tools[key])
        elif result and result[-1]['sessionUpdate'] == value['sessionUpdate']:
            result[-1]['content']['text'] += value['content']['text']
        else:
            result.append(json.loads(json.dumps(value)))
    return result


def snapshot_tolerant(updates):
    result, tools = [], {}
    skip_keys = {'tool_event', 'swarm', 'swarm_update', 'status_args_invalid', 'status_output_pending'}
    for value in updates:
        if value['sessionUpdate'] == 'tool_call':
            key = value['toolCallId']
            if not key:
                raise ProtocolError('Tool history omitted identity')
            if key in tools:
                old = tools[key]
                if 'kind' in value and 'kind' in old and value['kind'] != old['kind']:
                    raise ProtocolError('Native tool identity changed; reconciliation required')
                identity = value.get('swarm', value.get('swarm_update', {}) if ('swarm' in old or old.get('title') == 'swarm' or value.get('title') == 'swarm') else {})
                if any(k in old.get('swarm', {}) and v != old['swarm'][k] for k, v in identity.items()):
                    raise ProtocolError('Native tool identity changed; reconciliation required')
                if identity:
                    old['swarm'] = dict(old.get('swarm', {}), **identity)
                old_s = _safe_tool_status(old)
                new_s = _safe_tool_status(value)
                if old_s in {'completed', 'failed'} and new_s in {'completed', 'failed'} and old_s != new_s:
                    raise ProtocolError('Native tool identity changed; reconciliation required')
                for k, v in value.items():
                    if k in skip_keys:
                        continue
                    if k == 'status':
                        if old_s is not None and new_s is not None and _TOOL_STATUS_RANK[new_s] < _TOOL_STATUS_RANK[old_s]:
                            continue
                        old['status'] = v
                    elif k in {'title', 'kind'} and not v and old.get(k):
                        continue
                    else:
                        old[k] = v
                if ('status_output_pending' in value and old.get('title') == 'swarm'
                        and old.get('swarm', {}).get('action') == 'status'):
                    old['native_work_pending'] = value['status_output_pending']
            else:
                entry = {k: v for k, v in value.items() if k not in {'tool_event', 'swarm_update', 'status_args_invalid', 'status_output_pending'}}
                if 'swarm' not in entry and value.get('swarm_update') and entry.get('title') == 'swarm':
                    entry['swarm'] = dict(value['swarm_update'])
                if ('status_output_pending' in value and entry.get('title') == 'swarm'
                        and entry.get('swarm', {}).get('action') == 'status'):
                    entry['native_work_pending'] = value['status_output_pending']
                tools[key] = entry
                result.append(entry)
        elif result and result[-1]['sessionUpdate'] == value['sessionUpdate']:
            result[-1]['content']['text'] += value['content']['text']
        else:
            result.append(json.loads(json.dumps(value)))
    return result


_KNOWN_HISTORY_UPDATES = {'user_message_chunk', 'agent_message_chunk', 'tool_call'}
_KNOWN_TOOL_STATUSES = {'pending', 'in_progress', 'completed', 'failed'}


def _safe_slot_type(entry):
    kind = entry.get('sessionUpdate') if isinstance(entry, dict) else None
    return kind if kind in _KNOWN_HISTORY_UPDATES else None


def _safe_tool_status(entry):
    status = entry.get('status') if isinstance(entry, dict) else None
    return status if isinstance(status, str) and status in _KNOWN_TOOL_STATUSES else None


def _entry_text(entry):
    if not isinstance(entry, dict):
        return None
    content = entry.get('content')
    return content.get('text') if isinstance(content, dict) and isinstance(content.get('text'), str) else None


def _tool_history_conflict(old, new):
    if old.get('kind') not in (None, '') and new.get('kind') != old['kind']:
        return True
    if 'swarm' in old:
        return (not isinstance(new.get('swarm'), dict)
                or any(new['swarm'].get(k) != v for k, v in old['swarm'].items()))
    if 'swarm' in new:
        return (not isinstance(new.get('swarm'), dict)
                or new.get('title') != 'swarm'
                or old.get('title') not in (None, '', 'swarm'))
    return False


def _history_diagnostic(reason_code, previous=(), current=(), *, index=None,
                        background_growth=False, call_site=None, interaction_seq=None, **extra):
    prev_agent = ''.join(_entry_text(e) or '' for e in previous if _safe_slot_type(e) == 'agent_message_chunk')
    curr_agent = ''.join(_entry_text(e) or '' for e in current if _safe_slot_type(e) == 'agent_message_chunk')
    prev_tools = [e.get('toolCallId') for e in previous if _safe_slot_type(e) == 'tool_call']
    curr_tools = [e.get('toolCallId') for e in current if _safe_slot_type(e) == 'tool_call']
    prev_users = [_entry_text(e) for e in previous if _safe_slot_type(e) == 'user_message_chunk']
    curr_users = [_entry_text(e) for e in current if _safe_slot_type(e) == 'user_message_chunk']
    diag = dict(
        reason_code=reason_code,
        call_site=call_site,
        interaction_seq=interaction_seq,
        previous_len=len(previous),
        current_len=len(current),
        background_growth=bool(background_growth),
        concat_agent_is_prefix=curr_agent.startswith(prev_agent),
        same_tool_id_prefix=(len(curr_tools) >= len(prev_tools) and curr_tools[:len(prev_tools)] == prev_tools),
        same_user_prefix=(len(curr_users) >= len(prev_users) and curr_users[:len(prev_users)] == prev_users),
    )
    if index is not None and 0 <= index < len(previous):
        old = previous[index]
        new = current[index] if index < len(current) else None
        old_type = _safe_slot_type(old)
        new_type = _safe_slot_type(new)
        both_tools = (old_type == new_type == 'tool_call')
        old_text = _entry_text(old) if old_type in {'agent_message_chunk', 'user_message_chunk'} else None
        new_text = _entry_text(new) if new_type in {'agent_message_chunk', 'user_message_chunk'} else None
        same_text_kind = (old_type == new_type and isinstance(old_text, str) and isinstance(new_text, str))
        diag.update(
            index=index,
            is_tail=(index == len(previous) - 1),
            old_type=old_type,
            new_type=new_type,
            prev_slot_type=_safe_slot_type(previous[index - 1]) if index > 0 else None,
            next_new_slot_type=_safe_slot_type(current[index + 1]) if new is not None and index + 1 < len(current) else None,
            same_tool_id=(old.get('toolCallId') == new.get('toolCallId')) if both_tools else None,
            old_status=_safe_tool_status(old),
            new_status=_safe_tool_status(new),
            old_has_kind=bool(old.get('kind')) if old_type == 'tool_call' else None,
            new_has_kind=bool(new.get('kind')) if new_type == 'tool_call' else None,
            same_kind=(old.get('kind') == new.get('kind')) if both_tools else None,
            same_swarm=(old.get('swarm') == new.get('swarm')) if both_tools else None,
            old_len=len(old_text) if isinstance(old_text, str) else None,
            new_len=len(new_text) if isinstance(new_text, str) else None,
            text_equal=(old_text == new_text) if same_text_kind else None,
            is_prefix=new_text.startswith(old_text) if same_text_kind else None,
            rstrip_is_prefix=new_text.startswith(old_text.rstrip('\r\n')) if same_text_kind else None,
        )
    diag.update(extra)
    return diag


def _format_history_diagnostic(diag):
    fields = (
        ('site', 'call_site'),
        ('seq', 'interaction_seq'),
        ('reason', 'reason_code'),
        ('idx', 'index'),
        ('tail', 'is_tail'),
        ('prev', 'previous_len'),
        ('curr', 'current_len'),
        ('old', 'old_type'),
        ('new', 'new_type'),
        ('old_len', 'old_len'),
        ('new_len', 'new_len'),
        ('eq', 'text_equal'),
        ('prefix', 'is_prefix'),
        ('tool_id', 'same_tool_id'),
        ('agent_prefix', 'concat_agent_is_prefix'),
        ('tool_prefix', 'same_tool_id_prefix'),
        ('user_prefix', 'same_user_prefix'),
    )
    parts = [f'{short}={diag[key]}' for short, key in fields if diag.get(key) is not None]
    return '[' + ' '.join(parts) + ']' if parts else ''


def additions(previous, current, *, background_growth=False, call_site=None, interaction_seq=None):
    if len(current) < len(previous):
        raise ProtocolError('Native history shrank; reconciliation required',
            history_diagnostic=_history_diagnostic('history_shrank', previous, current,
                background_growth=background_growth, call_site=call_site, interaction_seq=interaction_seq))
    changed = []
    for index, old in enumerate(previous):
        new = current[index]
        if old == new:
            continue
        if old['sessionUpdate'] == new['sessionUpdate'] == 'tool_call' and old['toolCallId'] == new['toolCallId']:
            if _tool_history_conflict(old, new):
                raise ProtocolError('Native tool history changed; reconciliation required',
                    history_diagnostic=_history_diagnostic('tool_changed', previous, current, index=index,
                        background_growth=background_growth, call_site=call_site, interaction_seq=interaction_seq))
            if any(old.get(k) != new.get(k) for k in ('status', 'kind', 'swarm')):
                changed.append(new)
        elif (old['sessionUpdate'] == new['sessionUpdate']
              and ((old['sessionUpdate'] == 'agent_message_chunk' and background_growth)
                   or (index == len(previous)-1 and old['sessionUpdate'] in {'agent_message_chunk', 'user_message_chunk'}))
              and new['content']['text'].startswith(old['content']['text'])):
            changed.append(dict(sessionUpdate=new['sessionUpdate'], content=dict(type='text',
                text=new['content']['text'][len(old['content']['text']):])))
        else:
            raise ProtocolError('Native history prefix changed; reconciliation required',
                history_diagnostic=_history_diagnostic('prefix_changed', previous, current, index=index,
                    background_growth=background_growth, call_site=call_site, interaction_seq=interaction_seq))
    return changed+current[len(previous):]


def first_replay(published, replay, *, call_site=None, interaction_seq=None):
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
                    raise ProtocolError('Native live message history changed; reconciliation required',
                        history_diagnostic=_history_diagnostic('live_message_changed', published, replay,
                            call_site=call_site, interaction_seq=interaction_seq,
                            message_index=message_index, message_offset=message_offset,
                            published_message_count=len(messages),
                            old_len=len(old), new_len=len(text), consumed=consumed, is_prefix=False))
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
        raise ProtocolError('Native live message history changed; reconciliation required',
            history_diagnostic=_history_diagnostic('live_message_unconsumed', published, replay,
                call_site=call_site, interaction_seq=interaction_seq,
                message_index=message_index, message_offset=message_offset,
                published_message_count=len(messages)))
    return fresh


_TOOL_STATUS_RANK = {'pending': 0, 'in_progress': 1, 'completed': 2, 'failed': 2}


def _inverted_tool_counts(updates):
    created = set()
    orphan_updates = 0
    duplicate_creates = 0
    for entry in updates:
        if entry.get('sessionUpdate') != 'tool_call':
            continue
        tid = entry.get('toolCallId')
        if not tid:
            continue
        event = entry.get('tool_event')
        if event == 'tool_call':
            if tid in created:
                duplicate_creates += 1
            created.add(tid)
        elif event == 'tool_call_update' and tid not in created:
            orphan_updates += 1
    return orphan_updates, duplicate_creates


def _agent_chunks(entries):
    return [
        e for e in (entries or ())
        if e.get('sessionUpdate') == 'agent_message_chunk' and _entry_text(e)
    ]


def _text_len(entries):
    return sum(len(_entry_text(e) or '') for e in entries)


def _coalesce_message_slots(updates):
    """Merge consecutive agent_message_chunk entries without crossing tool or user boundaries."""
    slots = []
    prev_was_agent = False
    for entry in updates:
        kind = entry.get('sessionUpdate')
        if kind == 'agent_message_chunk':
            text = _entry_text(entry)
            if not text:
                continue
            if prev_was_agent and slots:
                slots[-1] = dict(
                    sessionUpdate='agent_message_chunk',
                    content=dict(type='text', text=slots[-1]['content']['text'] + text),
                )
            else:
                slots.append(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text=text)))
            prev_was_agent = True
        elif kind in {'tool_call', 'user_message_chunk'}:
            prev_was_agent = False
    return slots


def _reconcile_drained_tools(drained, history, *, call_site='drain_reconcile', interaction_seq=0):
    """Validate raw drained and retry tool frames in wire order without collapsing via snapshot()."""
    history_tools = {e['toolCallId']: e for e in history if e.get('sessionUpdate') == 'tool_call'}
    seen_tools = {}
    for entry in drained:
        if entry.get('sessionUpdate') != 'tool_call':
            continue
        tid = entry.get('toolCallId')
        if not tid:
            raise ProtocolError('Tool history omitted identity')
        clean = {k: v for k, v in entry.items() if k != '_retry_create'}
        persisted = history_tools.get(tid)
        if persisted is None:
            raise ProtocolError(
                'Native tool history changed; reconciliation required',
                history_diagnostic=_history_diagnostic(
                    'drained_tool_missing', [clean], (), index=0,
                    background_growth=True, call_site=call_site, interaction_seq=interaction_seq,
                ),
            )
        if ('swarm' not in clean and clean.get('title') in (None, '', 'swarm')
                and 'swarm' in persisted and isinstance(clean.get('swarm_update'), dict)):
            checked = dict(clean, swarm=clean['swarm_update'])
        else:
            checked = clean
        if _tool_history_conflict(checked, persisted):
            raise ProtocolError(
                'Native tool history changed; reconciliation required',
                history_diagnostic=_history_diagnostic(
                    'drained_tool_conflict', [checked], [persisted], index=0,
                    background_growth=True, call_site=call_site, interaction_seq=interaction_seq,
                ),
            )
        d_status = _safe_tool_status(checked)
        h_status = _safe_tool_status(persisted)
        if d_status is not None:
            prev_checked, prev_status = seen_tools.get(tid, (None, None))
            if prev_status is not None:
                if entry.get('_retry_create'):
                    regressed = (
                        prev_status in {'completed', 'failed'}
                        and d_status in {'completed', 'failed'}
                        and d_status != prev_status
                    )
                else:
                    regressed = (
                        _TOOL_STATUS_RANK[d_status] < _TOOL_STATUS_RANK[prev_status]
                        or (prev_status in {'completed', 'failed'} and d_status != prev_status)
                    )
                if regressed:
                    raise ProtocolError(
                        'Native tool history changed; reconciliation required',
                        history_diagnostic=_history_diagnostic(
                            'drained_tool_status_regressed', [prev_checked], [checked], index=0,
                            background_growth=True, call_site=call_site, interaction_seq=interaction_seq,
                        ),
                    )
            if (h_status is None
                    or _TOOL_STATUS_RANK[h_status] < _TOOL_STATUS_RANK[d_status]
                    or (d_status in {'completed', 'failed'} and h_status != d_status)):
                raise ProtocolError(
                    'Native tool history changed; reconciliation required',
                    history_diagnostic=_history_diagnostic(
                        'drained_tool_status_regressed', [checked], [persisted], index=0,
                        background_growth=True, call_site=call_site, interaction_seq=interaction_seq,
                    ),
                )
            if prev_status is None or _TOOL_STATUS_RANK[d_status] >= _TOOL_STATUS_RANK[prev_status]:
                seen_tools[tid] = (checked, d_status)


def _reconcile_non_tail_slots(rem_slots, history_msgs, persisted_prior_slots, unpersisted_prior_msgs,
                              *, call_site, interaction_seq):
    """Verify non-tail slot growth against persisted prior slots and return trailing unconsumed text."""
    if not persisted_prior_slots:
        return None
    if len(rem_slots) < len(persisted_prior_slots) or len(history_msgs) < len(rem_slots):
        return None
    for idx, p_slot in enumerate(persisted_prior_slots):
        if not rem_slots[idx]['content']['text'].startswith(p_slot['content']['text']):
            return None
    if unpersisted_prior_msgs:
        try:
            first_replay(
                unpersisted_prior_msgs,
                rem_slots[len(persisted_prior_slots):],
                call_site=call_site,
                interaction_seq=interaction_seq,
            )
        except ProtocolError:
            return None
    rem_after_slots = []
    for idx, r_slot in enumerate(rem_slots):
        r_text = r_slot['content']['text']
        h_text = history_msgs[idx]['content']['text']
        allow_growth = idx < len(persisted_prior_slots) or idx == len(rem_slots) - 1
        if allow_growth:
            if not h_text.startswith(r_text):
                return None
            delta = h_text[len(r_text):]
            if delta:
                rem_after_slots.append(
                    dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text=delta))
                )
        elif h_text != r_text:
            return None
    rem_after_slots.extend(history_msgs[len(rem_slots):])
    return rem_after_slots


def _reconcile_retry_messages(retry_state, history_msgs, reconciled_msgs, prior_messages,
                              persisted_prior_slots, *, call_site, interaction_seq):
    """Reconcile D1 -> Attempt #1 -> D2 message streams fail-closed without guessing by user chunks."""
    if retry_state.get('prior') is not None:
        rem_base = _reconcile_retry_messages(
            retry_state['prior'], history_msgs, reconciled_msgs,
            prior_messages, persisted_prior_slots,
            call_site=call_site, interaction_seq=interaction_seq,
        )
    else:
        rem_base = reconciled_msgs

    d1_msgs = _agent_chunks(retry_state.get('d1_updates'))
    rem_after_d1 = (
        first_replay(d1_msgs, rem_base, call_site=call_site, interaction_seq=interaction_seq)
        if d1_msgs else rem_base
    )

    attempt_updates = retry_state.get('attempt_updates') or ()
    msg_indices = [
        i for i, e in enumerate(attempt_updates)
        if e.get('sessionUpdate') == 'agent_message_chunk' and _entry_text(e)
    ]
    if not msg_indices:
        rem_for_d2 = rem_after_d1
    else:
        attempt_msgs = [attempt_updates[i] for i in msg_indices]
        unpersisted_prior = prior_messages[len(persisted_prior_slots):]
        rem_for_d2 = None
        last_exc = None
        for split in range(len(attempt_msgs) + 1):
            pre_msgs = attempt_msgs[:split]
            if pre_msgs:
                try:
                    rem_after_pre = first_replay(
                        pre_msgs, rem_after_d1, call_site=call_site, interaction_seq=interaction_seq,
                    )
                except ProtocolError as exc:
                    last_exc = exc
                    continue
            else:
                rem_after_pre = rem_after_d1

            if split == len(attempt_msgs):
                rem_for_d2 = rem_after_pre
                break

            found = False
            for end in range(len(attempt_msgs), split, -1):
                saved_part = attempt_msgs[split:end]
                post_msgs = attempt_msgs[end:]
                rem_after_saved = None
                try:
                    if prior_messages:
                        first_replay(
                            prior_messages, saved_part,
                            call_site=call_site, interaction_seq=interaction_seq,
                        )
                    rem_after_r = first_replay(
                        saved_part, history_msgs,
                        call_site=call_site, interaction_seq=interaction_seq,
                    )
                    rem_after_saved = (
                        rem_after_pre
                        if _text_len(rem_after_pre) <= _text_len(rem_after_r)
                        else rem_after_r
                    )
                except ProtocolError as exc:
                    last_exc = exc
                    slice_start = msg_indices[split]
                    slice_stop = msg_indices[end - 1] + 1
                    rem_slots = _coalesce_message_slots(attempt_updates[slice_start:slice_stop])
                    rem_after_slots = _reconcile_non_tail_slots(
                        rem_slots, history_msgs, persisted_prior_slots, unpersisted_prior,
                        call_site=call_site, interaction_seq=interaction_seq,
                    )
                    if rem_after_slots is not None:
                        rem_after_saved = (
                            rem_after_pre
                            if _text_len(rem_after_pre) <= _text_len(rem_after_slots)
                            else rem_after_slots
                        )

                if rem_after_saved is None:
                    continue

                if post_msgs:
                    try:
                        rem_for_d2 = first_replay(
                            post_msgs, rem_after_saved,
                            call_site=call_site, interaction_seq=interaction_seq,
                        )
                    except ProtocolError as exc:
                        last_exc = exc
                        continue
                else:
                    rem_for_d2 = rem_after_saved
                found = True
                break
            if found:
                break

        if rem_for_d2 is None:
            raise last_exc

    d2_msgs = _agent_chunks(retry_state.get('d2_updates'))
    if d2_msgs:
        return first_replay(d2_msgs, rem_for_d2, call_site=call_site, interaction_seq=interaction_seq)
    return rem_for_d2


def _reconcile_drained(drained, history, fresh, recovered, *, retry_state=None, prior_messages=(),
                       persisted_prior_slots=(), call_site='drain_reconcile', interaction_seq=0):
    if not drained and retry_state is None:
        return
    _reconcile_drained_tools(drained, history, call_site=call_site, interaction_seq=interaction_seq)
    recovered_msgs = [
        dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text=suffix))
        for _, suffix, _ in recovered if suffix
    ]
    reconciled_msgs = recovered_msgs + _agent_chunks(fresh)
    if retry_state is None:
        drained_msgs = _coalesce_message_slots(
            [e for e in drained if e.get('sessionUpdate') != 'user_message_chunk']
        )
        if drained_msgs:
            first_replay(drained_msgs, reconciled_msgs, call_site=call_site, interaction_seq=interaction_seq)
        return
    history_msgs = _agent_chunks(history)
    _reconcile_retry_messages(
        retry_state, history_msgs, reconciled_msgs,
        list(prior_messages), list(persisted_prior_slots),
        call_site=call_site, interaction_seq=interaction_seq,
    )





def dialogue_body(task, entry):
    header = 'CALLCLI_DIALOGUE '+json.dumps(dict(task=task['id'], turn=task['turn'],
        interaction_seq=entry['interaction_seq'], command_id=entry['command_id'],
        question_id=entry.get('question_id'), question=entry.get('question')))
    return header+' Continue the same native session; answer the request and remaining questions.\n\n'+entry['prompt']


def history_sections(history, task, commands):
    """Partition only at exact returned inputs, not model-authored labels."""
    boundaries = {dialogue_body(task, c): c['interaction_seq'] for c in commands.values()
        if c.get('kind') == 'dialogue' and c.get('turn') == task['turn'] and c.get('status') == 'returned'}
    sections, interaction = {0: []}, 0
    for entry in history:
        if entry['sessionUpdate'] == 'user_message_chunk':
            next_interaction = boundaries.get(entry['content']['text'])
            if next_interaction is not None:
                if next_interaction <= interaction:
                    raise ProtocolError('Native dialogue history reordered; reconciliation required',
                        history_diagnostic=_history_diagnostic('dialogue_reordered', (), history,
                            call_site='history_sections', interaction_seq=next_interaction))
                interaction = next_interaction
                sections[interaction] = []
        sections[interaction].append(entry)
    return sections


def pending_question(decoder):
    line = decoder.pending.lstrip()
    marker = 'CALLCLI_QUESTION '
    return bool(line) and (line.startswith(marker.rstrip()) or marker.startswith(line))


def brief(task, policy):
    preference = ('Requested worker model preference: '+task['agy_worker_model']+'.\n'
                  if task.get('agy_worker_model') else '')
    mode = task.get('agy_mode', 'swarm')
    bridge = ('Native delegation acknowledgements (invoke_subagent/send_message/ask_question) are progress, not final results. '
              'Use CALLCLI_STAGE for delegation or ongoing work and CALLCLI_QUESTION Q<n> for questions needed from the original source. '
              'Return the actual final public result only when ready; do not return a delegation acknowledgement as the final answer.\n'
              if mode == 'teamwork' else '')
    return '/'+mode+'\n'+protocol_prompt(policy+guidance(task)+bridge+preference+task['prompt'])





_DEGRADABLE_HISTORY_REASONS = {
    'prefix_changed',
    'history_shrank',
    'dialogue_missing',
    'dialogue_boundary_missing',
    'dialogue_reordered',
    'live_message_changed',
    'live_message_unconsumed',
    'resumed_boundary_missing',
}
try:
    from .agy import AGY_FATAL_LINE as _AGY_FATAL_CONST, BOARD_HINT_RE as _BOARD_HINT_RE
except ImportError:
    from agy import AGY_FATAL_LINE as _AGY_FATAL_CONST, BOARD_HINT_RE as _BOARD_HINT_RE


def _iter_retry_streams(retry_state):
    if not isinstance(retry_state, dict):
        return
    if retry_state.get('prior') is not None:
        yield from _iter_retry_streams(retry_state['prior'])
    for key in ('d1_updates', 'attempt_updates', 'd2_updates'):
        stream = retry_state.get(key)
        if stream:
            yield stream


def _has_completed_fatal_line(updates, *, require_terminated=False):
    pending_line = ''
    for entry in (updates or ()):
        kind = entry.get('sessionUpdate')
        if kind == 'agent_message_chunk':
            chunk = _entry_text(entry) or ''
            if not chunk:
                continue
            parts = (pending_line + chunk).split('\n')
            for seg in parts[:-1]:
                if seg.strip() == _AGY_FATAL_CONST:
                    return True
            pending_line = parts[-1][-64000:]
        elif kind in {'tool_call', 'user_message_chunk'}:
            if pending_line.strip() == _AGY_FATAL_CONST:
                return True
            pending_line = ''
    if not require_terminated and pending_line.strip() == _AGY_FATAL_CONST:
        return True
    return False


def _updates_have_fatal(updates, fatal_fn=None, *, require_terminated=False):
    if _has_completed_fatal_line(updates, require_terminated=require_terminated):
        return True
    if fatal_fn is not None and not require_terminated:
        text = ''.join(_entry_text(e) or '' for e in (updates or ()) if e.get('sessionUpdate') == 'agent_message_chunk')
        return bool(text) and fatal_fn(text)
    return False


def _salvage_wire_signals(updates):
    """Extract complete stage/question/skill/answer events from wire frames; never flush partial questions."""
    dec = EventDecoder()
    signals = []
    for entry in (updates or ()):
        kind = entry.get('sessionUpdate')
        if kind in {'tool_call', 'user_message_chunk'}:
            if dec.pending and not pending_question(dec):
                for ev_kind, ev_text in dec.finish():
                    if ev_kind in {'stage', 'question', 'skill', 'answer'}:
                        signals.append((ev_kind, ev_text))
            else:
                dec.pending = ''
                dec.pending_overflow = False
            continue
        if kind != 'agent_message_chunk':
            continue
        chunk = _entry_text(entry) or ''
        if not chunk:
            continue
        parts = chunk.split('\n')
        for idx, part in enumerate(parts):
            ended = idx < len(parts) - 1
            remaining = 64000 - len(dec.pending)
            dec.pending += part[:remaining]
            dec.pending_overflow |= len(part) > remaining
            if ended:
                for ev_kind, ev_text in dec.finish():
                    if ev_kind in {'stage', 'question', 'skill', 'answer'}:
                        signals.append((ev_kind, ev_text))
    return signals


def _iter_salvage_slices(retry_state, pending_drained, *, current_interaction, baseline=None,
                         prompt_text=None, task=None, commands=None, pre_dialogue_history=None, resumed=False):
    def _walk_retry(r_state):
        if not isinstance(r_state, dict):
            return
        if r_state.get('prior') is not None:
            yield from _walk_retry(r_state['prior'])
        if r_state.get('d1_updates'):
            yield current_interaction, r_state['d1_updates'], True
        att = r_state.get('attempt_updates')
        if att:
            first_user = next((i for i, e in enumerate(att) if e.get('sessionUpdate') == 'user_message_chunk'), None)
            if first_user is None:
                yield current_interaction, att, True
            else:
                if first_user > 0:
                    yield current_interaction, att[:first_user], True
                saved_part = list(att[first_user:])
                if (baseline or resumed) and prompt_text:
                    sliced = _slice_after_prompt_boundary(saved_part, prompt_text)
                    turn_part = sliced[1] if sliced is not None else []
                else:
                    turn_part = saved_part
                if turn_part:
                    if task is not None and commands is not None:
                        try:
                            att_secs = history_sections(turn_part, task, commands)
                        except ProtocolError:
                            att_secs = {current_interaction: turn_part}
                    else:
                        att_secs = {current_interaction: turn_part}
                    if current_interaction > 0 and current_interaction not in att_secs and att_secs:
                        m_seq = max(att_secs)
                        pre_p, post_p = _extract_post_dialogue_entries(att_secs[m_seq], pre_dialogue_history or ())
                        att_secs[m_seq] = pre_p
                        att_secs[current_interaction] = post_p
                    for a_seq, a_sec in sorted(att_secs.items()):
                        if a_sec:
                            yield a_seq, a_sec, False
        if r_state.get('d2_updates'):
            yield current_interaction, r_state['d2_updates'], True
    yield from _walk_retry(retry_state)
    if pending_drained:
        yield current_interaction, pending_drained, True


def _reconcile_resumed_live_tools(deg_sections, history, *, call_site='resumed_live_tool_reconcile'):
    """Cross-check live resumed-turn tools against session/load history without borrowing prior-turn tools."""
    by_id = {}
    for item in (history or ()):
        if item.get('sessionUpdate') == 'tool_call' and item.get('toolCallId'):
            by_id.setdefault(item['toolCallId'], []).append(item)
    for seq, sec in sorted(deg_sections.items()):
        for entry in sec:
            if entry.get('sessionUpdate') != 'tool_call':
                continue
            tid = entry.get('toolCallId')
            if not tid or tid not in by_id:
                continue
            matches = by_id[tid]
            l_status = _safe_tool_status(entry)
            for persisted in matches:
                title_conflict = (
                    bool(entry.get('title')) and bool(persisted.get('title'))
                    and entry.get('title') != persisted.get('title')
                )
                if title_conflict or _tool_history_conflict(entry, persisted):
                    raise ProtocolError(
                        'Native tool history changed; reconciliation required',
                        history_diagnostic=_history_diagnostic(
                            'resumed_live_tool_conflict', [entry], [persisted], index=0,
                            background_growth=True, call_site=call_site, interaction_seq=seq,
                        ),
                    )
            if len(matches) == 1:
                persisted = matches[0]
                h_status = _safe_tool_status(persisted)
                if l_status is not None and (
                    h_status is None
                    or _TOOL_STATUS_RANK[h_status] < _TOOL_STATUS_RANK[l_status]
                    or (l_status in {'completed', 'failed'} and h_status != l_status)
                ):
                    raise ProtocolError(
                        'Native tool history changed; reconciliation required',
                        history_diagnostic=_history_diagnostic(
                            'resumed_live_tool_regressed', [entry], [persisted], index=0,
                            background_growth=True, call_site=call_site, interaction_seq=seq,
                        ),
                    )
                if h_status in {'completed', 'failed'} and l_status in {'pending', 'in_progress'}:
                    entry.update({k: v for k, v in persisted.items() if k != 'sessionUpdate'})


def _check_degraded_drained_tools(drained, history, *, call_site='drain_reconcile', interaction_seq=0):
    """Enforce tool conflict, status regression, and vanished failed-tool safety even under compacted history."""
    history_tools = {e['toolCallId']: e for e in history if e.get('sessionUpdate') == 'tool_call' and e.get('toolCallId')}
    seen_tools = {}
    for entry in (drained or ()):
        if entry.get('sessionUpdate') != 'tool_call':
            continue
        tid = entry.get('toolCallId')
        if not tid:
            raise ProtocolError('Tool history omitted identity')
        clean = {k: v for k, v in entry.items() if k != '_retry_create'}
        d_status = _safe_tool_status(clean)
        if d_status is not None:
            prev_checked, prev_status = seen_tools.get(tid, (None, None))
            if prev_status is not None:
                if entry.get('_retry_create'):
                    regressed = (
                        prev_status in {'completed', 'failed'}
                        and d_status in {'completed', 'failed'}
                        and d_status != prev_status
                    )
                else:
                    regressed = (
                        _TOOL_STATUS_RANK[d_status] < _TOOL_STATUS_RANK[prev_status]
                        or (prev_status in {'completed', 'failed'} and d_status != prev_status)
                    )
                if regressed:
                    raise ProtocolError(
                        'Native tool history changed; reconciliation required',
                        history_diagnostic=_history_diagnostic(
                            'drained_tool_status_regressed', [prev_checked], [clean], index=0,
                            background_growth=True, call_site=call_site, interaction_seq=interaction_seq,
                        ),
                    )
            if prev_status is None or _TOOL_STATUS_RANK[d_status] >= _TOOL_STATUS_RANK[prev_status]:
                seen_tools[tid] = (clean, d_status)
        persisted = history_tools.get(tid)
        if persisted is None:
            if d_status == 'failed':
                raise ProtocolError(
                    'Native tool history changed; reconciliation required',
                    history_diagnostic=_history_diagnostic(
                        'drained_tool_missing', [clean], (), index=0,
                        background_growth=True, call_site=call_site, interaction_seq=interaction_seq,
                    ),
                )
            continue
        if ('swarm' not in clean and clean.get('title') in (None, '', 'swarm')
                and 'swarm' in persisted and isinstance(clean.get('swarm_update'), dict)):
            checked = dict(clean, swarm=clean['swarm_update'])
        else:
            checked = clean
        if _tool_history_conflict(checked, persisted):
            raise ProtocolError(
                'Native tool history changed; reconciliation required',
                history_diagnostic=_history_diagnostic(
                    'drained_tool_conflict', [checked], [persisted], index=0,
                    background_growth=True, call_site=call_site, interaction_seq=interaction_seq,
                ),
            )
        h_status = _safe_tool_status(persisted)
        if d_status is not None and (
            h_status is None
            or _TOOL_STATUS_RANK[h_status] < _TOOL_STATUS_RANK[d_status]
            or (d_status in {'completed', 'failed'} and h_status != d_status)
        ):
            raise ProtocolError(
                'Native tool history changed; reconciliation required',
                history_diagnostic=_history_diagnostic(
                    'drained_tool_status_regressed', [checked], [persisted], index=0,
                    background_growth=True, call_site=call_site, interaction_seq=interaction_seq,
                ),
            )


def _extract_post_dialogue_entries(entries, pre_history):
    """Split compacted/merged entries into (pre_dialogue_prefix, post_dialogue_suffix) without resurrecting old text."""
    if not entries:
        return [], []
    pre_tools = {
        e.get('toolCallId') for e in (pre_history or ())
        if e.get('sessionUpdate') == 'tool_call' and e.get('toolCallId')
    }
    pre_agent_texts = [
        _entry_text(e) for e in (pre_history or ())
        if e.get('sessionUpdate') == 'agent_message_chunk' and _entry_text(e)
    ]
    pre_part = []
    idx = 0
    agent_cursor = 0
    while idx < len(entries):
        entry = entries[idx]
        kind = entry.get('sessionUpdate')
        if idx == 0 and kind == 'user_message_chunk':
            pre_part.append(entry)
            idx += 1
            continue
        if kind == 'tool_call' and entry.get('toolCallId') in pre_tools:
            pre_part.append(entry)
            idx += 1
            continue
        if kind == 'agent_message_chunk' and agent_cursor < len(pre_agent_texts):
            etext = _entry_text(entry) or ''
            ptext = pre_agent_texts[agent_cursor]
            if etext == ptext:
                pre_part.append(entry)
                agent_cursor += 1
                idx += 1
                continue
            if etext.startswith(ptext):
                pre_part.append(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text=ptext)))
                rem = etext[len(ptext):]
                post_part = ([dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text=rem))] if rem else [])
                post_part.extend(entries[idx + 1:])
                return pre_part, post_part
        break
    return pre_part, list(entries[idx:])


def _slice_after_prompt_boundary(history, prompt_text):
    if not prompt_text:
        return None
    for i in range(len(history) - 1, -1, -1):
        e = history[i]
        if e.get('sessionUpdate') == 'user_message_chunk':
            utext = e.get('content', {}).get('text', '')
            if utext == prompt_text or utext.endswith(prompt_text):
                prefix = utext[:-len(prompt_text)] if utext != prompt_text else ''
                base = list(history[:i])
                if prefix:
                    base.append(dict(sessionUpdate='user_message_chunk', content=dict(type='text', text=prefix)))
                return base, list(history[i:])
    return None


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
        from .agy import (AGY_FATAL_LINE, AGY_INTERRUPTED_LINE, AGY_INVALID_OUTPUT_LINE,
                          fatal_error, is_board_navigation_only, is_standalone_incomplete_diagnostic, outcome_note)
    except ImportError:
        from agy import (AGY_FATAL_LINE, AGY_INTERRUPTED_LINE, AGY_INVALID_OUTPUT_LINE,
                         fatal_error, is_board_navigation_only, is_standalone_incomplete_diagnostic, outcome_note)
    if 'version_check' not in task:
        checked = ensure_current(store, task)
        emit('version', json.dumps(checked))
    owner = None
    submitted = False
    cleanup_token = None
    baseline = None
    cancellation_unverified = False
    abnormal_exit = False
    settled_result = None
    decoder = None
    try:
        with device_lock(task):
            if store.get(task['id'])['state'] == 'cancelling':
                if task.get('native_session_id') and not task.get('native_cleanup', {}).get('session_unloaded'):
                    raise ProtocolError('Cancelled before resumed native session was unloaded; retain HOLD')
                settled_result = TurnResult('cancelled', 0, 'cancelled', 'Cancelled before native owner launch')
                return settled_result
            owner = NativeOwner(task)
            binding = (task['id'], task['turn'], task['launch_id'], task.get('control_epoch', 1))
            cleanup_token = store.native_cleanup_owner(*binding)
            def unload(context, *, fence_pending_intervention=False):
                if getattr(owner,'close_attempted',False):
                    raise ProtocolError('Native unload already attempted; retain existing observation, no replay')
                observation=dict(task=task['id'],session_id=owner.session_id,turn=task['turn'],
                    launch_id=task['launch_id'],epoch=task.get('control_epoch',1),context=context,
                    attempted=True,verified=False,reason_code='unload_in_progress')
                begin_state = store.native_begin_unload(
                    *binding, observation, fence_pending_intervention=fence_pending_intervention
                )
                if begin_state == 'stale':
                    raise ProtocolError('Native unload owner identity stale; retain HOLD')
                if begin_state == 'pending_intervention':
                    return 'pending_intervention'
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
            def flush_before_cancel(target_dec=None):
                dec_to_flush = target_dec if target_dec is not None else decoder
                if dec_to_flush is None:
                    return
                if pending_question(dec_to_flush):
                    dec_to_flush.pending = ''
                    dec_to_flush.pending_overflow = False
                else:
                    for kind, text in dec_to_flush.finish():
                        emit(kind, text)
            def cancel_cleanup(known, notify=True, first=False, *, stop_reason=None, flush_decoder=None):
                nonlocal cancellation_unverified, owner, settled_result
                flush_before_cancel(flush_decoder)
                cancellation_unverified = True
                store.native_cancel_seal(*binding)
                if notify:
                    owner.notify_cancel()
                foreground = stop_reason if stop_reason else 'cancel_requested'
                dispatch_tools = {'swarm', 'invoke_subagent', 'send_message', 'run_workflow'}
                delegated = bool(
                    not first
                    or store.get(task['id']).get('native_phase') in {'background_pending', 'dynamic_pending'}
                    or any(e.get('sessionUpdate') == 'tool_call' and e.get('title') in dispatch_tools
                           for e in (known or ()))
                )
                workers_obs = 'delegated_observed' if delegated else 'none_observed'
                if (submitted and not getattr(owner, 'close_attempted', False)
                        and stop_reason in {'cancelled', 'end_turn'}):
                    def preserve_for_resume():
                        nonlocal cancellation_unverified, owner, settled_result
                        if not getattr(owner, 'resume_supported', False):
                            store.native_owner_diagnostics(*binding, native_cancel=dict(
                                foreground=stop_reason, stop_reason=stop_reason,
                                session_unloaded=False, resumable=False,
                                workers='unknown', delegated_workers_observed=delegated,
                                all_descendants_proven_stopped=False,
                                write_lock_retained=bool(task.get('write'))))
                            raise ProtocolError('Native session resume unsupported; retain HOLD')
                        saved_sid = owner.session_id
                        saved_perms = owner.permission_decisions
                        exit_receipt = owner.stop(force=False, wait_timeout=5)
                        owner = None
                        store.native_owner_diagnostics(*binding, native_owner_exit=exit_receipt,
                            native_permission_decisions=saved_perms)
                        if exit_receipt.get('kind') == 'natural' and exit_receipt.get('returncode') == 0:
                            cancellation_unverified = False
                            store.native_owner_diagnostics(*binding,
                                native_resume_session_id=saved_sid,
                                native_phase='cancelled',
                                native_workers=workers_obs,
                                native_cancel=dict(
                                    foreground=stop_reason, stop_reason=stop_reason,
                                    session_unloaded=False, resumable=True,
                                    resume_session_id=saved_sid, workers=workers_obs,
                                    delegated_workers_observed=delegated,
                                    all_descendants_proven_stopped=False,
                                    write_lock_retained=bool(task.get('write'))))
                            settled_result = TurnResult('cancelled', 0, 'cancelled',
                                'Foreground cancelled; exact session preserved for resume')
                            return settled_result
                        store.native_owner_diagnostics(*binding, native_cancel=dict(
                            foreground=stop_reason, stop_reason=stop_reason,
                            session_unloaded=False, resumable=False,
                            workers='unknown', delegated_workers_observed=delegated,
                            all_descendants_proven_stopped=False,
                            write_lock_retained=bool(task.get('write'))))
                        raise ProtocolError('Native owner exit unverified after cancel; retain HOLD')
                    pending_iv = (store.get(task['id']).get('intervention', {}).get('status') == 'pending')
                    if pending_iv:
                        return preserve_for_resume()
                    unloaded = unload('cancelled', fence_pending_intervention=True)
                    if unloaded == 'pending_intervention':
                        return preserve_for_resume()
                    if unloaded is True:
                        cancellation_unverified = False
                        cancel_id = store.get(task['id']).get('native_cancel_id')
                        store.native_owner_diagnostics(*binding, native_cancel=dict(
                            foreground=stop_reason, stop_reason=stop_reason,
                            session_unloaded=True, resumable=False,
                            workers=workers_obs, delegated_workers_observed=delegated,
                            all_descendants_proven_stopped=False,
                            write_lock_retained=False))
                        store.native_cleanup_commit(*binding, cleanup_token, dict(
                            kind='verified-cancellation', task=task['id'], session_id=owner.session_id,
                            turn=task['turn'], launch_id=task['launch_id'],
                            epoch=task.get('control_epoch', 1), cancel_id=cancel_id,
                            session_unloaded=True, workers=workers_obs,
                            all_descendants_proven_stopped=False))
                        settled_result = TurnResult('cancelled', 0, 'cancelled',
                            'Cancellation cleanup verified; business result not claimed')
                        return settled_result
                store.native_owner_diagnostics(*binding, native_cancel=dict(
                    foreground=foreground, stop_reason=stop_reason,
                    session_unloaded=False, resumable=False,
                    workers='unknown', delegated_workers_observed=delegated,
                    all_descendants_proven_stopped=False,
                    write_lock_retained=bool(task.get('write'))))
                raise ProtocolError('Native cancel forwarded; internal stop unverified, retain HOLD; no automatic replay')
            info = owner.initialize()
            store.update(task['id'], native_agent=info.get('agentInfo', {}), native_phase='initializing',
                native_capabilities=dict(load_session=True, close_supported=True,
                    resume_supported=owner.resume_supported))
            resumed = False
            if task.get('native_session_id'):
                owner.session_id = task['native_session_id']
                if task.get('native_resume_session_id') == owner.session_id:
                    resumed = True
                    metadata = owner.resume()
                    baseline = []
                    store.update(task['id'], native_resume_session_id='')
                else:
                    metadata, baseline = owner.load()
            else:
                store.update(task['id'], session_creation_attempted=True)
                metadata = owner.new()
                store.update(task['id'], native_session_id=owner.session_id, needs_session=False)
                _, baseline = owner.load()
            mode = task.get('agy_mode', 'swarm')
            native_command = 'run-workflow' if mode == 'dynamic' else mode
            if resumed:
                caps = task.get('native_capabilities', {})
                if caps.get('session_id') == owner.session_id:
                    owner.available_commands.update(caps.get('advertised_commands', []))
            if native_command not in owner.available_commands:
                owner.request(None, dict(command=native_command), timeout=1)
            if native_command not in owner.available_commands and (mode in {'dynamic','teamwork'} or owner.created_session or owner.available_commands):
                raise ProtocolError('Native '+native_command+' command not advertised; no prompt or fallback')
            store.update(task['id'], native_capabilities=dict(load_session=True, close_supported=True,
                resume_supported=owner.resume_supported, session_id=owner.session_id,
                advertised_commands=sorted(owner.available_commands)))
            selected = owner.select(metadata, allow_load=not resumed)
            task = store.update(task['id'], **selected)
            if owner.created_session:
                _, baseline = owner.load()
            emit('model', json.dumps(selected))
            try:
                task = stage(store, task, emit)
            except RuntimeError as exc:
                raise ProtocolError(str(exc)) from exc
            binding = (task['id'], task['turn'], task['launch_id'], task.get('control_epoch', 1))
            store.native_input_open(*binding)
            current_interaction = 0
            public_emit = emit
            emitted_events_by_seq = {}
            emitted_tool_summaries_by_seq = {}
            emitted_questions_turn = set()
            interruption_notified_seqs = set()
            def _is_usable_answer_line(line_text):
                stripped = (line_text or '').strip()
                return (bool(stripped)
                        and not is_board_navigation_only(stripped)
                        and is_standalone_incomplete_diagnostic(stripped) is None
                        and stripped != AGY_FATAL_LINE
                        and stripped != AGY_INVALID_OUTPUT_LINE)
            def emit(kind, text, **extra):
                interaction = extra.pop('interaction_seq', current_interaction)
                dedupe_only = extra.pop('_degraded_dedupe', False)
                suppress_resurrected_q = extra.pop('_suppress_prior_question', False)
                if kind == 'tool':
                    tid = text.split(' ', 1)[0] if text else ''
                    seen_tools = emitted_tool_summaries_by_seq.setdefault(interaction, {})
                    if dedupe_only and tid and seen_tools.get(tid) == text:
                        return
                    if tid:
                        seen_tools[tid] = text
                elif kind in {'stage', 'question', 'skill', 'answer'}:
                    if kind == 'question' and suppress_resurrected_q and text in emitted_questions_turn:
                        return
                    seen_ev = emitted_events_by_seq.setdefault(interaction, set())
                    if dedupe_only and (kind, text) in seen_ev:
                        return
                    seen_ev.add((kind, text))
                    if kind == 'question':
                        emitted_questions_turn.add(text)
                public_emit(kind, text, interaction_seq=interaction, **extra)
                if (kind == 'answer' and text.strip() == AGY_INTERRUPTED_LINE
                        and interaction not in interruption_notified_seqs):
                    interruption_notified_seqs.add(interaction)
                    public_emit(
                        'diagnostic',
                        'AGY reported stream interruption; native owner remains attached. '
                        'Inspect task state and send explicit dialogue to continue from existing progress without replaying completed work.',
                        interaction_seq=interaction,
                        diagnostic_code='agy_stream_interrupted',
                        attention_required=True,
                        notification_scope='originating-session',
                        next_action='Send explicit same-owner dialogue once foreground settles to continue from existing artifacts; do not replay the original prompt.',
                    )
            if not store.native_prompt_begin(*binding):
                store.native_cancel_seal(*binding)
                unload('preprompt-cancellation')
                if not owner.created_session or baseline != []:
                    raise ProtocolError('Preprompt cancellation prior roster unverified; retain HOLD')
                store.native_cleanup_commit(*binding,cleanup_token,dict(kind='fresh-no-prompt',task=task['id'],
                    session_id=owner.session_id,turn=task['turn'],launch_id=task['launch_id'],epoch=task.get('control_epoch',1),
                    fresh=True,baseline_empty=True,no_prompt=True,workers=[],session_unloaded=True,
                    all_descendants_proven_stopped=False))
                settled_result = TurnResult('cancelled',0,'cancelled')
                return settled_result
            if task.get('agy_mode') == 'dynamic':
                try:
                    from .agy_dynamic import run_dynamic
                except ImportError:
                    from agy_dynamic import run_dynamic
                submitted = True
                settled_result = run_dynamic(
                    store, task, owner, emit, policy, binding, cleanup_token,
                    baseline, unload, check_cancel,
                    cancel_cleanup=cancel_cleanup, resumed=resumed,
                )
                return settled_result
            decoder = EventDecoder()
            live_updates = []
            tool_last_status = {}
            usable_answer_count_by_seq = {}
            failed_tools_by_seq = {}
            unrecovered_live_tool_failure = False
            failure_hwm = None
            live_interaction_answers = {}
            live_wire_answers_by_seq = {}
            def _record_live_wire_answer(seq, line_text, *, deduped_already_seen=False):
                stripped = (line_text or '').strip()
                if not stripped or deduped_already_seen:
                    return
                live_wire_answers_by_seq.setdefault(seq, []).append(stripped)
            def _note_tool_status(tid, status, seq, *, usable_count_override=None):
                nonlocal unrecovered_live_tool_failure, failure_hwm
                if status not in _KNOWN_TOOL_STATUSES:
                    return
                key = tid or f'_anon_{len(tool_last_status)}'
                prev = tool_last_status.get(key)
                tool_last_status[key] = status
                if status == 'failed' and prev != 'failed':
                    unrecovered_live_tool_failure = True
                    base_cnt = usable_answer_count_by_seq.get(seq, 0) if usable_count_override is None else usable_count_override
                    failure_hwm = (seq, base_cnt)
                    failed_tools_by_seq.setdefault(seq, set()).add(key)
                    live_interaction_answers[seq] = ''
                    live_wire_answers_by_seq[seq] = []
            def _note_usable_answer(seq, line_text):
                nonlocal unrecovered_live_tool_failure
                if not _is_usable_answer_line(line_text):
                    return
                cnt = usable_answer_count_by_seq.get(seq, 0) + 1
                usable_answer_count_by_seq[seq] = cnt
                if unrecovered_live_tool_failure and failure_hwm is not None:
                    f_seq, f_cnt = failure_hwm
                    if seq > f_seq or (seq == f_seq and cnt > f_cnt):
                        unrecovered_live_tool_failure = False
                        failed_tools_by_seq.clear()
            def publish_update(entry):
                nonlocal last_visible
                live_updates.append(entry)
                if entry.get('sessionUpdate') != 'user_message_chunk':
                    last_visible = time.monotonic()
                for kind, text in decoder.feed(dict(params=dict(update=entry))):
                    if kind == 'answer':
                        _record_live_wire_answer(0, text)
                        _note_usable_answer(0, text)
                    emit(kind, text)
                if entry.get('sessionUpdate') == 'tool_call':
                    if entry.get('tool_event') == 'tool_call':
                        live_wire_answers_by_seq[0] = []
                    _note_tool_status(entry.get('toolCallId'), _safe_tool_status(entry), 0)
            started_at = last_notice = last_visible = time.monotonic()
            def review():
                nonlocal last_notice
                now = time.monotonic()
                if (not owner.review_blocked and now-last_notice >= 600
                        and store.get(task['id'])['state'] == 'running'):
                    notice_kind = 'quiet' if now - last_visible >= 600 else 'checkpoint'
                    emit('stage', 'Coordinator review requested: native work pending; no cancellation or prompt sent.',
                         quiet_notice=True, notice_kind=notice_kind, elapsed=int(now - started_at))
                    last_notice = now
            # One cooldown across foreground, dialogue and passive observation requests.
            owner.review = review
            submitted = True
            prompt_text = brief(task, policy)
            if resumed:
                owner.resumed_prompt_boundary = prompt_text
            response, _ = owner.request('session/prompt', dict(sessionId=owner.session_id,
                prompt=[dict(type='text', text=prompt_text)]), timeout=None,
                check_cancel=check_cancel, on_update=publish_update)
            if check_cancel():
                return cancel_cleanup(snapshot(live_updates), notify=not owner.request_cancel_sent,
                    first=True, stop_reason=response.get('stopReason'))
            if response.get('stopReason') != 'end_turn':
                for kind, text in decoder.finish():
                    emit(kind, text)
                unloaded = unload('foreground-abnormal')
                store.update(task['id'], native_cancel=dict(foreground=response.get('stopReason'),
                             session_unloaded=unloaded, workers='unknown'))
                raise ProtocolError('Native foreground settled '+str(response.get('stopReason'))+'; internal work fate unknown')
            store.update(task['id'], native_phase='background_pending', native_foreground_stop='end_turn')
            for kind, text in decoder.finish():
                if kind == 'answer':
                    _record_live_wire_answer(0, text)
                    _note_usable_answer(0, text)
                emit(kind, text)
            if getattr(owner, 'fatal_observed', False) or fatal_error(decoder.text):
                raise ProtocolError('AGY reported a fatal execution error; outcome unknown, no automatic replay')
            fg_ans = decoder.final_answer.strip()
            live_interaction_answers[0] = (
                '' if (is_board_navigation_only(fg_ans) or is_standalone_incomplete_diagnostic(fg_ans) is not None) else fg_ans
            )
            pre_dialogue_history = []
            previous_public = snapshot([e for e in live_updates if e['sessionUpdate'] != 'user_message_chunk'])
            # A completed handoff RPC and its foreground acknowledgement are not
            # the delegated result. Only later public output can supply that result.
            handoff_names = {'invoke_subagent', 'send_message', 'ask_question'}
            response_floor = previous_public if mode == 'teamwork' and any(
                e.get('title') in handoff_names for e in previous_public if e['sessionUpdate']=='tool_call') else []
            previous = None
            last_semantic_change = None
            dialogue_public = None
            deferred_questions = []
            interaction_body = None
            resumed_boundary_confirmed = not resumed
            live_turn_updates_by_seq = {
                0: [e for e in live_updates if e.get('sessionUpdate') != 'user_message_chunk']
            }

            blocker_notice = None
            while True:
                if check_cancel():
                    return cancel_cleanup(previous if previous is not None else snapshot(live_updates),
                                          notify=not getattr(owner, 'request_cancel_sent', False),
                                          first=previous is None, stop_reason='end_turn')
                _, history = owner.load(reconcile_stream=True, check_cancel=check_cancel)
                if check_cancel():
                    return cancel_cleanup(previous if previous is not None else snapshot(live_updates),
                                          notify=not getattr(owner, 'request_cancel_sent', False),
                                          first=previous is None, stop_reason='end_turn')
                if getattr(owner, 'fatal_observed', False):
                    raise ProtocolError('AGY reported a fatal execution error; outcome unknown, no automatic replay')
                if getattr(owner, 'last_history_diagnostic', None) is not None:
                    owner.last_history_diagnostic.setdefault('turn', task['turn'])
                    if owner.last_history_diagnostic.get('interaction_seq') is None:
                        owner.last_history_diagnostic['interaction_seq'] = current_interaction
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
                try:
                    if not resumed_boundary_confirmed:
                        sliced = _slice_after_prompt_boundary(history, prompt_text) if previous is None else None
                        if sliced is None:
                            raise ProtocolError(
                                'Native resumed turn boundary missing; reconciliation required',
                                history_diagnostic=_history_diagnostic(
                                    'resumed_boundary_missing',
                                    previous if previous is not None else previous_public,
                                    history,
                                    call_site='resumed_boundary',
                                    interaction_seq=current_interaction,
                                ),
                            )
                        baseline, _ = sliced
                        resumed_boundary_confirmed = True
                    turn_history = additions(baseline, history, background_growth=True,
                        call_site='baseline', interaction_seq=0)
                    public_history = turn_history
                    if previous is None:
                        fresh_sections = {0: first_replay(previous_public, public_history,
                            call_site='initial_replay', interaction_seq=0)}
                    else:
                        commands = store.get(task['id']).get('commands', {})
                        old_sections = history_sections(comparison, task, commands)
                        new_sections = history_sections(history, task, commands)
                        if not old_sections.keys() <= new_sections.keys():
                            raise ProtocolError('Native dialogue history missing; reconciliation required',
                                history_diagnostic=_history_diagnostic('dialogue_missing', comparison, history,
                                    call_site='section', interaction_seq=current_interaction))
                        fresh_sections = {seq: additions(old_sections.get(seq, []), section, background_growth=True,
                            call_site='section', interaction_seq=seq)
                            for seq, section in new_sections.items()}
                    pre_load_dialogue = dialogue_public
                    if dialogue_public is not None:
                        if current_interaction not in fresh_sections:
                            raise ProtocolError('Native dialogue replay boundary missing; reconciliation required',
                                history_diagnostic=_history_diagnostic('dialogue_boundary_missing',
                                    comparison if comparison is not None else baseline, history,
                                    call_site='dialogue_replay', interaction_seq=current_interaction))
                        fresh_sections[current_interaction] = first_replay(dialogue_public, fresh_sections[current_interaction],
                            call_site='dialogue_replay', interaction_seq=current_interaction)
                        dialogue_public = None
                    fresh = [e for section in fresh_sections.values() for e in section]
                    prior_base = previous if previous is not None else (list(baseline) + list(previous_public))
                    prior_messages = _agent_chunks(prior_base) + _agent_chunks(pre_load_dialogue if previous is not None else ())
                    persisted_prior_slots = _agent_chunks(previous if previous is not None else baseline)
                    _reconcile_drained(
                        owner.pending_drained, history, fresh, recovered,
                        retry_state=getattr(owner, 'pending_retry_state', None),
                        prior_messages=prior_messages,
                        persisted_prior_slots=persisted_prior_slots,
                        call_site='drain_reconcile', interaction_seq=current_interaction,
                    )
                    owner.pending_drained.clear()
                    owner.pending_retry_state = None
                    owner.drained_public_text = ''
                    owner.drained_line_pending = ''
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
                    for seq, section in fresh_sections.items():
                        # A completed older response must not enter the current decoder.
                        target = decoder if seq == current_interaction else EventDecoder()
                        for entry in section:
                            is_appended = entry in appended
                            for kind, text in target.feed(dict(params=dict(update=entry)), text_boundary=is_appended):
                                if kind == 'answer':
                                    already_seen = ('answer', text) in emitted_events_by_seq.get(seq, set())
                                    _record_live_wire_answer(seq, text, deduped_already_seen=already_seen)
                                    _note_usable_answer(seq, text)
                                emit(kind, text, interaction_seq=seq)
                            if entry.get('sessionUpdate') == 'tool_call':
                                if is_appended:
                                    live_wire_answers_by_seq[seq] = []
                                _note_tool_status(entry.get('toolCallId'), _safe_tool_status(entry), seq)
                        if seq != current_interaction:
                            for kind, text in target.finish():
                                if kind == 'answer':
                                    already_seen = ('answer', text) in emitted_events_by_seq.get(seq, set())
                                    _record_live_wire_answer(seq, text, deduped_already_seen=already_seen)
                                    _note_usable_answer(seq, text)
                                emit(kind, text, interaction_seq=seq)
                except ProtocolError as hist_exc:
                    h_diag = getattr(hist_exc, 'history_diagnostic', None)
                    reason = h_diag.get('reason_code') if isinstance(h_diag, dict) else None
                    unresumed_reused_baseline = (
                        reason in {'prefix_changed', 'history_shrank'}
                        and h_diag.get('call_site') == 'baseline'
                        and not owner.created_session
                        and not resumed
                    )
                    if reason not in _DEGRADABLE_HISTORY_REASONS or unresumed_reused_baseline:
                        raise
                    commands = store.get(task['id']).get('commands', {})
                    salvage_slices = list(_iter_salvage_slices(
                        getattr(owner, 'pending_retry_state', None),
                        owner.pending_drained,
                        current_interaction=current_interaction,
                        baseline=baseline,
                        prompt_text=prompt_text,
                        task=task,
                        commands=commands,
                        pre_dialogue_history=pre_dialogue_history,
                        resumed=resumed,
                    ))
                    if resumed_boundary_confirmed:
                        for stream in _iter_retry_streams(getattr(owner, 'pending_retry_state', None)):
                            if _updates_have_fatal(stream, fatal_error):
                                owner.fatal_observed = True
                        if resumed:
                            sliced_h = _slice_after_prompt_boundary(history, prompt_text)
                            history_for_fatal = sliced_h[1] if sliced_h is not None else []
                        else:
                            history_for_fatal = history
                        if (getattr(owner, 'fatal_observed', False)
                                or _updates_have_fatal(owner.pending_drained, fatal_error, require_terminated=True)
                                or _updates_have_fatal(history_for_fatal, fatal_error)):
                            owner.fatal_observed = True
                            raise ProtocolError('AGY reported a fatal execution error; outcome unknown, no automatic replay')
                    else:
                        if any(_updates_have_fatal(s_stream, fatal_error) for _, s_stream, _ in salvage_slices):
                            owner.fatal_observed = True
                        if any(_updates_have_fatal(stream, fatal_error) for stream in live_turn_updates_by_seq.values()):
                            owner.fatal_observed = True
                        if (getattr(owner, 'fatal_observed', False)
                                or _updates_have_fatal(owner.pending_drained, fatal_error, require_terminated=True)
                                or fatal_error(decoder.text)):
                            owner.fatal_observed = True
                            raise ProtocolError('AGY reported a fatal execution error; outcome unknown, no automatic replay')
                    _check_degraded_drained_tools(
                        owner.pending_drained, history,
                        call_site='drain_reconcile', interaction_seq=current_interaction,
                    )
                    diag = dict(h_diag, turn=task['turn'])
                    if diag.get('interaction_seq') is None:
                        diag['interaction_seq'] = current_interaction
                    diag.setdefault('drained_before_load', getattr(owner, 'last_load_drained', 0))
                    diag.setdefault('load_retried', getattr(owner, 'last_load_retried', False))
                    owner.history_degraded = True
                    owner.last_history_diagnostic = diag
                    for s_seq, s_stream, s_live in salvage_slices:
                        if s_live:
                            live_turn_updates_by_seq.setdefault(s_seq, []).extend(
                                e for e in s_stream if e.get('sessionUpdate') != 'user_message_chunk'
                            )
                        for s_entry in s_stream:
                            if s_entry.get('sessionUpdate') == 'tool_call' and (s_live or s_seq == current_interaction):
                                _note_tool_status(s_entry.get('toolCallId'), _safe_tool_status(s_entry), s_seq)
                        for sig_kind, sig_text in _salvage_wire_signals(s_stream):
                            already_emitted = (sig_kind, sig_text) in emitted_events_by_seq.get(s_seq, set())
                            emit(sig_kind, sig_text, interaction_seq=s_seq, _degraded_dedupe=True)
                            if sig_kind == 'answer' and not already_emitted:
                                _record_live_wire_answer(s_seq, sig_text)
                                _note_usable_answer(s_seq, sig_text)
                    if not resumed_boundary_confirmed:
                        deg_sections = {
                            seq: snapshot_tolerant(entries)
                            for seq, entries in sorted(live_turn_updates_by_seq.items())
                            if entries
                        }
                        deg_sections.setdefault(current_interaction, [])
                        collapsed_boundary = False
                        _reconcile_resumed_live_tools(deg_sections, history)
                        turn_history = [e for _, sec in sorted(deg_sections.items()) for e in sec]
                    else:
                        if baseline:
                            sliced = _slice_after_prompt_boundary(history, prompt_text)
                            turn_history = sliced[1] if sliced is not None else list(history)
                        else:
                            turn_history = list(history)
                        try:
                            deg_sections = history_sections(turn_history, task, commands)
                        except ProtocolError:
                            deg_sections = {current_interaction: list(turn_history)}
                        collapsed_boundary = False
                        if current_interaction > 0 and current_interaction not in deg_sections:
                            collapsed_boundary = True
                            max_seq = max(deg_sections)
                            pre_part, post_part = _extract_post_dialogue_entries(deg_sections[max_seq], pre_dialogue_history)
                            deg_sections[max_seq] = pre_part
                            deg_sections[current_interaction] = post_part
                    for seq, sec in sorted(deg_sections.items()):
                        sec_dec = EventDecoder()
                        suppress_q = collapsed_boundary and seq == current_interaction
                        active_in_sec = any(
                            e.get('sessionUpdate') == 'tool_call' and e.get('status') not in {'completed', 'failed'}
                            for e in sec
                        )
                        last_failed_idx = max(
                            (i for i, e in enumerate(sec)
                             if e.get('sessionUpdate') == 'tool_call' and _safe_tool_status(e) == 'failed'),
                            default=-1,
                        )
                        sec_failed_keys = {
                            e.get('toolCallId') for e in sec
                            if e.get('sessionUpdate') == 'tool_call'
                            and _safe_tool_status(e) == 'failed'
                            and e.get('toolCallId')
                        }
                        sec_usable_idx = 0
                        def _note_deg_answer(a_seq, a_text, after_failed):
                            nonlocal sec_usable_idx, unrecovered_live_tool_failure
                            if not _is_usable_answer_line(a_text):
                                return
                            sec_usable_idx += 1
                            if not after_failed:
                                return
                            if unrecovered_live_tool_failure and failure_hwm is not None:
                                f_seq, f_cnt = failure_hwm
                                if a_seq > f_seq or (
                                    a_seq == f_seq
                                    and sec_usable_idx > f_cnt
                                    and failed_tools_by_seq.get(f_seq, set()) <= sec_failed_keys
                                ):
                                    usable_answer_count_by_seq[a_seq] = max(usable_answer_count_by_seq.get(a_seq, 0), sec_usable_idx)
                                    unrecovered_live_tool_failure = False
                                    failed_tools_by_seq.clear()
                            else:
                                usable_answer_count_by_seq[a_seq] = max(usable_answer_count_by_seq.get(a_seq, 0), sec_usable_idx)
                        for idx, entry in enumerate(sec):
                            for kind, text in sec_dec.feed(
                                dict(params=dict(update=entry)),
                                text_boundary=(idx < len(sec) - 1 or (not resumed_boundary_confirmed and not active_in_sec)),
                            ):
                                if kind == 'answer':
                                    _note_deg_answer(seq, text, idx > last_failed_idx)
                                emit(kind, text, interaction_seq=seq, _degraded_dedupe=True,
                                     _suppress_prior_question=suppress_q)
                            if entry.get('sessionUpdate') == 'tool_call':
                                _note_tool_status(entry.get('toolCallId'), _safe_tool_status(entry), seq,
                                                  usable_count_override=sec_usable_idx)
                        if seq == current_interaction:
                            if pending_question(sec_dec) and (resumed_boundary_confirmed or active_in_sec):
                                decoder.pending = sec_dec.pending
                                decoder.pending_overflow = sec_dec.pending_overflow
                            else:
                                decoder.pending = ''
                                decoder.pending_overflow = False
                                for kind, text in sec_dec.finish():
                                    if kind == 'answer':
                                        _note_deg_answer(seq, text, len(sec) > last_failed_idx)
                                    emit(kind, text, interaction_seq=seq, _degraded_dedupe=True,
                                         _suppress_prior_question=suppress_q)
                        elif not pending_question(sec_dec):
                            for kind, text in sec_dec.finish():
                                if kind == 'answer':
                                    _note_deg_answer(seq, text, len(sec) > last_failed_idx)
                                emit(kind, text, interaction_seq=seq, _degraded_dedupe=True)
                    dialogue_public = None
                    owner.pending_drained.clear()
                    owner.pending_retry_state = None
                    owner.drained_public_text = ''
                    owner.drained_line_pending = ''
                    fresh = [e for sec in deg_sections.values() for e in sec]
                    recovered = []
                effective_history = turn_history if not resumed_boundary_confirmed else history
                previous = effective_history
                now = time.time()
                has_recovery = any(suffix or boundary for _, suffix, boundary in recovered)
                if fresh or has_recovery or last_semantic_change is None:
                    last_semantic_change = now
                if any(e.get('sessionUpdate') != 'user_message_chunk' for e in fresh) or has_recovery:
                    last_visible = time.monotonic()
                prior_obs = store.get(task['id']).get('native_observation') or {}
                obs_payload = dict(turn=task['turn'], last_success_at=now,
                    last_semantic_change_at=last_semantic_change,
                    pending_tools=[dict(id=safe_tool_id(e['toolCallId']), name=safe_tool_name(e.get('title')), kind=safe_tool_kind(e.get('kind')),
                        status=e['status']) for e in effective_history if e['sessionUpdate']=='tool_call'
                        and e.get('status') in {'pending', 'in_progress'}],
                    question_ids=store.get(task['id']).get('questions', []), input_available=True,
                    question_blockers=[dict(interaction_seq=d['interaction_seq'], reason='incomplete_question',
                        available_actions=['observe', 'dialogue', 'cancel'], blocks_completion=True)
                        for d in deferred_questions]+([dict(interaction_seq=current_interaction,
                            reason='incomplete_question', available_actions=['observe', 'dialogue', 'cancel'],
                            blocks_completion=True)] if pending_question(decoder) else []))
                if getattr(owner, 'history_degraded', False) or prior_obs.get('history_degraded'):
                    obs_payload['history_degraded'] = True
                hist_diag_val = getattr(owner, 'last_history_diagnostic', None) or prior_obs.get('history_diagnostic')
                if hist_diag_val is not None:
                    obs_payload['history_diagnostic'] = hist_diag_val
                # Persist only reconciled successful loads, never partial RPC updates.
                store.update(task['id'], native_snapshot=[e for e in effective_history if e['sessionUpdate'] != 'user_message_chunk'],
                    native_permission_decisions=owner.permission_decisions,
                    native_observation=obs_payload)
                blockers = store.get(task['id'])['native_observation']['question_blockers']
                signature = tuple(b['interaction_seq'] for b in blockers)
                if signature and signature != blocker_notice:
                    public_emit('stage', 'Native question fragment incomplete; observe this owner for a complete boundary, '
                        'or explicitly dialogue/cancel after review. Completion remains blocked; no partial question published.',
                        interaction_seq=signature[0], notice_kind='incomplete_question', question_blockers=blockers)
                blocker_notice = signature
                current_text = '\n'.join(e['content']['text'] for e in turn_history if e['sessionUpdate'] == 'agent_message_chunk')
                if getattr(owner, 'fatal_observed', False) or _has_completed_fatal_line(turn_history) or fatal_error(current_text):
                    raise ProtocolError('AGY reported a fatal execution error; outcome unknown, no automatic replay')
                owner.drain_pending(check_cancel=check_cancel)
                if owner.pending_drained:
                    continue
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
                    pre_dialogue_history = list(history)
                    prev_interaction = current_interaction
                    current_interaction = entry['interaction_seq']
                    dialogue_live = []
                    def publish_dialogue(update):
                        nonlocal last_visible
                        dialogue_live.append(update)
                        if update.get('sessionUpdate') != 'user_message_chunk':
                            last_visible = time.monotonic()
                        for kind, text in decoder.feed(dict(params=dict(update=update))):
                            if kind == 'answer':
                                _record_live_wire_answer(current_interaction, text)
                                _note_usable_answer(current_interaction, text)
                            emit(kind, text)
                        if update.get('sessionUpdate') == 'tool_call':
                            if update.get('tool_event') == 'tool_call':
                                live_wire_answers_by_seq[current_interaction] = []
                            _note_tool_status(update.get('toolCallId'), _safe_tool_status(update), current_interaction)
                    emit('stage', 'Dialogue '+entry['command_id']+' claimed; not yet sent.')
                    interaction_body = dialogue_body(task, entry)
                    if deferred_questions and deferred_questions[-1]['interaction_seq'] == prev_interaction:
                        deferred_questions[-1].update(replacement_command=entry['command_id'],
                            replacement_body=interaction_body)
                    store.native_dialogue_state(*binding, entry['command_id'], 'send_started')
                    decoder.final_answer = ''
                    response, _ = owner.request('session/prompt', dict(sessionId=owner.session_id,
                        prompt=[dict(type='text', text=interaction_body)]), timeout=None,
                        check_cancel=check_cancel, on_update=publish_dialogue)
                    if check_cancel():
                        if response.get('stopReason')=='cancelled':
                            store.native_dialogue_state(*binding,entry['command_id'],'cancelled')
                        return cancel_cleanup(effective_history, notify=not owner.request_cancel_sent,
                            stop_reason=response.get('stopReason'))
                    if response.get('stopReason') != 'end_turn':
                        raise ProtocolError('Native dialogue did not return end_turn; effects unknown')
                    for kind, text in decoder.finish():
                        if kind == 'answer':
                            _record_live_wire_answer(current_interaction, text)
                            _note_usable_answer(current_interaction, text)
                        emit(kind, text)
                    d_ans = decoder.final_answer.strip()
                    live_interaction_answers[current_interaction] = (
                        '' if (is_board_navigation_only(d_ans) or is_standalone_incomplete_diagnostic(d_ans) is not None) else d_ans
                    )
                    dialogue_public = snapshot([e for e in dialogue_live if e['sessionUpdate']!='user_message_chunk'])
                    live_turn_updates_by_seq[current_interaction] = [
                        e for e in dialogue_live if e.get('sessionUpdate') != 'user_message_chunk'
                    ]
                    store.native_dialogue_state(*binding, entry['command_id'], 'returned')
                    if getattr(owner, 'fatal_observed', False) or fatal_error(decoder.text):
                        raise ProtocolError('AGY reported a fatal execution error; outcome unknown, no automatic replay')
                    emit('stage', 'Dialogue '+entry['command_id']+' returned; verify the requested result independently.')
                    response_floor = dialogue_public if mode == 'teamwork' and any(
                        e.get('title') in handoff_names for e in dialogue_public if e['sessionUpdate']=='tool_call') else []
                    continue
                current = store.get(task['id'])
                active = any(e.get('status') not in {'completed','failed'} for e in turn_history
                             if e['sessionUpdate']=='tool_call')

                response_history = turn_history
                if not resumed_boundary_confirmed:
                    response_history = list(deg_sections.get(current_interaction, []))
                elif interaction_body is not None:
                    boundary = next((index for index,e in enumerate(turn_history)
                        if e['sessionUpdate']=='user_message_chunk' and interaction_body in e['content']['text']), None)
                    if boundary is not None:
                        response_history = turn_history[boundary+1:]
                    elif getattr(owner, 'history_degraded', False):
                        response_history = _extract_post_dialogue_entries(turn_history, pre_dialogue_history)[1]
                    else:
                        response_history = []
                if mode == 'teamwork' and any(e.get('title') in handoff_names for e in fresh
                        if e['sessionUpdate']=='tool_call'):
                    response_floor = response_history
                if response_floor:
                    try:
                        response_history = first_replay(response_floor, response_history,
                            call_site='response_floor_replay', interaction_seq=current_interaction)
                    except ProtocolError:
                        if not getattr(owner, 'history_degraded', False):
                            raise
                        last_handoff = max(
                            (i for i, e in enumerate(response_history)
                             if e.get('sessionUpdate') == 'tool_call' and e.get('title') in handoff_names),
                            default=-1,
                        )
                        if last_handoff >= 0:
                            response_history = response_history[last_handoff + 1:]
                result_decoder = EventDecoder()
                for item in response_history:
                    result_decoder.feed(dict(params=dict(update=item)))
                if not turn_history and not decoder.final_answer.strip() and not live_interaction_answers.get(current_interaction, '').strip():
                    raise ProtocolError('Native response has no final answer; outcome unknown')
                if (not active and not getattr(owner, 'fatal_observed', False)
                        and not pending_question(decoder) and not pending_question(result_decoder)
                        and not current.get('questions')):
                    result_decoder.finish()
                    if (getattr(owner, 'history_degraded', False)
                            and not result_decoder.tool_failed
                            and not unrecovered_live_tool_failure):
                        live_lines = live_wire_answers_by_seq.get(current_interaction, [])
                        usable_live = [ln for ln in live_lines if _is_usable_answer_line(ln)]
                        hist_lines = [ln.strip() for ln in result_decoder.final_answer.splitlines() if ln.strip()]
                        if usable_live and (
                            not hist_lines
                            or is_board_navigation_only(result_decoder.final_answer)
                            or any(ln not in hist_lines for ln in usable_live)
                        ):
                            result_decoder.final_answer = '\n'.join(live_lines) + '\n'
                        elif not hist_lines or is_board_navigation_only(result_decoder.final_answer):
                            fallback_ans = live_interaction_answers.get(current_interaction, '').strip()
                            if fallback_ans and not is_board_navigation_only(fallback_ans):
                                result_decoder.final_answer = fallback_ans + '\n'
                    if unrecovered_live_tool_failure and not result_decoder.final_answer.strip():
                        result_decoder.tool_failed = True
                    # This native response is navigation/progress, not the requested result.
                    board_hint = is_board_navigation_only(result_decoder.final_answer)
                    handoff_only = bool(response_floor) and not result_decoder.final_answer.strip()
                    if not resumed_boundary_confirmed:
                        has_delegation = any(
                            e.get('sessionUpdate') == 'tool_call' and e.get('title') in ({'swarm'} | handoff_names)
                            for e in turn_history
                        )
                        result_decoder.stop_reason = 'end_turn'
                        diag_note = outcome_note(result_decoder, 0, 'completed')
                        if (
                            unrecovered_live_tool_failure
                            or diag_note
                            or (not has_delegation and (board_hint or handoff_only or not result_decoder.final_answer.strip()))
                        ):
                            raise ProtocolError(
                                diag_note or 'Native resumed turn boundary missing; reconciliation required',
                                history_diagnostic=owner.last_history_diagnostic,
                            )
                    if board_hint or handoff_only:
                        observation = store.get(task['id']).get('native_observation', {})
                        observation['response_pending_reason'] = (
                            'board_navigation_only' if board_hint else 'native_handoff_only')
                        store.update(task['id'],native_observation=observation)
                        review()
                        owner.drain_pending(check_cancel=check_cancel)
                        time.sleep(.1)
                        continue
                    if not result_decoder.final_answer.strip():
                        review()
                        owner.drain_pending(check_cancel=check_cancel)
                        time.sleep(.1)
                        continue
                    # A reviewed, unbound input can replace an incomplete interview,
                    # but only its exact replay boundary plus a final response proves
                    # reception. Queuing/return alone must not discard a question.
                    for deferred in list(deferred_questions):
                        command = current.get('commands', {}).get(deferred.get('replacement_command'), {})
                        index = deferred['index']+1
                        if (command.get('status') == 'returned' and command.get('question_id') is None
                                and index < len(history) and history[index]['sessionUpdate'] == 'user_message_chunk'
                                and history[index]['content']['text'] == deferred.get('replacement_body')):
                            deferred_questions.remove(deferred)
                            emit('stage', 'Incomplete question superseded by returned dialogue '+command['command_id']+
                                ' and its final public response; not a token answer.',
                                interaction_seq=deferred['interaction_seq'])
                    if deferred_questions:
                        review()
                        owner.drain_pending(check_cancel=check_cancel)
                        time.sleep(.1)
                        continue
                    observation = current['native_observation']
                    observation['question_blockers'] = []
                    store.update(task['id'], native_observation=observation)
                    for kind,text in decoder.finish():
                        emit(kind,text)
                    if not store.native_input_seal(*binding, current_interaction):
                        continue
                    # Content failure does not erase the proven protocol settlement.
                    # Reuse single-mode policy only after the existing safe boundary.
                    result_decoder.stop_reason = 'end_turn'
                    note = outcome_note(result_decoder, 0, 'completed')
                    if not note and unrecovered_live_tool_failure:
                        note = ('AGY tool failed without a final answer although the protocol ended '
                                'end_turn with exit 0; turn not completed, outcome unknown, no automatic replay. '
                                'Tool failure cause unknown; permission decisions are not observable in the public Shared Runtime events.')
                    state = 'unknown' if note else 'completed'
                    if not note:
                        store.update(task['id'], native_result=dict(turn=task['turn'],interaction_seq=current_interaction,
                            result=result_decoder.final_answer.strip(), source='native-public-response'))
                    unloaded = unload('completion')
                    store.native_cleanup_commit(*binding,cleanup_token,dict(kind='native-mode-settled',mode=mode,
                        task=task['id'],turn=task['turn'],launch_id=task['launch_id'],epoch=task.get('control_epoch',1),
                        session_id=owner.session_id,session_unloaded=unloaded,outcome=state,
                        evidence='public-response-and-session-unload',all_descendants_proven_stopped=False))
                    store.update(task['id'],native_phase='settled',native_workers='unobserved')
                    settled_result = TurnResult(state, 0, 'end_turn', note)
                    return settled_result
                review()
                # Observation interval, never an execution deadline or generation prompt.
                for _ in range(20):
                    owner.drain_pending(check_cancel=check_cancel)
                    current = store.get(task['id'])
                    if (owner.pending_drained or current['state'] == 'cancelling'
                            or any(c.get('kind')=='dialogue' and c.get('status')=='accepted'
                                   for c in current.get('commands', {}).values())):
                        break
                    time.sleep(.1)
    except (ProtocolError, OSError) as exc:
        abnormal_exit = True
        if 'deferred_questions' in locals() and deferred_questions:
            observation = store.get(task['id']).get('native_observation', {})
            reason = ('history_ambiguous' if str(exc).startswith('Deferred question history ambiguous;')
                      else 'unresolved_question')
            observation['question_blockers'] = [dict(interaction_seq=d['interaction_seq'], reason=reason,
                available_actions=['inspect_origin', 'reconcile_hold'], blocks_completion=True) for d in deferred_questions]
            store.update(task['id'], native_observation=observation)
        if isinstance(exc, ProtocolError):
            note = str(exc)
        else:
            phase = getattr(owner, 'last_rpc_phase', None) or ('post_submit' if submitted else 'setup')
            note = (f'Native ACP transport I/O error during {phase} '
                    f'[reason=acp_transport_io_error phase={phase} submitted={bool(submitted)}]; '
                    'effects unknown, no automatic replay')
        if getattr(exc, 'history_diagnostic', None) is not None:
            diag = dict(exc.history_diagnostic, turn=task['turn'])
            if diag.get('interaction_seq') is None and 'current_interaction' in locals():
                diag['interaction_seq'] = current_interaction
            if owner is not None:
                diag.setdefault('drained_before_load', getattr(owner, 'last_load_drained', 0))
                diag.setdefault('load_retried', getattr(owner, 'last_load_retried', False))
            observation = dict(store.get(task['id']).get('native_observation') or {})
            observation.setdefault('turn', task['turn'])
            observation['history_diagnostic'] = diag
            if 'binding' in locals():
                store.native_owner_diagnostics(*binding, native_observation=observation)
            else:
                store.update(task['id'], native_observation=observation)
            formatted = _format_history_diagnostic(diag)
            if formatted:
                note = note + ' ' + formatted
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
        if owner is not None and getattr(owner, 'request_cancel_sent', False):
            cancellation_unverified = True
            if not store.get(task['id']).get('native_cancel'):
                nc = dict(foreground='unsettled', stop_reason=None,
                    session_unloaded=False, resumable=False, workers='unknown',
                    all_descendants_proven_stopped=False,
                    write_lock_retained=bool(task.get('write')))
                if 'binding' in locals():
                    store.native_owner_diagnostics(*binding, native_cancel=nc)
                else:
                    store.update(task['id'], native_cancel=nc)
        store.update(task['id'], native_phase='unknown', native_workers='unknown' if submitted else 'not_submitted')
        return TurnResult('unknown', None, None, note)
    finally:
        if 'binding' in locals():
            try:
                store.native_input_end(*binding)
            except ValueError:
                pass  # A stale owner cannot mutate its successor or release fence.
        if owner is not None:
            no_force = bool(
                cancellation_unverified
                or abnormal_exit
                or settled_result is None
                or settled_result.state == 'unknown'
            )
            exit_receipt = owner.stop(force=False) if no_force else owner.stop()
            try:
                store.native_owner_diagnostics(*binding,native_owner_exit=exit_receipt,
                                               native_permission_decisions=owner.permission_decisions)
            except ValueError:
                pass  # Closed/replaced identity cannot be reopened by final diagnostics.
