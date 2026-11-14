"""Dynamic mode on the existing native connection; no child-event inference."""
import hashlib
import json
import time
import uuid

try:
    from .agy_native import ProtocolError, snapshot, additions, first_replay
    from .events import EventDecoder, safe_tool_id
    from .transport import protocol_prompt
    from .skills import guidance
    from .backends import TurnResult
    from .acpx_backend import QUIET_NOTICE_SECONDS
    from .agy import fatal_error
except ImportError:
    from agy_native import ProtocolError, snapshot, additions, first_replay
    from events import EventDecoder, safe_tool_id
    from transport import protocol_prompt
    from skills import guidance
    from backends import TurnResult
    from acpx_backend import QUIET_NOTICE_SECONDS
    from agy import fatal_error


def run_dynamic(store, task, owner, emit, policy, binding, token, baseline, unload, check_cancel):
    if baseline:
        raise ProtocolError('Dynamic requires fresh empty history; restored workflow control unverified')
    interaction = 0
    last_notice = started = time.monotonic()
    approved_tools = {}

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
    command = None
    body = '/run-workflow\n'+protocol_prompt(policy+guidance(task)+
        'Use native run_workflow, not Swarm. Present each exact Script for single-use approval. '
        'Workflow child model values are native tiers (inherit/flash_lite/flash/pro), not ACP model family IDs. '
        'Do not infer authorization to change child tiers. Report only observed parent tool status and public answers; '
        'do not claim unseen child/phase/log events. No automatic retries.\n\n'+task['prompt'])
    emit('stage', 'Dynamic observation: parent tool status and parent public answer only; direct child/phase/log output unavailable.',
         interaction_seq=interaction)
    while True:
        if body is not None:
            live = []
            def publish(update):
                live.append(update)
                for kind, text in decoder.feed(dict(params=dict(update=update))):
                    emit(kind, text, interaction_seq=interaction)
            response, _ = owner.request('session/prompt', dict(sessionId=owner.session_id,
                prompt=[dict(type='text', text=body)]), timeout=None, check_cancel=check_cancel, on_update=publish)
            for kind, text in decoder.finish():
                emit(kind, text, interaction_seq=interaction)
            if check_cancel() or owner.request_cancel_sent:
                raise ProtocolError('Dynamic cancel observed; cascade/child stop unverified, retain HOLD; no automatic resume')
            if response.get('stopReason')!='end_turn' or fatal_error(decoder.text):
                raise ProtocolError('Dynamic foreground abnormal/fatal; retain HOLD')
            if command:
                store.native_dialogue_state(*binding, command, 'returned')
                command = None
            published = snapshot(live)
            body = None
        if check_cancel():
            owner.notify_cancel()
            raise ProtocolError('Dynamic cancel sent; cascade/child stop unverified, retain HOLD')
        _, replay = owner.load()
        if check_cancel():
            raise ProtocolError('Dynamic cancellation during observation; retain HOLD')
        fresh = additions(history, replay)
        live_tools = {e['toolCallId']:e for e in published if e['sessionUpdate']=='tool_call'}
        replay_tools = {e['toolCallId']:e for e in replay if e['sessionUpdate']=='tool_call'}
        if any(e.get('title')=='run_workflow' and (ident not in replay_tools
                or replay_tools[ident].get('title')!='run_workflow') for ident,e in live_tools.items()):
            raise ProtocolError('Dynamic workflow tool identity changed; retain HOLD')
        delta = first_replay(published, fresh)
        for update in delta:
            for kind, text in decoder.feed(dict(params=dict(update=update))):
                emit(kind, text, interaction_seq=interaction)
        history = replay
        published = []
        tools = [e for e in replay if e['sessionUpdate']=='tool_call']
        workflow = [e for e in tools if e.get('title')=='run_workflow']
        if any(e['toolCallId'] not in approved_tools or e.get('workflow_script_sha256')!=approved_tools[e['toolCallId']] for e in workflow):
            raise ProtocolError('Dynamic workflow lacks exact owner-approved script; retain HOLD')
        active = any(e.get('status') not in {'completed','failed'} for e in tools)
        current = store.get(task['id'])
        store.update(task['id'], native_snapshot=replay, native_phase='dynamic_pending',
            native_observation=dict(turn=task['turn'], mode='dynamic', interaction_seq=interaction,
                workflow_tools=[dict(id=e['toolCallId'],status=e.get('status')) for e in workflow],
                output_source='parent-public-answer', child_events='unavailable', phase_logs='unavailable',
                all_descendants_proven_stopped=False, input_available=True,
                input_semantics='queued-until-foreground-return; no hot script/child steer',
                cancellation='cascade-unverified-HOLD', last_success_at=time.time()))
        if fatal_error('\n'.join(e['content']['text'] for e in replay if e['sessionUpdate']=='agent_message_chunk')):
            raise ProtocolError('Dynamic replay fatal; retain HOLD')
        pending_question = decoder.pending.lstrip()
        if pending_question and (pending_question.startswith('CALLCLI_QUESTION') or 'CALLCLI_QUESTION'.startswith(pending_question)):
            raise ProtocolError('Dynamic incomplete question requires reconciliation; no input attribution guess, retain HOLD')
        entry = store.native_dialogue_claim(*binding)
        if entry is not None:
            interaction = entry['interaction_seq']
            command = entry['command_id']
            store.native_dialogue_state(*binding, command, 'send_started')
            body = 'CALLCLI_DIALOGUE '+json.dumps(dict(task=task['id'], turn=task['turn'],
                interaction_seq=interaction, command_id=command, question=entry.get('question')))+'\n\n'+entry['prompt']
            decoder = EventDecoder()
            continue
        if not active and workflow and not current.get('questions'):
            for kind,text in decoder.finish():
                emit(kind,text,interaction_seq=interaction)
            if not decoder.final_answer.strip():
                raise ProtocolError('Dynamic terminal output absent after last tool; retain context/HOLD')
            if not store.native_input_seal(*binding, interaction):
                continue
            success = all(e.get('status')=='completed' for e in workflow)
            unloaded = unload('dynamic-settled')
            receipt = dict(kind='native-mode-settled', mode='dynamic', task=task['id'], turn=task['turn'],
                launch_id=task['launch_id'], epoch=task.get('control_epoch',1), session_id=owner.session_id,
                session_unloaded=unloaded, outcome='completed' if success else 'unknown',
                evidence='parent-end-turn-complete-tool-history-and-unload', all_descendants_proven_stopped=False)
            store.native_cleanup_commit(*binding, token, receipt)
            return TurnResult('completed',0,'end_turn') if success else TurnResult('unknown',None,None,
                'Dynamic workflow failed or parent output absent; no business success')
        if not active and not workflow and not current.get('questions'):
            raise ProtocolError('Dynamic omitted workflow execution evidence; retain HOLD')
        review()
        time.sleep(.1)
