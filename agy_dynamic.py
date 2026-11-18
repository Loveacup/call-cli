"""Dynamic mode on the existing native connection; no child-event inference."""
import hashlib
import json
import time
import uuid

try:
    from .agy_native import (
        ProtocolError, snapshot, snapshot_tolerant, additions, first_replay, pending_question,
        _DEGRADABLE_HISTORY_REASONS, _KNOWN_TOOL_STATUSES, _agent_chunks,
        _check_degraded_drained_tools, _extract_post_dialogue_entries, _has_completed_fatal_line,
        _iter_retry_streams, _reconcile_drained, _safe_tool_status, _salvage_wire_signals,
        _slice_after_prompt_boundary, _updates_have_fatal,
    )
    from .events import EventDecoder, safe_tool_id
    from .transport import protocol_prompt
    from .skills import guidance
    from .backends import TurnResult
    from .acpx_backend import QUIET_NOTICE_SECONDS
    from .agy import (
        AGY_FATAL_LINE, AGY_INVALID_OUTPUT_LINE, fatal_error,
        is_board_navigation_only, is_standalone_incomplete_diagnostic, outcome_note,
    )
except ImportError:
    from agy_native import (
        ProtocolError, snapshot, snapshot_tolerant, additions, first_replay, pending_question,
        _DEGRADABLE_HISTORY_REASONS, _KNOWN_TOOL_STATUSES, _agent_chunks,
        _check_degraded_drained_tools, _extract_post_dialogue_entries, _has_completed_fatal_line,
        _iter_retry_streams, _reconcile_drained, _safe_tool_status, _salvage_wire_signals,
        _slice_after_prompt_boundary, _updates_have_fatal,
    )
    from events import EventDecoder, safe_tool_id
    from transport import protocol_prompt
    from skills import guidance
    from backends import TurnResult
    from acpx_backend import QUIET_NOTICE_SECONDS
    from agy import (
        AGY_FATAL_LINE, AGY_INVALID_OUTPUT_LINE, fatal_error,
        is_board_navigation_only, is_standalone_incomplete_diagnostic, outcome_note,
    )


def _dynamic_sections(turn_replay, dialogue_boundaries):
    sections, cur = {0: []}, 0
    for entry in turn_replay:
        if entry.get('sessionUpdate') == 'user_message_chunk':
            next_seq = dialogue_boundaries.get(entry.get('content', {}).get('text'))
            if next_seq is not None and next_seq > cur:
                cur = next_seq
                sections[cur] = []
        sections[cur].append(entry)
    return sections


def _dynamic_dialogue_body(task, entry):
    """Build Dynamic CALLCLI_DIALOGUE wire text; reject blocker_interaction_seq."""
    if entry.get('blocker_interaction_seq') is not None:
        raise ProtocolError('Dynamic dialogue does not support blocker_interaction_seq; retain HOLD')
    payload = dict(task=task['id'], turn=task['turn'],
        interaction_seq=entry['interaction_seq'], command_id=entry['command_id'],
        question=entry.get('question'))
    if entry.get('question_ids') is not None:
        payload['question_ids'] = entry['question_ids']
        payload['questions'] = entry.get('questions')
    return 'CALLCLI_DIALOGUE '+json.dumps(payload)+'\n\n'+entry['prompt']


def run_dynamic(store, task, owner, emit, policy, binding, token, baseline, unload, check_cancel,
                *, cancel_cleanup=None, resumed=False):
    if baseline and not resumed:
        raise ProtocolError('Dynamic requires fresh empty history; restored workflow control unverified')
    interaction = 0
    last_notice = started = time.monotonic()
    approved_tools = {}
    native_skip = bool(task.get('execution_policy') == 'native-skip'
                       or task.get('agy_approval') == 'skip-permissions')

    def _is_usable_answer_line(line_text):
        stripped = (line_text or '').strip()
        return (bool(stripped)
                and not is_board_navigation_only(stripped)
                and is_standalone_incomplete_diagnostic(stripped) is None
                and stripped != AGY_FATAL_LINE
                and stripped != AGY_INVALID_OUTPUT_LINE)

    def review():
        nonlocal last_notice
        now = time.monotonic()
        if not owner.review_blocked and now-last_notice >= QUIET_NOTICE_SECONDS and store.get(task['id'])['state']=='running':
            emit('stage', 'Coordinator review requested (checkpoint): Dynamic pending; inspect exact task before control.',
                 quiet_notice=True, notice_kind='checkpoint', elapsed=now-started, interaction_seq=interaction)
            last_notice = now

    def permission(frame):
        params = frame['params']
        tool = params.get('toolCall', {})
        args = tool.get('rawInput', {})
        script = args.get('Script') if isinstance(args, dict) else None
        if (not isinstance(script, str) or not script or len(script.encode())>128000 or not safe_tool_id(tool.get('toolCallId'))
                or ('args' in tool and (not isinstance(tool['args'],dict) or tool['args'].get('Script')!=script))
                or (tool.get('name') or tool.get('title'))!='run_workflow'):
            owner.send(dict(id=frame['id'], result=dict(outcome=dict(outcome='cancelled'))))
            raise ProtocolError('Dynamic permission lacks bounded exact Script/tool identity; rejected, retain HOLD')
        ident = uuid.uuid4().hex
        digest = hashlib.sha256(script.encode()).hexdigest()
        if native_skip:
            allow_once = [o.get('optionId') for o in params.get('options', [])
                          if o.get('kind') == 'allow_once' and isinstance(o.get('optionId'), str)]
            allow_always = [o.get('optionId') for o in params.get('options', [])
                            if o.get('kind') == 'allow_always' and isinstance(o.get('optionId'), str)]
            if len(allow_once) > 1 or (not allow_once and len(allow_always) != 1):
                owner.send(dict(id=frame['id'], result=dict(outcome=dict(outcome='cancelled'))))
                raise ProtocolError('Dynamic requires unique native-skip approval option')
            selected = allow_once[0] if allow_once else allow_always[0]
            approved_tools[safe_tool_id(tool['toolCallId'])] = digest
            owner.send(dict(id=frame['id'], result=dict(outcome=dict(outcome='selected', optionId=selected))))
            return
        options = {o.get('kind'):o.get('optionId') for o in params.get('options', [])
                   if o.get('kind') in {'allow_once','reject_once'} and isinstance(o.get('optionId'), str)}
        if len(options)!=2 or len(params.get('options',[]))!=2 or len(set(options.values()))!=2:
            owner.send(dict(id=frame['id'], result=dict(outcome=dict(outcome='cancelled'))))
            raise ProtocolError('Dynamic requires exact single-use approval/rejection options')
        store.native_permission_open(*binding, dict(id=ident, script=script, script_sha256=digest,
            tool=safe_tool_id(tool['toolCallId']), session_id=owner.session_id))
        emit('permission', 'Review exact proposed workflow script; choose allow_once or reject_once.',
             permission_id=ident, script=script, script_sha256=digest, interaction_seq=interaction,
             attention_required=True, notification_scope='originating-session')
        waiting_started = time.monotonic()
        while True:
            if check_cancel():
                owner.send(dict(id=frame['id'], result=dict(outcome=dict(outcome='cancelled'))))
                raise ProtocolError('Dynamic cancelled during approval; child fate unverified, retain HOLD')
            entry = store.get(task['id'])['native_permission']
            if entry['status']=='decided':
                owner.approval_wait_seconds += time.monotonic()-waiting_started
                store.native_permission_state(*binding, ident, 'send_started')
                owner.send(dict(id=frame['id'], result=dict(outcome=dict(outcome='selected', optionId=options[entry['decision']]))))
                store.native_permission_state(*binding, ident, 'returned')
                if entry['decision']=='allow_once':
                    approved_tools[safe_tool_id(tool['toolCallId'])] = digest
                return
            review()
            time.sleep(.1)

    owner.review = review
    owner.permission_handler = permission
    decoder = EventDecoder()
    history = []
    published = []
    pre_dialogue_history = []
    dialogue_boundaries = {}
    tool_last_status = {}
    usable_answer_count_by_seq = {}
    failed_tools_by_seq = {}
    unrecovered_parent_tool_failure = False
    failure_hwm = None
    live_wire_answers_by_seq = {}
    segment_seen_answers_by_seq = {}
    known_segment_tools = set()

    def _reset_live_answer_segment(seq):
        live_wire_answers_by_seq[seq] = []
        segment_seen_answers_by_seq[seq] = set()

    def _record_live_answer(seq, line_text, *, dedupe_in_segment=False):
        stripped = (line_text or '').strip()
        if not stripped:
            return False
        seg_seen = segment_seen_answers_by_seq.setdefault(seq, set())
        if dedupe_in_segment and stripped in seg_seen:
            return False
        seg_seen.add(stripped)
        lst = live_wire_answers_by_seq.setdefault(seq, [])
        if not lst or lst[-1] != stripped:
            lst.append(stripped)
        return True

    def _note_tool_status(tid, status, title, seq, *, usable_count_override=None):
        nonlocal unrecovered_parent_tool_failure, failure_hwm
        if title == 'run_workflow' or status not in _KNOWN_TOOL_STATUSES:
            return
        key = tid or f'_anon_{len(tool_last_status)}'
        prev = tool_last_status.get(key)
        tool_last_status[key] = status
        if status == 'failed' and prev != 'failed':
            unrecovered_parent_tool_failure = True
            base_cnt = usable_answer_count_by_seq.get(seq, 0) if usable_count_override is None else usable_count_override
            failure_hwm = (seq, base_cnt)
            failed_tools_by_seq.setdefault(seq, set()).add(key)
            _reset_live_answer_segment(seq)

    def _note_usable_answer(seq, line_text):
        nonlocal unrecovered_parent_tool_failure
        if not _is_usable_answer_line(line_text):
            return
        cnt = usable_answer_count_by_seq.get(seq, 0) + 1
        usable_answer_count_by_seq[seq] = cnt
        if unrecovered_parent_tool_failure and failure_hwm is not None:
            f_seq, f_cnt = failure_hwm
            if seq > f_seq or (seq == f_seq and cnt > f_cnt):
                unrecovered_parent_tool_failure = False
                failed_tools_by_seq.clear()

    command = None
    body = '/run-workflow\n'+protocol_prompt(policy+guidance(task)+
        'Use native run_workflow, not Swarm. '
        + ('Execute authorized workflows natively under skip-permissions without redundant script approval prompts. '
           if native_skip else 'Present each exact Script for single-use approval. ')
        + 'Workflow child model values are native tiers (inherit/flash_lite/flash/pro), not ACP model family IDs. '
        'Do not infer authorization to change child tiers. Report only observed parent tool status and public answers; '
        'do not claim unseen child/phase/log events. No automatic retries.\n\n'+task['prompt'])
    initial_body = body
    emit('stage', 'Dynamic observation: parent tool status and parent public answer only; direct child/phase/log output unavailable.',
         interaction_seq=interaction)
    emitted_signals_by_seq = {}
    def _emit_tracked(kind, text, *, interaction_seq=0, **extra):
        if kind in {'stage', 'question', 'skill', 'answer'}:
            emitted_signals_by_seq.setdefault(interaction_seq, set()).add((kind, text))
        emit(kind, text, interaction_seq=interaction_seq, **extra)
    if resumed:
        owner.resumed_prompt_boundary = initial_body
    while True:
        if body is not None:
            live = []
            def publish(update):
                live.append(update)
                for kind, text in decoder.feed(dict(params=dict(update=update))):
                    if kind == 'answer':
                        _record_live_answer(interaction, text)
                        _note_usable_answer(interaction, text)
                    _emit_tracked(kind, text, interaction_seq=interaction)
                if update.get('sessionUpdate') == 'tool_call':
                    if update.get('tool_event') == 'tool_call':
                        if update.get('toolCallId'):
                            known_segment_tools.add(update['toolCallId'])
                        _reset_live_answer_segment(interaction)
                    _note_tool_status(update.get('toolCallId'), _safe_tool_status(update), update.get('title'), interaction)
            response, _ = owner.request('session/prompt', dict(sessionId=owner.session_id,
                prompt=[dict(type='text', text=body)]), timeout=None, check_cancel=check_cancel, on_update=publish)
            if check_cancel() or getattr(owner, 'request_cancel_sent', False):
                if command and response.get('stopReason') == 'cancelled':
                    store.native_dialogue_state(*binding, command, 'cancelled')
                if cancel_cleanup is not None:
                    return cancel_cleanup(
                        snapshot_tolerant(live) if live else history,
                        notify=not getattr(owner, 'request_cancel_sent', False),
                        first=not history,
                        stop_reason=response.get('stopReason'),
                        flush_decoder=decoder,
                    )
                raise ProtocolError('Dynamic cancel observed; cascade/child stop unverified, retain HOLD; no automatic resume')
            for kind, text in decoder.finish():
                if kind == 'answer':
                    _record_live_answer(interaction, text)
                    _note_usable_answer(interaction, text)
                _emit_tracked(kind, text, interaction_seq=interaction)
            if response.get('stopReason')!='end_turn' or getattr(owner, 'fatal_observed', False) or fatal_error(decoder.text):
                raise ProtocolError('Dynamic foreground abnormal/fatal; retain HOLD')
            if command:
                store.native_dialogue_state(*binding, command, 'returned')
                dialogue_boundaries[body] = interaction
                command = None
            published = snapshot([e for e in live if e['sessionUpdate'] != 'user_message_chunk'])
            body = None
        if check_cancel():
            if cancel_cleanup is not None:
                return cancel_cleanup(
                    history if history else published,
                    notify=not getattr(owner, 'request_cancel_sent', False),
                    first=not history,
                    stop_reason='end_turn',
                    flush_decoder=decoder,
                )
            owner.notify_cancel()
            raise ProtocolError('Dynamic cancel sent; cascade/child stop unverified, retain HOLD')
        _, replay = owner.load(reconcile_stream=True, check_cancel=check_cancel)
        if check_cancel():
            if cancel_cleanup is not None:
                return cancel_cleanup(
                    history if history else published,
                    notify=not getattr(owner, 'request_cancel_sent', False),
                    first=not history,
                    stop_reason='end_turn',
                    flush_decoder=decoder,
                )
            raise ProtocolError('Dynamic cancellation during observation; retain HOLD')
        if getattr(owner, 'fatal_observed', False):
            raise ProtocolError('Dynamic replay fatal; retain HOLD')
        if getattr(owner, 'last_history_diagnostic', None) is not None:
            owner.last_history_diagnostic.setdefault('turn', task['turn'])
            if owner.last_history_diagnostic.get('interaction_seq') is None:
                owner.last_history_diagnostic['interaction_seq'] = interaction
        if resumed:
            sliced = _slice_after_prompt_boundary(replay, initial_body)
            if sliced is None:
                raise ProtocolError('Dynamic resumed turn boundary missing; retain HOLD')
            _, turn_replay = sliced
        else:
            turn_replay = replay
        if _has_completed_fatal_line(turn_replay):
            owner.fatal_observed = True
            raise ProtocolError('Dynamic replay fatal; retain HOLD')
        live_tools = {e['toolCallId']: e for e in published if e['sessionUpdate'] == 'tool_call'}
        for d_entry in getattr(owner, 'pending_drained', ()):
            if d_entry.get('sessionUpdate') == 'tool_call' and d_entry.get('toolCallId'):
                live_tools.setdefault(d_entry['toolCallId'], d_entry)
        replay_tools = {e['toolCallId']: e for e in turn_replay if e['sessionUpdate'] == 'tool_call'}
        if any(e.get('title')=='run_workflow' and (ident not in replay_tools
                or replay_tools[ident].get('title')!='run_workflow') for ident,e in live_tools.items()):
            raise ProtocolError('Dynamic workflow tool identity changed; retain HOLD')
        tools = [e for e in turn_replay if e['sessionUpdate']=='tool_call']
        workflow = [e for e in tools if e.get('title')=='run_workflow']
        if any(not e.get('workflow_script_sha256')
               or e.get('workflow_script_sha256') != owner.workflow_identity.get(e['toolCallId'])
               for e in workflow):
            raise ProtocolError('Dynamic workflow lacks exact observed Script identity; retain HOLD')
        if native_skip:
            if any(e['toolCallId'] in approved_tools
                   and e.get('workflow_script_sha256') != approved_tools[e['toolCallId']]
                   for e in workflow):
                raise ProtocolError('Dynamic workflow script diverged from native permission; retain HOLD')
        elif any(e['toolCallId'] not in approved_tools or e.get('workflow_script_sha256')!=approved_tools[e['toolCallId']] for e in workflow):
            raise ProtocolError('Dynamic workflow lacks exact owner-approved script; retain HOLD')
        try:
            fresh = additions(history, turn_replay, background_growth=True, call_site='dynamic_load', interaction_seq=interaction)
            public_fresh = [e for e in fresh if e['sessionUpdate'] != 'user_message_chunk'] if not history and resumed else fresh
            delta = first_replay(published, public_fresh, call_site='dynamic_replay', interaction_seq=interaction)
            prior_messages = _agent_chunks(history) + _agent_chunks(published)
            persisted_prior_slots = _agent_chunks(history)
            _reconcile_drained(
                owner.pending_drained, turn_replay, delta, (),
                retry_state=getattr(owner, 'pending_retry_state', None),
                prior_messages=prior_messages,
                persisted_prior_slots=persisted_prior_slots,
                call_site='dynamic_drain_reconcile', interaction_seq=interaction,
            )
            owner.pending_drained.clear()
            owner.pending_retry_state = None
            owner.drained_public_text = ''
            owner.drained_line_pending = ''
            appended = turn_replay[len(history):]
            for update in delta:
                is_appended = update in appended
                for kind, text in decoder.feed(dict(params=dict(update=update)), text_boundary=is_appended or not history):
                    if kind == 'answer':
                        _record_live_answer(interaction, text)
                        _note_usable_answer(interaction, text)
                    _emit_tracked(kind, text, interaction_seq=interaction)
                if update.get('sessionUpdate') == 'tool_call':
                    if is_appended:
                        tid = update.get('toolCallId')
                        if not tid or tid not in known_segment_tools:
                            if tid:
                                known_segment_tools.add(tid)
                            _reset_live_answer_segment(interaction)
                    _note_tool_status(update.get('toolCallId'), _safe_tool_status(update), update.get('title'), interaction)
        except ProtocolError as hist_exc:
            h_diag = getattr(hist_exc, 'history_diagnostic', None)
            reason = h_diag.get('reason_code') if isinstance(h_diag, dict) else None
            if reason not in _DEGRADABLE_HISTORY_REASONS:
                raise
            def _split_attempt_stream(entries):
                items = list(entries or ())
                if not items:
                    return [], []
                def _is_session_user(entry):
                    if entry.get('sessionUpdate') != 'user_message_chunk':
                        return False
                    utext = entry.get('content', {}).get('text', '')
                    if not isinstance(utext, str) or utext in dialogue_boundaries or utext.startswith('CALLCLI_DIALOGUE '):
                        return False
                    if resumed and (utext == initial_body or utext.endswith(initial_body)):
                        return False
                    return True
                first_session_user = next((i for i, e in enumerate(items) if _is_session_user(e)), None)
                inflight = items[:first_session_user] if first_session_user is not None and first_session_user > 0 else []
                replay_part = items[first_session_user:] if first_session_user is not None else items
                if resumed:
                    if any(e.get('sessionUpdate') == 'user_message_chunk' for e in replay_part):
                        sliced_att = _slice_after_prompt_boundary(replay_part, initial_body)
                        replay_part = sliced_att[1] if sliced_att is not None else []
                    else:
                        replay_part = []
                return inflight, replay_part
            def _iter_current_retry_streams(state):
                if not isinstance(state, dict):
                    return
                if state.get('prior') is not None:
                    yield from _iter_current_retry_streams(state['prior'])
                if state.get('d1_updates'):
                    yield state['d1_updates']
                if state.get('attempt_updates'):
                    inflight, replay_part = _split_attempt_stream(state['attempt_updates'])
                    if inflight:
                        yield inflight
                    if replay_part:
                        yield replay_part
                if state.get('d2_updates'):
                    yield state['d2_updates']
            for stream in _iter_current_retry_streams(getattr(owner, 'pending_retry_state', None)):
                if _updates_have_fatal(stream, fatal_error):
                    owner.fatal_observed = True
            if (getattr(owner, 'fatal_observed', False)
                    or _updates_have_fatal(owner.pending_drained, fatal_error, require_terminated=True)
                    or _updates_have_fatal(turn_replay, fatal_error)):
                owner.fatal_observed = True
                raise ProtocolError('Dynamic replay fatal; retain HOLD')
            _check_degraded_drained_tools(
                owner.pending_drained, turn_replay,
                call_site='dynamic_drain_reconcile', interaction_seq=interaction,
            )
            diag = dict(h_diag, turn=task['turn'])
            if diag.get('interaction_seq') is None:
                diag['interaction_seq'] = interaction
            diag.setdefault('drained_before_load', getattr(owner, 'last_load_drained', 0))
            diag.setdefault('load_retried', getattr(owner, 'last_load_retried', False))
            owner.history_degraded = True
            owner.last_history_diagnostic = diag
            def _walk_salvage_slices(state):
                if not isinstance(state, dict):
                    return
                if state.get('prior') is not None:
                    yield from _walk_salvage_slices(state['prior'])
                if state.get('d1_updates'):
                    yield interaction, state['d1_updates'], 'drain'
                if state.get('attempt_updates'):
                    inflight, replay_part = _split_attempt_stream(state['attempt_updates'])
                    if inflight:
                        yield interaction, inflight, 'inflight'
                    if replay_part:
                        secs = _dynamic_sections(replay_part, dialogue_boundaries)
                        if interaction > 0 and interaction not in secs and secs:
                            m_seq = max(secs)
                            pre_p, post_p = _extract_post_dialogue_entries(secs[m_seq], pre_dialogue_history)
                            secs[m_seq] = pre_p
                            secs[interaction] = post_p
                        for a_seq, a_sec in sorted(secs.items()):
                            if a_sec:
                                yield a_seq, a_sec, 'replay'
                if state.get('d2_updates'):
                    yield interaction, state['d2_updates'], 'drain'
            retry_state = getattr(owner, 'pending_retry_state', None)
            salvage_slices = list(_walk_salvage_slices(retry_state))
            if owner.pending_drained:
                if retry_state is None:
                    salvage_slices.append((interaction, owner.pending_drained, 'drain'))
                else:
                    drained_tools = [e for e in owner.pending_drained if e.get('sessionUpdate') == 'tool_call']
                    if drained_tools:
                        salvage_slices.append((interaction, drained_tools, 'drain'))
            def _iter_stream_steps(entries):
                buf = []
                for item in (entries or ()):
                    buf.append(item)
                    if item.get('sessionUpdate') == 'tool_call':
                        for sig in _salvage_wire_signals(buf):
                            yield 'signal', sig
                        buf = []
                        yield 'tool', item
                if buf:
                    for sig in _salvage_wire_signals(buf):
                        yield 'signal', sig
            for s_seq, stream, s_source in salvage_slices:
                is_live_slice = s_source in {'drain', 'inflight'}
                for step_kind, payload in _iter_stream_steps(stream):
                    if step_kind == 'tool':
                        s_entry = payload
                        tid = s_entry.get('toolCallId')
                        if is_live_slice and s_entry.get('tool_event') == 'tool_call' and not s_entry.get('_retry_create'):
                            if not tid or tid not in known_segment_tools:
                                if tid:
                                    known_segment_tools.add(tid)
                                _reset_live_answer_segment(s_seq)
                        if is_live_slice or s_seq == interaction:
                            _note_tool_status(tid, _safe_tool_status(s_entry), s_entry.get('title'), s_seq)
                    else:
                        sig_kind, sig_text = payload
                        _emit_tracked(sig_kind, sig_text, interaction_seq=s_seq, _degraded_dedupe=True)
                        if is_live_slice and sig_kind == 'answer':
                            if _record_live_answer(s_seq, sig_text, dedupe_in_segment=True):
                                _note_usable_answer(s_seq, sig_text)
            deg_secs = _dynamic_sections(turn_replay, dialogue_boundaries)
            collapsed = False
            if interaction > 0 and interaction not in deg_secs:
                collapsed = True
                m_seq = max(deg_secs)
                pre_p, post_p = _extract_post_dialogue_entries(deg_secs[m_seq], pre_dialogue_history)
                deg_secs[m_seq] = pre_p
                deg_secs[interaction] = post_p
            cur_sec = deg_secs.get(interaction, [])
            sec_dec = EventDecoder()
            last_failed_idx = max(
                (i for i, e in enumerate(cur_sec)
                 if e.get('sessionUpdate') == 'tool_call' and e.get('title') != 'run_workflow'
                 and _safe_tool_status(e) == 'failed'),
                default=-1,
            )
            sec_failed_keys = {
                e.get('toolCallId') for e in cur_sec
                if e.get('sessionUpdate') == 'tool_call'
                and e.get('title') != 'run_workflow'
                and _safe_tool_status(e) == 'failed'
                and e.get('toolCallId')
            }
            sec_usable_idx = 0
            def _note_dyn_deg(a_text, after_failed):
                nonlocal sec_usable_idx, unrecovered_parent_tool_failure
                if not _is_usable_answer_line(a_text):
                    return
                sec_usable_idx += 1
                if not after_failed:
                    return
                if unrecovered_parent_tool_failure and failure_hwm is not None:
                    f_seq, f_cnt = failure_hwm
                    if interaction > f_seq or (
                        interaction == f_seq
                        and sec_usable_idx > f_cnt
                        and failed_tools_by_seq.get(f_seq, set()) <= sec_failed_keys
                    ):
                        usable_answer_count_by_seq[interaction] = max(usable_answer_count_by_seq.get(interaction, 0), sec_usable_idx)
                        unrecovered_parent_tool_failure = False
                        failed_tools_by_seq.clear()
                else:
                    usable_answer_count_by_seq[interaction] = max(usable_answer_count_by_seq.get(interaction, 0), sec_usable_idx)
            for idx, d_item in enumerate(cur_sec):
                for kind, text in sec_dec.feed(dict(params=dict(update=d_item)), text_boundary=(idx < len(cur_sec) - 1)):
                    if kind == 'answer':
                        _note_dyn_deg(text, idx > last_failed_idx)
                    _emit_tracked(kind, text, interaction_seq=interaction, _degraded_dedupe=True,
                                  _suppress_prior_question=collapsed)
                if d_item.get('sessionUpdate') == 'tool_call':
                    _note_tool_status(d_item.get('toolCallId'), _safe_tool_status(d_item), d_item.get('title'),
                                      interaction, usable_count_override=sec_usable_idx)
            if pending_question(sec_dec):
                decoder.pending = sec_dec.pending
                decoder.pending_overflow = sec_dec.pending_overflow
                decoder.final_answer = sec_dec.final_answer
            else:
                for kind, text in sec_dec.finish():
                    if kind == 'answer':
                        _note_dyn_deg(text, len(cur_sec) > last_failed_idx)
                    _emit_tracked(kind, text, interaction_seq=interaction, _degraded_dedupe=True,
                                  _suppress_prior_question=collapsed)
                decoder = sec_dec
            owner.pending_drained.clear()
            owner.pending_retry_state = None
            owner.drained_public_text = ''
            owner.drained_line_pending = ''
        history = turn_replay
        published = []
        active = any(e.get('status') not in {'completed','failed'} for e in tools)
        current = store.get(task['id'])
        prior_obs = current.get('native_observation') or {}
        obs_payload = dict(turn=task['turn'], mode='dynamic', interaction_seq=interaction,
            workflow_tools=[dict(id=e['toolCallId'],status=e.get('status')) for e in workflow],
            output_source='parent-public-answer', child_events='unavailable', phase_logs='unavailable',
            all_descendants_proven_stopped=False, input_available=True,
            input_semantics='queued-until-foreground-return; no hot script/child steer',
            cancellation='foreground-settle-and-session-unload', last_success_at=time.time())
        if getattr(owner, 'history_degraded', False) or prior_obs.get('history_degraded'):
            obs_payload['history_degraded'] = True
        hist_diag_val = getattr(owner, 'last_history_diagnostic', None) or prior_obs.get('history_diagnostic')
        if hist_diag_val is not None:
            obs_payload['history_diagnostic'] = hist_diag_val
        store.update(task['id'], native_snapshot=[e for e in replay if e['sessionUpdate'] != 'user_message_chunk'],
            native_phase='dynamic_pending', native_observation=obs_payload)
        if (getattr(owner, 'fatal_observed', False)
                or _has_completed_fatal_line(turn_replay)
                or fatal_error('\n'.join(e['content']['text'] for e in turn_replay if e['sessionUpdate']=='agent_message_chunk'))):
            raise ProtocolError('Dynamic replay fatal; retain HOLD')
        pending_q = decoder.pending.lstrip()
        if pending_q and (pending_q.startswith('CALLCLI_QUESTION') or 'CALLCLI_QUESTION'.startswith(pending_q)):
            raise ProtocolError('Dynamic incomplete question requires reconciliation; no input attribution guess, retain HOLD')
        owner.drain_pending(check_cancel=check_cancel)
        if check_cancel():
            continue
        if owner.pending_drained:
            continue
        entry = store.native_dialogue_claim(*binding)
        if entry is not None:
            body = _dynamic_dialogue_body(task, entry)
            pre_dialogue_history = list(turn_replay)
            interaction = entry['interaction_seq']
            command = entry['command_id']
            store.native_dialogue_state(*binding, command, 'send_started')
            decoder = EventDecoder()
            continue
        if not active and workflow and not current.get('questions'):
            for kind,text in decoder.finish():
                if kind == 'answer' and not getattr(owner, 'history_degraded', False):
                    _record_live_answer(interaction, text)
                    _note_usable_answer(interaction, text)
                _emit_tracked(kind,text,interaction_seq=interaction)
            if getattr(owner, 'history_degraded', False) and not unrecovered_parent_tool_failure:
                live_lines = live_wire_answers_by_seq.get(interaction, [])
                usable_live = [ln for ln in live_lines if _is_usable_answer_line(ln)]
                hist_lines = [ln.strip() for ln in decoder.final_answer.splitlines() if ln.strip()]
                if usable_live and (
                    not hist_lines
                    or is_board_navigation_only(decoder.final_answer)
                    or any(ln not in hist_lines for ln in usable_live)
                ):
                    decoder.final_answer = '\n'.join(live_lines) + '\n'
            if not decoder.final_answer.strip() or is_board_navigation_only(decoder.final_answer) or unrecovered_parent_tool_failure:
                raise ProtocolError('Dynamic terminal output absent after last tool; retain context/HOLD')
            if not store.native_input_seal(*binding, interaction):
                continue
            decoder.stop_reason = 'end_turn'
            diag_note = outcome_note(decoder, 0, 'completed')
            success = all(e.get('status')=='completed' for e in workflow) and not diag_note
            if success:
                store.update(task['id'], native_result=dict(turn=task['turn'], interaction_seq=interaction,
                    result=decoder.final_answer.strip(), source='native-public-response'))
            unloaded = unload('dynamic-settled')
            receipt = dict(kind='native-mode-settled', mode='dynamic', task=task['id'], turn=task['turn'],
                launch_id=task['launch_id'], epoch=task.get('control_epoch',1), session_id=owner.session_id,
                session_unloaded=unloaded, outcome='completed' if success else 'unknown',
                evidence='parent-end-turn-complete-tool-history-and-unload', all_descendants_proven_stopped=False)
            store.native_cleanup_commit(*binding, token, receipt)
            return TurnResult('completed',0,'end_turn') if success else TurnResult(
                'unknown', 0 if diag_note else None, 'end_turn' if diag_note else None,
                diag_note or 'Dynamic workflow failed or parent output absent; no business success')
        if not active and not workflow and not current.get('questions'):
            raise ProtocolError('Dynamic omitted workflow execution evidence; retain HOLD')
        review()
        time.sleep(.1)
