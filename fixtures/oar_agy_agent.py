#!/usr/bin/env python3
"""SYNTHETIC ACP TEST DOUBLE — NOT LIVE AGY MODEL.

Minimal local ACP stdio test endpoint used ONLY by offline Slice D tests to
exercise the real @botiverse/oar/runtimes/antigravity adapter without hitting live
Google accounts or paid model endpoints.
"""
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid


if '--version' in sys.argv[1:]:
    print('1.5.12')
    raise SystemExit(0)

STATE_PATH = Path(os.environ['OAR_FIXTURE_STATE_FILE']) if os.environ.get('OAR_FIXTURE_STATE_FILE') else None
SEND_LOCK = threading.Lock()
STATE_LOCK = threading.Lock()


def send(payload: dict):
    with SEND_LOCK:
        print(json.dumps({'jsonrpc': '2.0', **payload}), flush=True)


def emit_update(session_id: str, update: dict):
    send({'method': 'session/update', 'params': {'sessionId': session_id, 'update': update}})


def load_state() -> dict:
    if STATE_PATH and STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {'sessions': {}, 'closed': [], 'requests': []}


def save_state(state: dict):
    if STATE_PATH:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2))


def record_request(method: str, params: dict):
    if not STATE_PATH:
        return
    with STATE_LOCK:
        st = load_state()
        st.setdefault('requests', []).append({'method': method, 'params': params})
        save_state(st)


def build_config_options(model: str, effort: str) -> list[dict]:
    return [
        {
            'id': 'model',
            'name': 'Model',
            'category': 'model',
            'type': 'select',
            'currentValue': model,
            'options': [
                {'value': 'gemini-pro-orchestrator', 'name': 'Orchestrator'},
                {'value': 'gemini-pro-orchestrator-argon', 'name': 'Orchestrator Argon'},
                {'value': 'gemini-4-carbon', 'name': 'Gemini 4 Carbon'},
                {'value': 'gemini-4-argon', 'name': 'Gemini 4 Argon'},
                {'value': 'gemini-4-hydrogen', 'name': 'Gemini 4 Hydrogen'},
            ],
        },
        {
            'id': 'thought_level',
            'name': 'Thought Level',
            'category': 'thought_level',
            'type': 'select',
            'currentValue': effort,
            'options': [
                {'value': 'low', 'name': 'Low'},
                {'value': 'medium', 'name': 'Medium'},
                {'value': 'high', 'name': 'High'},
            ],
        },
    ]


sessions_mem: dict[str, dict] = {}
active_prompts: dict[str, dict] = {}
post_turn_waiters: dict[str, tuple[threading.Event, threading.Thread]] = {}


def extract_prompt_text(params: dict) -> str:
    parts = params.get('prompt') or []
    texts = [p.get('text', '') for p in parts if isinstance(p, dict) and p.get('type') == 'text']
    return '\n'.join(texts)


def run_prompt_worker(rpc_id, params: dict):
    sid = params.get('sessionId')
    text = extract_prompt_text(params)
    sess = sessions_mem.get(sid, {'model': 'gemini-pro-orchestrator', 'effort': 'medium', 'append': ''})
    append_present = bool(sess.get('append'))
    cancel_ev = threading.Event()
    steer_ev = threading.Event()

    with STATE_LOCK:
        prior = active_prompts.get(sid)
        if prior is not None and not prior['settled'].is_set():
            # Second prompt arrives mid-turn: simulate AGY steerSupersedesPrompt=true!
            prior['steered_text'] = text
            prior['steer_ev'].set()
            # Keep track of this newer prompt under active_prompts[sid].
        state_entry = {
            'rpc_id': rpc_id,
            'text': text,
            'cancel_ev': cancel_ev,
            'steer_ev': steer_ev,
            'settled': threading.Event(),
            'steered_text': None,
        }
        active_prompts[sid] = state_entry

    # If this IS the second (steer) prompt superseding an earlier active STEER_WAIT prompt:
    if prior is not None and not prior['settled'].is_set():
        emit_update(sid, {'sessionUpdate': 'user_message_chunk', 'content': {'type': 'text', 'text': text}})
        time.sleep(0.05)
        if cancel_ev.is_set():
            state_entry['settled'].set()
            send({'id': rpc_id, 'result': {'stopReason': 'cancelled'}})
            return
        emit_update(sid, {
            'sessionUpdate': 'agent_message_chunk',
            'content': {'type': 'text', 'text': f'CALLCLI_STAGE steer_applied\nSteered answer: {text.strip()}\n'},
        })
        state_entry['settled'].set()
        send({'id': rpc_id, 'result': {'stopReason': 'end_turn'}})
        return

    if 'CRASH_MID_TURN' in text:
        emit_update(sid, {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'CALLCLI_STAGE starting_before_crash\n'}})
        time.sleep(0.03)
        os._exit(2)

    if 'STEER_WAIT' in text:
        emit_update(sid, {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'CALLCLI_STAGE waiting_for_steer\n'}})
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if cancel_ev.is_set():
                state_entry['settled'].set()
                send({'id': rpc_id, 'result': {'stopReason': 'cancelled'}})
                return
            if steer_ev.is_set():
                # Prompt #1 settles EARLY with cancelled when prompt #2 supersedes it.
                state_entry['settled'].set()
                send({'id': rpc_id, 'result': {'stopReason': 'cancelled'}})
                return
            time.sleep(0.01)
        state_entry['settled'].set()
        send({'id': rpc_id, 'result': {'stopReason': 'end_turn'}})
        return

    if 'CANCEL_WAIT' in text:
        emit_update(sid, {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'CALLCLI_STAGE waiting_for_cancel\n'}})
        cancel_ev.wait(timeout=5.0)
        state_entry['settled'].set()
        send({'id': rpc_id, 'result': {'stopReason': 'cancelled'}})
        return

    if 'OUTSIDE_TURN_FAIL' in text:
        emit_update(sid, {'sessionUpdate': 'tool_call', 'toolCallId': 't-bg', 'title': 'bash', 'kind': 'execute', 'status': 'in_progress'})
        emit_update(sid, {'sessionUpdate': 'tool_call_update', 'toolCallId': 't-bg', 'status': 'failed'})
        state_entry['settled'].set()
        wake_ev = threading.Event()
        waiter_entry = (wake_ev, threading.current_thread())
        with STATE_LOCK:
            post_turn_waiters[sid] = waiter_entry
        try:
            send({'id': rpc_id, 'result': {'stopReason': 'end_turn'}})
            wake_ev.wait(timeout=0.03)
            emit_update(sid, {'sessionUpdate': 'tool_call_update', 'toolCallId': 't-bg', 'status': 'completed'})
            emit_update(sid, {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'late background output\n'}})
        finally:
            with STATE_LOCK:
                if post_turn_waiters.get(sid) == waiter_entry:
                    post_turn_waiters.pop(sid, None)
        return

    if 'OUTSIDE_TURN' in text:
        emit_update(sid, {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'Primary in-turn answer\n'}})
        state_entry['settled'].set()
        wake_ev = threading.Event()
        waiter_entry = (wake_ev, threading.current_thread())
        with STATE_LOCK:
            post_turn_waiters[sid] = waiter_entry
        try:
            send({'id': rpc_id, 'result': {'stopReason': 'end_turn'}})
            wake_ev.wait(timeout=0.03)
            emit_update(sid, {'sessionUpdate': 'tool_call', 'toolCallId': 't-late', 'title': 'background_job', 'kind': 'execute', 'status': 'in_progress'})
            emit_update(sid, {'sessionUpdate': 'tool_call_update', 'toolCallId': 't-late', 'status': 'completed'})
            emit_update(sid, {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'late background output\n'}})
        finally:
            with STATE_LOCK:
                if post_turn_waiters.get(sid) == waiter_entry:
                    post_turn_waiters.pop(sid, None)
        return

    if 'ASK_QUESTION' in text:
        emit_update(sid, {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'CALLCLI_QUESTION Q1 Confirm next step?\n'}})
        state_entry['settled'].set()
        send({'id': rpc_id, 'result': {'stopReason': 'end_turn'}})
        return

    emit_update(sid, {'sessionUpdate': 'tool_call', 'toolCallId': 't-1', 'title': 'read_file', 'kind': 'read', 'status': 'in_progress'})
    emit_update(sid, {'sessionUpdate': 'tool_call_update', 'toolCallId': 't-1', 'status': 'completed'})
    emit_update(sid, {
        'sessionUpdate': 'agent_message_chunk',
        'content': {
            'type': 'text',
            'text': f"CALLCLI_STAGE running_fixture\nAnswer model={sess['model']} effort={sess['effort']} append={append_present} open={sess.get('open_method')}\n",
        },
    })
    state_entry['settled'].set()
    send({'id': rpc_id, 'result': {'stopReason': 'end_turn'}})


for raw in sys.stdin:
    line = raw.strip()
    if not line:
        continue
    msg = json.loads(line)
    method = msg.get('method')
    params = msg.get('params') or {}
    rpc_id = msg.get('id')
    record_request(method or 'response', params)

    if method == 'initialize':
        send({
            'id': rpc_id,
            'result': {
                'protocolVersion': 1,
                'agentCapabilities': {
                    'loadSession': True,
                    'sessionCapabilities': {'list': {}, 'resume': {}, 'close': {}},
                    '_meta': {'antigravity': {'systemPrompt': {'append': True}}},
                },
            },
        })
    elif method == 'session/new':
        sid = str(uuid.uuid4())
        append = ((params.get('_meta') or {}).get('antigravity') or {}).get('systemPrompt', {}).get('append', '')
        sess = {
            'sessionId': sid,
            'cwd': params.get('cwd', ''),
            'model': 'gemini-pro-orchestrator',
            'effort': 'medium',
            'append': append,
            'open_method': 'session/new',
        }
        sessions_mem[sid] = sess
        with STATE_LOCK:
            st = load_state()
            st.setdefault('sessions', {})[sid] = dict(sess)
            save_state(st)
        send({'id': rpc_id, 'result': {'sessionId': sid, 'configOptions': build_config_options(sess['model'], sess['effort'])}})
    elif method == 'session/list':
        with STATE_LOCK:
            st = load_state()
            all_sessions = list((st.get('sessions') or {}).values())
        for sid, s in sessions_mem.items():
            if all(item.get('sessionId') != sid for item in all_sessions):
                all_sessions.append(s)
        send({'id': rpc_id, 'result': {'sessions': [{'sessionId': s['sessionId'], 'cwd': s['cwd']} for s in all_sessions]}})
    elif method == 'session/resume':
        sid = params.get('sessionId')
        with STATE_LOCK:
            st = load_state()
            saved_sess = (st.get('sessions') or {}).get(sid) or sessions_mem.get(sid)
        if not saved_sess:
            send({'id': rpc_id, 'error': {'code': -32602, 'message': 'Session not found'}})
            continue
        # Deliberately reset model/effort on resume (like real agy CLI) so OAR must re-apply them!
        append = ((params.get('_meta') or {}).get('antigravity') or {}).get('systemPrompt', {}).get('append', '')
        sess = {
            'sessionId': sid,
            'cwd': saved_sess['cwd'],
            'model': 'gemini-pro-orchestrator',
            'effort': 'medium',
            'append': append,
            'open_method': 'session/resume',
        }
        sessions_mem[sid] = sess
        with STATE_LOCK:
            st = load_state()
            st.setdefault('sessions', {})[sid] = dict(sess)
            save_state(st)
        send({'id': rpc_id, 'result': {'sessionId': sid, 'configOptions': build_config_options(sess['model'], sess['effort'])}})
    elif method == 'session/set_config_option':
        sid = params.get('sessionId')
        cid = params.get('configId')
        val = params.get('value')
        sess = sessions_mem.get(sid)
        if not sess:
            send({'id': rpc_id, 'error': {'code': -32602, 'message': 'Session not found'}})
            continue
        if cid == 'model':
            sess['model'] = val
        elif cid == 'thought_level':
            sess['effort'] = val
        with STATE_LOCK:
            st = load_state()
            st.setdefault('sessions', {})[sid] = dict(sess)
            save_state(st)
        opts = build_config_options(sess['model'], sess['effort'])
        emit_update(sid, {'sessionUpdate': 'config_option_update', 'configOptions': opts})
        send({'id': rpc_id, 'result': {'configOptions': opts}})
    elif method == 'session/prompt':
        threading.Thread(target=run_prompt_worker, args=(rpc_id, params), daemon=True).start()
    elif method == 'session/cancel':
        sid = params.get('sessionId')
        entry = active_prompts.get(sid)
        if entry is not None:
            entry['cancel_ev'].set()
    elif method == 'session/close':
        sid = params.get('sessionId')
        with STATE_LOCK:
            pending = post_turn_waiters.pop(sid, None)
        if pending is not None:
            wake_ev, worker = pending
            wake_ev.set()
            worker.join(timeout=1.0)
        with STATE_LOCK:
            st = load_state()
            st.setdefault('closed', []).append(sid)
            save_state(st)
        if rpc_id is not None:
            send({'id': rpc_id, 'result': {}})
    elif rpc_id is not None:
        send({'id': rpc_id, 'error': {'code': -32601, 'message': f'Method not found: {method}'}})
