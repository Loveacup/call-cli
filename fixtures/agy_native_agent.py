#!/usr/bin/env python3
"""Owned synthetic ACP endpoint for native-owner lifecycle tests."""
import json
import os
from pathlib import Path
import sys

root = Path(os.environ['AGY_NATIVE_FIXTURE'])
session = '11111111-1111-4111-8111-111111111111'
model = 'gemini-4-carbon'
closed = False
effort = 'medium'
prompted = False
turn = int(os.environ.get('AGY_NATIVE_TURN', '1'))
saved = json.loads((root/'history.json').read_text()) if (root/'history.json').exists() else []
captured = None

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
        (root/'prompt').write_text(msg['params']['prompt'][0]['text'])
        text('CALLCLI_STAGE workers launched\n')
        result = dict(stopReason='end_turn')
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
        captured = []
        for prior in saved:
            update(prior)
        if not prompted:
            send(dict(id=msg['id'], result=metadata()))
            continue
        if os.environ.get('AGY_NATIVE_EXIT') == 'background':
            raise SystemExit(7)
        update(dict(sessionUpdate='user_message_chunk', content=dict(type='text', text='owned task')))
        text('CALLCLI_STAGE workers launched\n')
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
        result = {}
    else:
        send(dict(id=msg.get('id'), error=dict(code=-32601, message='Unsupported')))
        continue
    if 'id' in msg:
        send(dict(id=msg['id'], result=result))
