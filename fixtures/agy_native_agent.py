#!/usr/bin/env python3
"""Owned synthetic ACP endpoint for native-owner lifecycle tests."""
import json
import os
from pathlib import Path
import sys
import time
import uuid

root = Path(os.environ['AGY_NATIVE_FIXTURE'])
session = '11111111-1111-4111-8111-111111111111'
model = os.environ.get('AGY_NATIVE_INITIAL_MODEL', 'gemini-4-carbon')
closed = (root/'closed-session').exists()
effort = 'medium'
prompted = False
tail_loads = 0
turn = int(os.environ.get('AGY_NATIVE_TURN', '1'))
saved = json.loads((root/'history.json').read_text()) if (root/'history.json').exists() else []
captured = None
scenario = json.loads((root/'scenario.json').read_text()) if (root/'scenario.json').exists() else None
replay_index = 0
dialogue_history = []
dialogue_inputs = []
dialogue_commands = set()
answer_map = []
deferred_seq = 0
deferred_slots = {}
deferred_answers = []
review_prompts = 0
review_loads = 0


def review_gate(name):
    if not scenario or name not in scenario.get('review_gates', []):
        return False
    import select
    (root/('waiting-'+name)).touch()
    while not (root/('release-'+name)).exists():
        if (root/('active-'+name)).exists():
            update(dict(sessionUpdate='tool_call_update', toolCallId='read', status='completed'))
        if select.select([sys.stdin], [], [], .01)[0]:
            pending = json.loads(sys.stdin.readline())
            assert pending.get('method') == 'session/cancel', pending.get('method')
            with (root/'requests.jsonl').open('a') as handle:
                handle.write(json.dumps(pending)+'\n')
            (root/'cancelled').touch()
            return True
    return False


def send(value):
    print(json.dumps(dict(jsonrpc='2.0', **value)), flush=True)

def metadata():
    if (root/'catalog.json').exists():
        catalog = json.loads((root/'catalog.json').read_text())
        if model in catalog['selected']:
            return dict(sessionId=session, **catalog['selected'][model])
        metadata = catalog['initial']
        metadata['models']['currentModelId'] = model+'-'+effort
        for option in metadata['configOptions']:
            if option['id']=='model': option['currentValue']=model
            if option['id']=='thought_level': option['currentValue']=effort
        return dict(sessionId=session, **metadata)
    standalone=('gemini-pro-orchestrator','gemini-pro-orchestrator-argon')
    config=[dict(id='model',currentValue=model,options=[dict(value=x,name=x) for x in
        ('gemini-4-carbon','gemini-4-hydrogen','gemini-4-argon',*standalone)])]
    if model not in standalone:
        config.append(dict(id='thought_level',currentValue=effort,options=[dict(value=x) for x in ('low','medium')]))
    return dict(sessionId=session,models=dict(currentModelId=model if model in standalone else model+'-'+effort,
        availableModels=[dict(modelId=x+'-'+level) for x in ('gemini-4-carbon','gemini-4-hydrogen','gemini-4-argon')
            for level in ('low','medium')]+[dict(modelId=x) for x in standalone]),configOptions=config)

def update(value):
    if os.environ.get('AGY_NATIVE_NO_RECEIPTS'):
        value.pop('rawOutput', None)
    if captured is not None:
        captured.append(value)
    send(dict(method='session/update', params=dict(sessionId=session, update=value)))

def text(value):
    update(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text=value)))

for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get('method')
    with (root/'requests.jsonl').open('a') as f:
        f.write(json.dumps(msg)+'\n')
    if method == 'initialize':
        result = dict(protocolVersion=1, agentCapabilities=dict(loadSession=True, sessionCapabilities=dict(close={})))
    elif method == 'session/new':
        if closed:
            session = str(uuid.uuid4())
            closed = False
            saved = []
            (root/'closed-session').unlink()
        result = metadata()
        if os.environ.get('AGY_DYNAMIC_FIXTURE') and os.environ['AGY_DYNAMIC_FIXTURE']!='no-command':
            update(dict(sessionUpdate='available_commands_update', availableCommands=[dict(name='run-workflow')]))
        if os.environ.get('AGY_TEAMWORK_THIN') and os.environ['AGY_TEAMWORK_THIN']!='no-command':
            update(dict(sessionUpdate='available_commands_update',availableCommands=[dict(name='teamwork')]))
        if not os.environ.get('AGY_DYNAMIC_FIXTURE') and not os.environ.get('AGY_TEAMWORK_THIN') and os.environ.get('AGY_SWARM_THIN') != 'no-command':
            update(dict(sessionUpdate='available_commands_update',availableCommands=[dict(name='swarm')]))
    elif method == 'session/set_config_option':
        if closed:
            send(dict(id=msg['id'], error=dict(code=-32602, message='Session not found')))
            continue
        if msg['params']['configId'] == 'model':
            model = msg['params']['value']
        else:
            effort = msg['params']['value']
        result = metadata()
    elif method == 'session/prompt':
        prompted = True
        if os.environ.get('AGY_TEAMWORK_THIN'):
            body=msg['params']['prompt'][0]['text']
            captured=list(saved)
            if body.startswith('CALLCLI_DIALOGUE '):
                header,content=body.split('\n\n',1)
                update(dict(sessionUpdate='user_message_chunk',content=dict(type='text',text=body)))
                if os.environ['AGY_TEAMWORK_THIN']=='cancel-dialogue':
                    (root/'dialogue-active').touch()
                    pending=json.loads(sys.stdin.readline())
                    assert pending.get('method')=='session/cancel'
                    with (root/'requests.jsonl').open('a') as handle:
                        handle.write(json.dumps(pending)+'\n')
                    (root/'cancelled').touch()
                    saved=captured
                    captured=None
                    send(dict(id=msg['id'],result=dict(stopReason='cancelled')))
                    continue
                (root/'brief.txt').write_text(content)
                update(dict(sessionUpdate='tool_call',toolCallId='message'+str(len(saved)),
                    name='send_message',kind='other',status='completed'))
                text('CALLCLI_STAGE brief updated; native team still reviewing\n')
                if os.environ['AGY_TEAMWORK_THIN']=='unterminated-failure' and (root/'followup').exists():
                    if not (root/'followup-asked').exists():
                        text('CALLCLI_QUESTION Q2 Confirm failure report?')
                        (root/'followup-asked').touch()
                    else:
                        text('Final failure report: brief.md absent; write permission confirmation 60s timeout.\n')
            else:
                delegation=[dict(sessionUpdate='tool_call',toolCallId='delegate',name='invoke_subagent',
                    kind='other',status='completed'),dict(sessionUpdate='agent_message_chunk',
                    content=dict(type='text',text='Interview delegated\n'))]
                if os.environ['AGY_TEAMWORK_THIN']=='replay-only':
                    captured.extend(delegation)
                else:
                    for entry in delegation: update(entry)
            saved=captured
            captured=None
            send(dict(id=msg['id'],result=dict(stopReason='end_turn')))
            continue
        if os.environ.get('AGY_SWARM_THIN'):
            body=msg['params']['prompt'][0]['text']
            (root/'prompt').write_text(body)
            captured=list(saved)
            fault=os.environ['AGY_SWARM_THIN']
            if fault in {'board-working','status-working','status-new-id','board-only'} and not body.startswith('CALLCLI_DIALOGUE '):
                if fault!='board-only':
                    update(dict(sessionUpdate='tool_call',toolCallId='dispatch',name='swarm',kind='other',status='completed',rawInput=dict(action='add',name='worker-1')))
                    update(dict(sessionUpdate='tool_call',toolCallId='status',name='swarm',kind='other',status='completed',rawInput=dict(action='status'),rawOutput=dict(workers=[dict(name='worker-1',status='working')])))
                text('Work accepted\n' if fault=='status-working' else 'Run /swarm board\n')
                saved=captured
                captured=None
                send(dict(id=msg['id'],result=dict(stopReason='end_turn')))
                continue
            if fault=='replay-no-newline':
                saved=[dict(sessionUpdate='agent_message_chunk',content=dict(type='text',text='Native final replay'))]
                captured=None
                send(dict(id=msg['id'],result=dict(stopReason='end_turn')))
                continue
            if fault=='enrich':
                update(dict(sessionUpdate='tool_call',toolCallId='read',name='read_file',status='completed'))
                text('Native final initial\n')
                saved=captured
                saved[0]['kind']='read'
                captured=None
                send(dict(id=msg['id'],result=dict(stopReason='end_turn')))
                continue
            if fault=='late-old' and body.startswith('CALLCLI_DIALOGUE '):
                captured.extend([dict(sessionUpdate='agent_message_chunk',content=dict(type='text',text='Late old answer\n')),
                    dict(sessionUpdate='user_message_chunk',content=dict(type='text',text=body))])
                saved=captured
                captured=None
                send(dict(id=msg['id'],result=dict(stopReason='end_turn')))
                continue
            if fault=='async' and not body.startswith('CALLCLI_DIALOGUE '):
                update(dict(sessionUpdate='tool_call',toolCallId='pending',name='native_work',kind='other',status='in_progress'))
                text('CALLCLI_STAGE work in progress\n')
                saved=captured
                captured=None
                send(dict(id=msg['id'],result=dict(stopReason='end_turn')))
                continue
            if fault=='fatal':
                text('\u26a0 Agent execution terminated due to error.\n')
                saved=captured
                captured=None
                send(dict(id=msg['id'],result=dict(stopReason='end_turn')))
                continue
            if fault=='empty':
                saved=captured
                captured=None
                send(dict(id=msg['id'],result=dict(stopReason='end_turn')))
                continue
            if fault=='block':
                update(dict(sessionUpdate='tool_call',toolCallId='work',name='native_work',kind='other',status='in_progress'))
                (root/'active').touch()
                for pending_line in sys.stdin:
                    pending=json.loads(pending_line)
                    with (root/'requests.jsonl').open('a') as handle: handle.write(json.dumps(pending)+'\n')
                    if pending.get('method')=='session/cancel': break
                send(dict(id=msg['id'],result=dict(stopReason='cancelled')))
                captured=None
                continue
            if fault in {'question','late-old'} and not body.startswith('CALLCLI_DIALOGUE '):
                text('CALLCLI_STAGE draft ready\nCALLCLI_QUESTION Q1 Which label?\n')
            else:
                if body.startswith('CALLCLI_DIALOGUE '):
                    if fault in {'board-working','status-working'}:
                        update(dict(sessionUpdate='tool_call_update',toolCallId='status',name='swarm',kind='other',status='completed',rawInput=dict(action='status'),rawOutput=dict(workers=[dict(name='worker-1',status='completed')])))
                    if fault=='status-new-id':
                        update(dict(sessionUpdate='tool_call',toolCallId='status-later',name='swarm',kind='other',status='completed',rawInput=dict(action='status'),rawOutput=dict(workers=[dict(name='worker-1',status='completed')])))
                    update(dict(sessionUpdate='user_message_chunk',content=dict(type='text',text=body)))
                label='revised' if body.startswith('CALLCLI_DIALOGUE ') else 'initial'
                (root/'result.txt').write_text(label)
                text('Native final '+label+': documentation includes Run /swarm board\n' if fault=='final-board-mention' else 'Native final '+label+'\n')
            saved=captured
            captured=None
            send(dict(id=msg['id'],result=dict(stopReason='end_turn')))
            continue
        if os.environ.get('AGY_DYNAMIC_FIXTURE'):
            body = msg['params']['prompt'][0]['text']
            (root/'prompt').write_text(body)
            captured = list(saved)
            fault = os.environ['AGY_DYNAMIC_FIXTURE']
            if fault=='question' and not body.startswith('CALLCLI_DIALOGUE '):
                text('CALLCLI_QUESTION Q1 Which label?\n')
                saved = captured
                captured = None
                send(dict(id=msg['id'], result=dict(stopReason='end_turn')))
                continue
            text('CALLCLI_STAGE workflow pending\n')
            script = '' if fault=='bad-script' else 'print("synthetic")'
            if fault!='unapproved':
                send(dict(id='permit', method='session/request_permission', params=dict(sessionId='foreign' if fault=='foreign' else session,
                    toolCall=dict(toolCallId='workflow', title='run_workflow', kind='other', rawInput=dict(Script=script),
                        args=dict(Script='changed' if fault=='mismatched-script' else script)),
                    options=[dict(optionId='allow', kind='allow_once'), dict(optionId='deny', kind='reject_once')]+
                        ([dict(optionId='ambiguous',kind='allow_once')] if fault=='duplicate-option' else []))))
                permission = json.loads(sys.stdin.readline())
                if permission.get('result', {}).get('outcome', {}).get('optionId') != 'allow':
                    send(dict(id=msg['id'], result=dict(stopReason='end_turn')))
                    continue
            if fault=='disconnect':
                raise SystemExit(7)
            if fault=='block':
                update(dict(sessionUpdate='tool_call', toolCallId='workflow', name='run_workflow', kind='other', status='in_progress'))
                (root/'workflow-active').touch()
                for pending_line in sys.stdin:
                    pending = json.loads(pending_line)
                    with (root/'requests.jsonl').open('a') as f:
                        f.write(json.dumps(pending)+'\n')
                    if pending.get('method')=='session/cancel': break
                send(dict(id=msg['id'], result=dict(stopReason='cancelled')))
                continue
            update(dict(sessionUpdate='tool_call', toolCallId='workflow', name='run_workflow', kind='other',
                rawInput=dict(Script='changed' if fault=='changed-executed-script' else script),
                status='in_progress' if fault=='async' else 'failed' if fault=='failed' else 'completed'))
            if fault!='async':
                text('workflow failed\n' if fault=='failed' else '{"A":{"sum":5},"B":{"difference":3},"verified":true,"label":"revised"}\n')
            saved = captured
            captured = None
            send(dict(id=msg['id'], result=dict(stopReason='end_turn')))
            continue
        (root/'prompt').write_text(msg['params']['prompt'][0]['text'])
        if (root/'dialogue-block').exists() and msg['params']['prompt'][0]['text'].startswith('CALLCLI_DIALOGUE '):
            (root/'dialogue-blocked').touch()
            for pending_line in sys.stdin:
                if json.loads(pending_line).get('method')=='session/cancel':
                    (root/'cancelled').touch()
                    break
            send(dict(id=msg['id'],result=dict(stopReason='cancelled')))
            continue
        if os.environ.get('AGY_NATIVE_DEFERRED'):
            if not dialogue_history:
                dialogue_history = [dict(sessionUpdate='tool_call', toolCallId='add', name='swarm', status='completed',
                    args=dict(action='add', name='worker-a', model='gemini-4-hydrogen-medium'))]
            else:
                body = msg['params']['prompt'][0]['text']
                header, _ = json.JSONDecoder().raw_decode(body[len('CALLCLI_DIALOGUE '):])
                assert header['task'] == (root/'task-id').read_text() and header['turn'] == turn
                assert header['interaction_seq'] == deferred_seq+1
                deferred_seq = header['interaction_seq']
                if os.environ.get('AGY_NATIVE_DEFERRED_BOUNDARY') == 'tool':
                    dialogue_history.append(dict(sessionUpdate='tool_call', toolCallId='before-user'+str(deferred_seq), status='completed'))
                dialogue_history.append(dict(sessionUpdate='user_message_chunk', content=dict(type='text', text=body)))
                if header.get('question'):
                    q = header['question']
                    deferred_answers.append([q['interaction_seq'], q['label'], q['text']])
                    (root/'deferred-answers.json').write_text(json.dumps(deferred_answers))
                else:
                    entry = dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='CALLCLI_QUESTION Q1 SYNTHETIC_PRIVATE question?\n'))
                    dialogue_history.append(entry)
                    update(entry)
                dialogue_history.append(dict(sessionUpdate='tool_call', toolCallId='boundary'+str(deferred_seq), status='completed'))
                if len(deferred_answers) == (4 if os.environ.get('AGY_NATIVE_DEFERRED_MULTIPLE') else 2):
                    dialogue_history += [dict(sessionUpdate='tool_call', toolCallId='kill', name='swarm', status='completed',
                        args=dict(action='kill', name='worker-a')),
                        dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='CALLCLI_RESULT '+json.dumps(dict(
                            task=header['task'], turn=turn, interaction_seq=deferred_seq, status='completed',
                            result='deferred answers verified', workers=['worker-a']))+'\n'))]
            send(dict(id=msg['id'], result=dict(stopReason='end_turn')))
            continue
        if os.environ.get('AGY_NATIVE_DIALOGUE'):
            body = msg['params']['prompt'][0]['text']
            if not dialogue_history:
                dialogue_history = [dict(sessionUpdate='tool_call', toolCallId='add', name='swarm', status='completed',
                    args=dict(action='add', name='worker-a', model='gemini-4-hydrogen-medium')),
                    dict(sessionUpdate='agent_message_chunk', content=dict(type='text',
                        text='CALLCLI_QUESTION Q1 Format?\nCALLCLI_QUESTION Q2 Destination?\n'))]
                if os.environ.get('AGY_NATIVE_OLD_PENDING'):
                    dialogue_history += [dict(sessionUpdate='tool_call', toolCallId='boundary', status='completed'),
                        dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='CALLCLI_QUESTION Q3 Confirm revision?'))]
            else:
                try:
                    if not body.startswith('CALLCLI_DIALOGUE '):
                        raise ValueError('missing header')
                    header, _ = json.JSONDecoder().raw_decode(body[len('CALLCLI_DIALOGUE '):])
                    seq = header['interaction_seq']
                    if (header['task'] != (root/'task-id').read_text() or header['turn'] != turn
                            or type(seq) is not int or seq != len(dialogue_inputs)+1
                            or not isinstance(header['command_id'], str) or not header['command_id']
                            or header['command_id'] in dialogue_commands):
                        raise ValueError('identity mismatch')
                    question = header.get('question')
                    if header.get('question_id') is not None:
                        expected = {'Q1':'Q1 Format?', 'Q2':'Q2 Destination?'}
                        if (not isinstance(question, dict) or question['id'] != header['question_id']
                                or question['interaction_seq'] != 0 or question['text'] != expected.get(question['label'])
                                or any(q['label']==question['label'] for q in answer_map)):
                            raise ValueError('question mapping mismatch')
                        answer_map.append(question)
                    elif question is not None:
                        raise ValueError('unexpected question')
                    dialogue_commands.add(header['command_id'])
                    dialogue_inputs.append(body.split('\n\n', 1)[1])
                    (root/'answer-map.json').write_text(json.dumps(answer_map))
                except (ValueError, KeyError, TypeError):
                    (root/'invalid-dialogue').touch()
                    send(dict(id=msg['id'], error=dict(code=-32602, message='Invalid dialogue identity')))
                    continue
                (root/'dialogue-prompt-outstanding').touch()
                if os.environ.get('AGY_NATIVE_DIALOGUE_GATE') and seq == 1:
                    deadline = time.monotonic()+5
                    while not (root/'dialogue-continue').exists() and time.monotonic()<deadline:
                        time.sleep(.01)
                    if __import__('select').select([sys.stdin], [], [], 0)[0]:
                        (root/'concurrent-request').touch()
                if os.environ.get('AGY_NATIVE_DIALOGUE_EOF'):
                    raise SystemExit(7)
                if os.environ.get('AGY_NATIVE_DIALOGUE_QUESTION'):
                    question = dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='CALLCLI_QUESTION Q3 Confirm revision?'))
                    dialogue_history += [dict(sessionUpdate='user_message_chunk', content=dict(type='text', text=body)), question]
                    update(question)
                    send(dict(id=msg['id'], result=dict(stopReason='end_turn')))
                    continue
                dialogue_history += [dict(sessionUpdate='user_message_chunk', content=dict(type='text', text=body)),
                    dict(sessionUpdate='tool_call', toolCallId='dispatch'+str(seq), name='swarm', status='completed',
                        args=dict(action='post_message', name='worker-a')),
                    dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='CALLCLI_STAGE coordinator-reported ACK '+str(seq)+'\n'))]
                if seq == 2:
                    (root/'revision.txt').write_text(';'.join(dialogue_inputs))
                    dialogue_history += [dict(sessionUpdate='tool_call', toolCallId='kill', name='swarm', status='completed',
                        args=dict(action='kill', name='worker-a')),
                        dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='CALLCLI_RESULT '+json.dumps(dict(
                            task=header['task'], turn=header['turn'], interaction_seq=seq, status='completed',
                            result='revision adopted', workers=['worker-a']))+'\n'))]
            send(dict(id=msg['id'], result=dict(stopReason='end_turn')))
            continue
        if os.environ.get('AGY_NATIVE_TOOL_UPGRADE'):
            update(dict(sessionUpdate='tool_call', toolCallId='observed', name='read_board', kind='read', status='pending'))
        if not scenario and not os.environ.get('AGY_NATIVE_TAIL_GROWTH'):
            text('CALLCLI_STAGE workers launched\n')
        if os.environ.get('AGY_NATIVE_RECHUNK') == 'split':
            text('Hello world\n')
        elif os.environ.get('AGY_NATIVE_RECHUNK') == 'merge':
            text('Hello ')
            update(dict(sessionUpdate='tool_call', toolCallId='k', name='read_board', status='pending'))
            text('world\n')
        result = dict(stopReason='end_turn')
        if scenario:
            review_gate('before-prompt'+str(review_prompts))
            foregrounds = scenario.get('foregrounds', [scenario['foreground']])
            for entry in foregrounds[min(review_prompts, len(foregrounds)-1)]:
                update(entry)
            gate = 'prompt'+str(review_prompts)
            review_prompts += 1
            if review_gate(gate):
                result = dict(stopReason='cancelled')
        if os.environ.get('AGY_NATIVE_EXIT') == 'foreground':
            raise SystemExit(7)
        if os.environ.get('AGY_NATIVE_FATAL') == 'foreground':
            text('\n\u26a0 Agent execution terminated due to error.\n')
        if os.environ.get('AGY_NATIVE_BLOCK'):
            prompt_id = msg['id']
            for pending_line in sys.stdin:
                pending = json.loads(pending_line)
                if pending.get('method') == 'session/cancel':
                    (root/'cancelled').touch()
                    break
            send(dict(id=prompt_id, result=dict(stopReason='cancelled')))
            continue
    elif method == 'session/load':
        if closed:
            send(dict(id=msg['id'], error=dict(code=-32602, message='Session not found')))
            continue
        captured = []
        if os.environ.get('AGY_TEAMWORK_THIN'):
            if prompted and (root/'interview').exists() and not (root/'asked').exists():
                saved.extend([dict(sessionUpdate='tool_call',toolCallId='interview',name='ask_question',
                    kind='other',status='completed'),dict(sessionUpdate='agent_message_chunk',
                    content=dict(type='text',text='CALLCLI_QUESTION Q1 Which audience and three checks?' if os.environ['AGY_TEAMWORK_THIN']=='unterminated-failure' else 'CALLCLI_QUESTION Q1 Which label for the brief?\n'))])
                (root/'asked').touch()
            if prompted and (root/'finish').exists() and not (root/'result.txt').exists():
                (root/'result.txt').write_text('review of revised brief')
                saved.append(dict(sessionUpdate='agent_message_chunk',content=dict(type='text',
                    # A later response must not concatenate onto the Q2 line.
                    text='\n- **任务状态**：失败并按指令自然停止（受原生写入权限确认超时阻塞，未扩权、未反复派工、未伪造产物）。\n- **阻塞原因**：Teamwork root（`[SESSION]`）回报称其调用原生 `write_file` 创建 [brief.md](file://[SCRATCH]/work/brief.md) 时，因等待终端控制台权限确认超时（60s timeout）被系统安全机制取消。\n' if os.environ['AGY_TEAMWORK_THIN']=='unterminated-failure' else 'Final review: revised brief inspected; invoke_subagent delegation is documented.\n')))
            for entry in saved: update(entry)
            send(dict(id=msg['id'],result=metadata()))
            captured=None
            continue
        if os.environ.get('AGY_SWARM_THIN'):
            if os.environ['AGY_SWARM_THIN']=='async' and (root/'finish').exists():
                for entry in saved:
                    if entry.get('toolCallId')=='pending': entry['status']='completed'
                if not (root/'result.txt').exists():
                    (root/'result.txt').write_text('initial')
                    saved.append(dict(sessionUpdate='agent_message_chunk',content=dict(type='text',text='Native final initial\n')))
            for entry in saved: update(entry)
            send(dict(id=msg['id'],result=metadata()))
            captured=None
            continue
        if os.environ.get('AGY_DYNAMIC_FIXTURE'):
            if os.environ['AGY_DYNAMIC_FIXTURE']=='async' and (root/'workflow-release').exists():
                for entry in saved:
                    if entry.get('toolCallId')=='workflow': entry['status']='completed'
                if not any(entry.get('content',{}).get('text','').startswith('{"A"') for entry in saved):
                    saved.append(dict(sessionUpdate='agent_message_chunk',content=dict(type='text',text='{"A":{"sum":5},"B":{"difference":3},"verified":true}\n')))
            for entry in saved:
                if os.environ['AGY_DYNAMIC_FIXTURE']=='renamed-workflow' and entry.get('toolCallId')=='workflow':
                    entry = dict(entry,name='read_board')
                update(entry)
            send(dict(id=msg['id'], result=metadata()))
            captured = None
            continue
        if scenario and (root/'cancelled').exists() and 'cancel_replay' in scenario:
            for entry in scenario['cancel_replay']:
                update(entry)
            if scenario.get('cancel_error'):
                send(dict(id=msg['id'],error=dict(code=-32603,message='Synthetic incomplete cancellation replay')))
                captured=None
                continue
            captured = None
            send(dict(id=msg['id'], result=metadata()))
            continue
        if os.environ.get('AGY_NATIVE_DEFERRED'):
            if (root/'disconnect-deferred').exists():
                raise SystemExit(7)
            if dialogue_history and 0 not in deferred_slots:
                deferred_slots[0] = len(dialogue_history)
                dialogue_history.append(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='CALLCLI_QUESTION Q1 SYNTHETIC_PRIVATE')))
            if (os.environ.get('AGY_NATIVE_DEFERRED_MULTIPLE') and deferred_seq==1 and 1 not in deferred_slots):
                deferred_slots[1] = len(dialogue_history)
                dialogue_history.append(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='CALLCLI_QUESTION Q2 SYNTHETIC_PRIVATE')))
            if (root/'recover-1').exists() and 1 in deferred_slots:
                dialogue_history[deferred_slots[1]]['content']['text'] = 'CALLCLI_QUESTION Q2 SYNTHETIC_PRIVATE question?'+('' if os.environ.get('AGY_NATIVE_DEFERRED_BOUNDARY') else '\n')
            if (root/'recover-0').exists() and deferred_slots:
                dialogue_history[deferred_slots[0]]['content']['text'] = 'CALLCLI_QUESTION Q1 SYNTHETIC_PRIVATE question?'+('' if os.environ.get('AGY_NATIVE_DEFERRED_BOUNDARY') else '\n')
            if (root/'ambiguous-0').exists() and deferred_slots:
                dialogue_history[deferred_slots[0]]['content']['text'] = 'rewritten SYNTHETIC_PRIVATE'
            if (root/'invalid-replay').exists() and deferred_slots:
                dialogue_history[deferred_slots[0]]['content']['text'] = 'CALLCLI_QUESTION Q1 SYNTHETIC_PRIVATE question?\n'
                dialogue_history[0]['args']['name'] = 'rewritten-worker'
            for entry in dialogue_history:
                update(entry)
            send(dict(id=msg['id'], result=metadata()))
            captured = None
            continue
        if os.environ.get('AGY_NATIVE_DIALOGUE'):
            for entry in dialogue_history:
                update(entry)
            if (dialogue_history and (root/'dialogue-first-load').exists()
                    and not (root/'dialogue-load-returned').exists() and os.environ.get('AGY_NATIVE_DIALOGUE_GATE')):
                (root/'dialogue-load-outstanding').touch()
                deadline = time.monotonic()+5
                while not (root/'dialogue-load-continue').exists() and time.monotonic()<deadline:
                    time.sleep(.01)
                if __import__('select').select([sys.stdin], [], [], 0)[0]:
                    (root/'concurrent-request').touch()
                (root/'dialogue-load-returned').touch()
            if dialogue_history:
                (root/'dialogue-first-load').touch()
            send(dict(id=msg['id'], result=metadata()))
            captured = None
            continue
        for prior in saved:
            update(prior)
        if scenario and prompted:
            if replay_index >= len(scenario['replays']):
                raise SystemExit(7)
            replay = scenario['replays'][replay_index]
            review_gate('before-load'+str(review_loads))
            for entry in replay.get('updates', []) if isinstance(replay, dict) else replay:
                update(entry)
            gate = 'load'+str(review_loads)
            review_loads += 1
            review_gate(gate)
            replay_index += 1
            if isinstance(replay, dict) and replay.get('fail'):
                send(dict(id=msg['id'], error=dict(code=-32603, message='Synthetic incomplete replay')))
                captured = None
                continue
            captured = None
            send(dict(id=msg['id'], result=metadata()))
            continue
        if os.environ.get('AGY_NATIVE_TAIL_GROWTH'):
            tail_loads += int(prompted)
            if tail_loads > 2:
                raise SystemExit(7)
            for action in ('add', 'kill'):
                update(dict(sessionUpdate='tool_call', toolCallId=action, name='swarm',
                    status='completed' if prompted else 'pending',
                    args=dict(action=action, name='worker-a', model='gemini-4-hydrogen-medium')))
            task_id = (root/'task-id').read_text()
            old = 'CALLCLI_RESULT '+json.dumps(dict(task=task_id, turn=0, status='completed', result='old result', workers=[]))+'\n'
            extra = 'CALLCLI_STAGE tail stage\nCALLCLI_RESULT '+json.dumps(dict(task=task_id, turn=turn,
                status='completed', result='tail result', workers=['worker-a']))+'\n'
            text(old+(extra if prompted else ''))
            send(dict(id=msg['id'], result=metadata()))
            captured = None
            continue
        if not prompted:
            send(dict(id=msg['id'], result=metadata()))
            continue
        if os.environ.get('AGY_NATIVE_EXIT') == 'background':
            raise SystemExit(7)
        update(dict(sessionUpdate='user_message_chunk', content=dict(type='text', text='owned task')))
        if os.environ.get('AGY_NATIVE_TOOL_UPGRADE'):
            update(dict(sessionUpdate='tool_call', toolCallId='observed', name='read_board', kind='read',
                        status=os.environ['AGY_NATIVE_TOOL_UPGRADE']))
        text('CALLCLI_STAGE workers launched\n')
        if os.environ.get('AGY_NATIVE_RECHUNK') == 'split':
            text('Hello ')
            update(dict(sessionUpdate='tool_call', toolCallId='k', name='read_board', status='pending'))
            text('world\n')
        elif os.environ.get('AGY_NATIVE_RECHUNK') == 'merge':
            text('Hello world\n')
            update(dict(sessionUpdate='tool_call', toolCallId='k', name='read_board', status='pending'))
        update(dict(sessionUpdate='tool_call', toolCallId='add'+str(turn), name='swarm', status='completed',
            args=dict(action='add', name='worker-a', model='gemini-4-hydrogen-medium'), rawOutput=dict(status='added', name='worker-a')))
        if (root/'final').exists():
            if os.environ.get('AGY_NATIVE_RECHUNK'):
                update(dict(sessionUpdate='tool_call_update',toolCallId='k',status='completed'))
            if not os.environ.get('AGY_NATIVE_MISSING_KILL'):
                update(dict(sessionUpdate='tool_call', toolCallId='kill'+str(turn), name='swarm', status='completed',
                    args=dict(action='kill', name='worker-a'), rawOutput=dict(status='killed', name='worker-a')))
            task_id = (root/'task-id').read_text()
            text('same legitimate answer\n')
            update(dict(sessionUpdate='tool_call', toolCallId='read'+str(turn), name='read_board', status='completed'))
            envelope = dict(task=task_id, status='completed', result='two verified answers', workers=['worker-a'])
            if not os.environ.get('AGY_NATIVE_OMIT_TURN'):
                envelope['turn'] = turn
            text('same legitimate answer\nCALLCLI_RESULT '+json.dumps(envelope)+'\n')
            if os.environ.get('AGY_NATIVE_FATAL') == 'background':
                text('\n\u26a0 Agent execution terminated due to error.\n')
        (root/'history.json').write_text(json.dumps(captured))
        captured = None
        (root/'loaded').touch()
        result = metadata()
    elif method == 'session/cancel':
        (root/'cancelled').touch()
        continue
    elif method == 'session/close':
        if os.environ.get('AGY_DYNAMIC_FIXTURE')=='close-fail':
            send(dict(id=msg['id'], error=dict(code=-32603, message='synthetic close failure')))
            continue
        closed = True
        (root/'closed-session').touch()
        result = {}
    else:
        send(dict(id=msg.get('id'), error=dict(code=-32601, message='Unsupported')))
        continue
    if 'id' in msg:
        send(dict(id=msg['id'], result=result))
