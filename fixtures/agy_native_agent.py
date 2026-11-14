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
model = 'gemini-4-carbon'
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

def send(value):
    print(json.dumps(dict(jsonrpc='2.0', **value)), flush=True)

def metadata():
    return dict(sessionId=session, models=dict(currentModelId=model+'-'+effort,
        availableModels=[dict(modelId=x+'-'+level) for x in ('gemini-4-carbon', 'gemini-4-hydrogen', 'gemini-4-argon') for level in ('low', 'medium')]),
        configOptions=[dict(id='model', currentValue=model,
        options=[dict(value=x, name=x) for x in ('gemini-4-carbon', 'gemini-4-hydrogen', 'gemini-4-argon')]),
        dict(id='thought_level', currentValue=effort, options=[dict(value=x) for x in ('low', 'medium')])])

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
        if msg['params']['prompt'][0]['text'].startswith('CALLCLI_CANCEL_CLEANUP '):
            if scenario and 'cleanup_replay' in scenario:
                scenario['cancel_replay']=scenario['cleanup_replay']
            send(dict(id=msg['id'],result=dict(stopReason='end_turn')))
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
            for entry in scenario['foreground']:
                update(entry)
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
            for entry in replay.get('updates', []) if isinstance(replay, dict) else replay:
                update(entry)
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
        closed = True
        (root/'closed-session').touch()
        result = {}
    else:
        send(dict(id=msg.get('id'), error=dict(code=-32601, message='Unsupported')))
        continue
    if 'id' in msg:
        send(dict(id=msg['id'], result=result))
