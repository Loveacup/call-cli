"""OAR-backed local AGY execution adapter for Call-CLI.

Drives real @botiverse/oar modules via the thin Node bridge (oar-bridge.mjs)
under Call-CLI's existing claimed-turn, writer-lock, event and single-owner
cleanup lifecycle. Fails closed without falling back to ACPX or agy_native.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time

try:
    from .agy import outcome_note, prompt_guidance
    from .events import EventDecoder
    from .skills import guidance, stage
    from .transport import protocol_prompt
except ImportError:
    from agy import outcome_note, prompt_guidance
    from events import EventDecoder
    from skills import guidance, stage
    from transport import protocol_prompt

OPEN_TIMEOUT_SECONDS = 30.0
DISPOSE_TIMEOUT_SECONDS = 5.0
POLL_INTERVAL_SECONDS = 0.05


def _valid_oar_dist(raw: str) -> Path | None:
    text = raw.strip()
    if not text:
        return None
    base = Path(text).expanduser()
    if not base.exists():
        return None
    dist = base / 'dist' if (base / 'dist' / 'observe' / 'index.js').is_file() else base
    if not (dist / 'observe' / 'index.js').is_file():
        return None
    if not (dist / 'runtimes' / 'antigravity' / 'index.js').is_file():
        return None
    return base


def _feed_decoder(decoder: EventDecoder, ev: dict, tool_names: dict[str, str]) -> list[tuple[str, str]]:
    kind = ev.get('kind')
    if kind == 'text_delta':
        update = {
            'sessionUpdate': 'agent_message_chunk',
            'content': {'type': 'text', 'text': ev.get('text', '')},
        }
        return decoder.feed({'params': {'update': update}})
    if kind == 'tool_call_started':
        cid = str(ev.get('callId', ''))
        title = str(ev.get('tool') or ev.get('toolName') or '')
        if cid and title:
            tool_names[cid] = title
        update = {
            'sessionUpdate': 'tool_call_update',
            'toolCallId': cid,
            'status': 'in_progress',
            'title': title,
            'kind': 'other',
        }
        return decoder.feed({'params': {'update': update}})
    if kind == 'tool_call_ended':
        cid = str(ev.get('callId', ''))
        result = ev.get('result')
        status = 'failed' if result == 'failed' else 'completed'
        update = {
            'sessionUpdate': 'tool_call_update',
            'toolCallId': cid,
            'status': status,
            'title': tool_names.get(cid, ''),
            'kind': 'other',
        }
        return decoder.feed({'params': {'update': update}})
    return []


def _format_outside_turn(ev: dict) -> str:
    kind = str(ev.get('kind') or 'activity')
    if kind == 'text_delta':
        preview = str(ev.get('text', '')).strip().replace('\n', ' ')[:160]
        return f'text_delta: {preview}' if preview else 'text_delta'
    detail = ev.get('tool') or ev.get('taskId') or ev.get('status') or ev.get('result') or ''
    return f'{kind} {detail}'.strip()[:300]


class OarBackend:
    """Isolated local AGY execution via real OAR modules; no ACPX fallback."""

    def control(self, task, action, timeout=20):
        raise ValueError('OAR session control belongs to the claimed single owner; no ACPX control permitted')

    def run(self, store, task, emit):
        try:
            from .backends import TurnResult
        except ImportError:
            from backends import TurnResult

        task_id = task['id']
        turn = task['turn']
        launch_id = task.get('launch_id')
        epoch = task.get('control_epoch', 1)

        if task.get('device') != 'local' or task.get('agent') != 'agy' or task.get('backend') != 'oar':
            return TurnResult('unknown', None, None, note='OAR backend requires local AGY task; outcome unknown, no fallback.')

        oar_dist = _valid_oar_dist(os.environ.get('CALL_CLI_OAR_DIST', ''))
        if oar_dist is None:
            return TurnResult(
                'unknown', None, None,
                note='OAR runtime directory unavailable (CALL_CLI_OAR_DIST missing or incomplete); outcome unknown, no fallback to ACPX or agy_native.',
            )

        node_bin = shutil.which('node')
        if not node_bin:
            return TurnResult('unknown', None, None, note='Node executable unavailable for OAR bridge; outcome unknown, no fallback.')

        prev_session = task.get('oar_session') or {}
        resume_sid = prev_session.get('session_id')
        if resume_sid and prev_session.get('cwd') != task['cwd']:
            return TurnResult('unknown', None, None, note='OAR resume cwd mismatch; outcome unknown, no automatic replay.')

        if store.get(task_id)['state'] == 'cancelling':
            return TurnResult('cancelled', 0, 'cancelled', note='Cancelled before OAR launch')

        cleanup_token = store.oar_cleanup_owner(task_id, turn, launch_id, epoch)
        task = stage(store, task, emit)
        store.update(task_id, oar_turn_started=True, session_creation_attempted=True)

        bridge_path = Path(__file__).with_name('oar-bridge.mjs')
        env = dict(os.environ, CALL_CLI_OAR_DIST=str(oar_dist))
        proc = subprocess.Popen(
            [node_bin, str(bridge_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=task['cwd'],
            env=env,
            text=True,
            bufsize=1,
        )

        msg_queue: queue.Queue = queue.Queue()
        stderr_tail: list[str] = []

        def read_stdout():
            try:
                assert proc.stdout is not None
                for raw in proc.stdout:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        msg_queue.put(json.loads(line))
                    except json.JSONDecodeError:
                        msg_queue.put({'type': 'raw_stdout', 'text': line[:500]})
            except (OSError, ValueError):
                pass
            finally:
                msg_queue.put(None)

        def read_stderr():
            try:
                assert proc.stderr is not None
                for raw in proc.stderr:
                    text = raw.strip()
                    if text:
                        stderr_tail.append(text[:300])
                        if len(stderr_tail) > 8:
                            stderr_tail.pop(0)
            except (OSError, ValueError):
                pass

        stdout_thread = threading.Thread(target=read_stdout, daemon=True, name='oar-bridge-stdout')
        stderr_thread = threading.Thread(target=read_stderr, daemon=True, name='oar-bridge-stderr')
        stdout_thread.start()
        stderr_thread.start()

        def send(payload: dict) -> bool:
            try:
                if proc.stdin is None or proc.poll() is not None:
                    return False
                proc.stdin.write(json.dumps(payload) + '\n')
                proc.stdin.flush()
                return True
            except OSError:
                return False

        terminated_result: tuple[dict | None, int | None] | None = None

        def close_pipes():
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass

        def terminate_bridge(on_item=None) -> tuple[dict | None, int | None]:
            nonlocal terminated_result
            if terminated_result is not None:
                return terminated_result
            disposed_msg = None
            rc = None
            try:
                if proc.poll() is None:
                    send({'op': 'dispose'})
                    deadline = time.monotonic() + DISPOSE_TIMEOUT_SECONDS
                    while time.monotonic() < deadline:
                        remaining = max(0.01, deadline - time.monotonic())
                        try:
                            item = msg_queue.get(timeout=min(POLL_INTERVAL_SECONDS, remaining))
                        except queue.Empty:
                            if proc.poll() is not None:
                                break
                            continue
                        if item is None:
                            break
                        if item.get('type') == 'disposed':
                            disposed_msg = item
                            break
                        if on_item is not None:
                            on_item(item)
                if proc.stdin is not None:
                    try:
                        proc.stdin.close()
                    except OSError:
                        pass
                try:
                    rc = proc.wait(timeout=DISPOSE_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:
                        rc = proc.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        rc = proc.wait(timeout=2.0)
                stdout_thread.join(timeout=2.0)
                stderr_thread.join(timeout=2.0)
                while True:
                    try:
                        tail = msg_queue.get_nowait()
                    except queue.Empty:
                        break
                    if tail is None:
                        continue
                    if tail.get('type') == 'disposed':
                        if disposed_msg is None:
                            disposed_msg = tail
                        continue
                    if on_item is not None:
                        on_item(tail)
            finally:
                close_pipes()
            terminated_result = (disposed_msg, rc)
            return terminated_result

        try:
            driver = os.environ.get('CALL_CLI_OAR_TEST_DRIVER', '').strip() or 'antigravity'
            script_cfg = task.get('oar_script')
            if script_cfg is None and os.environ.get('CALL_CLI_OAR_SCRIPT_JSON'):
                script_cfg = json.loads(os.environ['CALL_CLI_OAR_SCRIPT_JSON'])

            guidance_text = prompt_guidance(task)
            open_timeout = float(os.environ.get('CALL_CLI_OAR_OPEN_TIMEOUT', OPEN_TIMEOUT_SECONDS))
            open_sent = send({
                'op': 'open',
                'driver': driver,
                'cwd': task['cwd'],
                'model': task.get('model'),
                'effort': task.get('effort'),
                'appendSystemPrompt': guidance_text,
                **({'resume': resume_sid} if resume_sid else {}),
                **({'script': script_cfg} if script_cfg is not None else {}),
            })

            opened_msg = None
            open_error = None
            open_deadline = time.monotonic() + open_timeout
            if open_sent:
                while time.monotonic() < open_deadline:
                    if store.get(task_id)['state'] == 'cancelling':
                        open_error = 'Cancelled during OAR session open'
                        break
                    try:
                        item = msg_queue.get(timeout=POLL_INTERVAL_SECONDS)
                    except queue.Empty:
                        continue
                    if item is None:
                        open_error = 'OAR bridge exited before session opened'
                        break
                    if item.get('type') == 'opened':
                        opened_msg = item
                        break
                    if item.get('type') == 'error':
                        open_error = f"{item.get('stage')}:{item.get('code')}: {item.get('message')}"
                        break
            else:
                open_error = 'Failed to write OAR open command'

            if opened_msg is None:
                _, rc = terminate_bridge()
                note = open_error or 'Timed out opening OAR session'
                if stderr_tail:
                    note = f'{note} ({stderr_tail[-1][:120]})'
                return TurnResult('unknown', rc, None, note=f'{note}; outcome unknown, no fallback.')

            session_id = opened_msg['sessionId']
            oar_session = {
                'driver': opened_msg.get('driver', driver),
                'runtime_id': opened_msg.get('runtimeId'),
                'oar_package': opened_msg.get('oarPackage'),
                'oar_version': opened_msg.get('oarVersion'),
                'session_id': session_id,
                'open_method': opened_msg.get('openMethod'),
                'cwd': task['cwd'],
                'model': opened_msg.get('model') or task.get('model'),
                'effort': opened_msg.get('effort') or task.get('effort'),
                'agy_approval': task.get('agy_approval'),
                'append_system_prompt_sent': bool(guidance_text),
            }
            store.update(task_id, oar_session=oar_session)
            emit('version', json.dumps({
                'package': oar_session['oar_package'],
                'version': oar_session['oar_version'],
                'driver': oar_session['driver'],
            }))
            emit('model', json.dumps({'model': oar_session['model'], 'effort': oar_session['effort']}))

            store.native_input_open(task_id, turn, launch_id, epoch)

            full_prompt = protocol_prompt(guidance_text + guidance(task) + task['prompt'])
            prompt_input_id = f'{task_id}:{turn}:prompt'
            if not send({'op': 'prompt', 'input': full_prompt, 'inputId': prompt_input_id}):
                store.native_input_end(task_id, turn, launch_id, epoch)
                _, rc = terminate_bridge()
                return TurnResult('unknown', rc, None, note='Failed to dispatch OAR prompt; outcome unknown.')

            decoder = EventDecoder()
            tool_names: dict[str, str] = {}
            outside_turn_count = 0
            steer_audit: dict[str, dict] = {}
            active_steers: set[str] = set()
            turn_outcome: dict | None = None
            cancel_sent = False
            bridge_exited = False
            bridge_error: str | None = None
            grace_ms = max(0, int(os.environ.get('CALL_CLI_OAR_POST_TURN_GRACE_MS', '0') or '0'))
            grace_deadline: float | None = None

            def record_outside_turn(ev: dict):
                nonlocal outside_turn_count
                outside_turn_count += 1
                emit('outside_turn_activity', _format_outside_turn(ev))
                store.update(task_id, oar_outside_turn_activity=outside_turn_count)

            def record_input_observed(item: dict):
                cid = item.get('inputId')
                if cid and cid in steer_audit:
                    steer_audit[cid]['runtime_observed'] = True
                    steer_audit[cid]['observed_kind'] = item.get('kind')
                    if item.get('requestId') and not steer_audit[cid].get('request_id'):
                        steer_audit[cid]['request_id'] = item.get('requestId')
                    store.update(task_id, oar_steer_audit=dict(steer_audit))

            def handle_dispose_item(item: dict):
                if item.get('type') == 'event' and item.get('outsideTurn'):
                    record_outside_turn(item.get('event') or {})
                elif item.get('type') == 'input_observed':
                    record_input_observed(item)

            def handle_bridge_item(item: dict):
                nonlocal turn_outcome, bridge_exited, bridge_error, grace_deadline
                mtype = item.get('type')
                if mtype == 'event':
                    ev = item.get('event') or {}
                    if item.get('outsideTurn'):
                        record_outside_turn(ev)
                    elif turn_outcome is None:
                        for ekind, etext in _feed_decoder(decoder, ev, tool_names):
                            emit(ekind, etext)
                elif mtype == 'control':
                    op = item.get('op')
                    outcome = item.get('outcome') or {}
                    if op == 'steer':
                        cid = item.get('inputId')
                        if cid and cid in steer_audit:
                            accepted_ok = outcome.get('kind') == 'accepted'
                            steer_audit[cid].update(
                                request_id=item.get('requestId'),
                                dispatched=accepted_ok,
                                outcome_kind=outcome.get('kind'),
                                **({'rejected_code': outcome.get('code')} if not accepted_ok else {}),
                            )
                            store.update(task_id, oar_steer_audit=dict(steer_audit))
                            if not accepted_ok and cid in active_steers:
                                active_steers.discard(cid)
                                try:
                                    store.native_dialogue_state(task_id, turn, launch_id, epoch, cid, 'cancelled')
                                except ValueError:
                                    pass
                    elif op == 'prompt' and outcome.get('kind') == 'rejected':
                        bridge_error = f"prompt_rejected:{outcome.get('code')}: {outcome.get('reason', '')}"
                elif mtype == 'input_observed':
                    record_input_observed(item)
                elif mtype == 'turn_ended':
                    for ekind, etext in decoder.finish():
                        emit(ekind, etext)
                    turn_outcome = item.get('outcome') or {'kind': 'unknown'}
                    if grace_ms > 0:
                        grace_deadline = time.monotonic() + (grace_ms / 1000.0)
                elif mtype == 'exited':
                    bridge_exited = True
                    for ekind, etext in decoder.finish():
                        emit(ekind, etext)
                elif mtype == 'error':
                    bridge_error = f"{item.get('stage')}:{item.get('code')}: {item.get('message')}"

            def drain_queued_items():
                nonlocal bridge_exited
                while True:
                    try:
                        extra = msg_queue.get_nowait()
                    except queue.Empty:
                        break
                    if extra is None:
                        bridge_exited = True
                        for ekind, etext in decoder.finish():
                            emit(ekind, etext)
                        break
                    handle_bridge_item(extra)
                    if bridge_exited or bridge_error is not None:
                        break

            while True:
                if turn_outcome is not None and (grace_deadline is None or time.monotonic() >= grace_deadline):
                    drain_queued_items()
                    break
                if bridge_exited or bridge_error is not None:
                    break

                wait_timeout = POLL_INTERVAL_SECONDS
                if grace_deadline is not None:
                    remaining = grace_deadline - time.monotonic()
                    if remaining <= 0:
                        drain_queued_items()
                        break
                    wait_timeout = min(POLL_INTERVAL_SECONDS, max(0.005, remaining))

                try:
                    item = msg_queue.get(timeout=wait_timeout)
                except queue.Empty:
                    item = 'EMPTY'

                if item is None:
                    bridge_exited = True
                    for ekind, etext in decoder.finish():
                        emit(ekind, etext)
                    break
                if item != 'EMPTY':
                    handle_bridge_item(item)
                    if bridge_exited or bridge_error is not None:
                        break
                    drain_queued_items()
                    if bridge_exited or bridge_error is not None:
                        break
                    if turn_outcome is not None and (grace_deadline is None or time.monotonic() >= grace_deadline):
                        drain_queued_items()
                        break

                if turn_outcome is None:
                    current = store.get(task_id)
                    if current['state'] == 'cancelling' and not cancel_sent:
                        cancel_sent = True
                        store.native_input_end(task_id, turn, launch_id, epoch)
                        send({'op': 'abort'})
                    elif not cancel_sent:
                        entry = store.native_dialogue_claim(task_id, turn, launch_id, epoch)
                        if entry is not None:
                            cid = entry['command_id']
                            store.native_dialogue_state(task_id, turn, launch_id, epoch, cid, 'send_started')
                            active_steers.add(cid)
                            steer_audit[cid] = {
                                'command_id': cid,
                                'input_id': cid,
                                'accepted': True,
                                'dispatched': False,
                                'request_id': None,
                                'runtime_observed': False,
                                'adopted': None,
                            }
                            store.update(task_id, oar_steer_audit=dict(steer_audit))
                            send({'op': 'steer', 'input': entry['prompt'], 'inputId': cid})

            for cid in list(active_steers):
                try:
                    if (turn_outcome or {}).get('kind') == 'completed' and not cancel_sent and steer_audit.get(cid, {}).get('dispatched'):
                        store.native_dialogue_state(task_id, turn, launch_id, epoch, cid, 'returned')
                    elif cancel_sent and (turn_outcome or {}).get('kind') == 'aborted':
                        store.native_dialogue_state(task_id, turn, launch_id, epoch, cid, 'cancelled')
                except ValueError:
                    pass

            store.native_input_end(task_id, turn, launch_id, epoch)

            disposed_msg, rc = terminate_bridge(on_item=handle_dispose_item)
            store.update(task_id, oar_outside_turn_activity=outside_turn_count, oar_steer_audit=dict(steer_audit))
            disposed_verified = bool(
                disposed_msg is not None
                and disposed_msg.get('sessionClosed') is True
                and disposed_msg.get('processExited') is True
                and rc == 0
            )

            if disposed_verified:
                store.oar_cleanup_commit(
                    task_id, turn, launch_id, epoch, cleanup_token,
                    {
                        'task': task_id,
                        'turn': turn,
                        'launch_id': launch_id,
                        'epoch': epoch,
                        'session_id': session_id,
                        'driver': oar_session['driver'],
                        'session_closed': True,
                        'process_exited': True,
                        'arbitraryDetachedDescendantsProven': False,
                    },
                )

            if bridge_error is not None:
                return TurnResult('unknown', rc, None, note=f'{bridge_error}; outcome unknown, no automatic replay.')

            if (turn_outcome or {}).get('kind') == 'completed' and not cancel_sent and disposed_verified:
                decoder.stop_reason = 'end_turn'
                note = outcome_note(decoder, 0, 'completed')
                if note:
                    return TurnResult('unknown', 0, 'end_turn', note=note)
                return TurnResult('completed', 0, 'end_turn')

            if (turn_outcome or {}).get('kind') == 'aborted' and cancel_sent and disposed_verified:
                return TurnResult('cancelled', 0, 'cancelled', note='OAR cancellation verified; business result not claimed')

            reason = (turn_outcome or {}).get('reason') or (turn_outcome or {}).get('kind') or 'unsettled'
            return TurnResult('unknown', rc, None, note=f'OAR turn did not settle cleanly ({reason}); outcome unknown, no automatic replay.')
        finally:
            terminate_bridge()
