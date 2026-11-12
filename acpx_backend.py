"""ACPX execution adapter shared by OMP, Codex, Pi and AGY."""
import json
import os
from pathlib import Path
import queue
import re
import selectors
import subprocess
import threading
import time
try:
    from .events import EventDecoder
    from .transport import command, protocol_prompt
except ImportError:
    from events import EventDecoder
    from transport import command, protocol_prompt
try:
    from .skills import stage, guidance
except ImportError:
    from skills import stage, guidance

STARTUP_TIMEOUT = 120
QUIET_NOTICE_SECONDS = 600
TRANSPORT_CLEANUP_TIMEOUT = 2


class AcpxBackend:
    def prepare_start(self, model, effort, approval):
        if approval is not None:
            raise ValueError('agy_approval is supported only for AGY')
        self.validate_selection(model, effort)
        return {k:v for k,v in dict(model=model, effort=effort).items() if v is not None}

    def validate_selection(self, model, effort):
        for value in (model, effort):
            if value is not None and (not isinstance(value, str) or not value.strip() or value != value.strip()):
                raise ValueError('model/effort must be nonempty literal IDs')
        if effort is not None:
            raise ValueError('effort is currently supported only for AGY')

    def agent_args(self, task):
        return [task['agent']]

    def permission_mode(self, task):
        return 'approve-all' if task['write'] else 'approve-reads'

    def prompt_guidance(self, task):
        return ''

    def pre_prompt(self, store, task, emit):
        pass

    def outcome_note(self, decoder, exit_code, state):
        return None

    def control(self, task, action, timeout=20):
        args = ['acpx','--cwd',task['cwd'],'--format','json']
        if action == 'new' and task.get('model') is not None:
            args += ['--model', task['model']]
        args += self.agent_args(task)
        if action == 'new':
            args += ['sessions','new','--name',task['session']]
        elif action in {'cancel','status'}:
            args += [action,'-s',task['session']]
        elif action == 'close':
            args += ['sessions','close',task['session']]
        elif action in {'model', 'effort'}:
            args += ['set', '-s', task['session'], 'model' if action == 'model' else 'thought_level', task[action]]
        elif action == 'show':
            args += ['sessions','show',task['session']]
        else:
            raise ValueError('Unknown control action')
        return subprocess.run(command(task['device'],task.get('shell','posix'),args),capture_output=True,timeout=timeout)

    def apply_selection(self, task):
        selected = {k: task[k] for k in ('model', 'effort') if task.get(k) is not None}
        if not selected:
            return {}
        for key in selected:
            if self.control(task, key, timeout=60).returncode:
                raise RuntimeError('Explicit '+key+' selection failed; no fallback or prompt submitted')
        result = self.control(task, 'show')
        if result.returncode:
            raise RuntimeError('Cannot read back selected model')
        record = json.loads(result.stdout)
        state = record.get('acpx', {})
        config = {o['id']: o.get('currentValue') for o in state.get('config_options', [])}
        if (record.get('name') != task['session']
                or ('model' in selected and state.get('current_model_id') != selected['model'])
                or ('effort' in selected and config.get('thought_level') != selected['effort'])):
            raise RuntimeError('Selected model/effort readback mismatch; no prompt submitted')
        return selected

    def transport(self, task):
        """Send our small facade and private prompt on stdin, including over existing SSH."""
        request_id = task['id'] + ':' + str(task['turn'])
        options = dict(cwd=task['cwd'], sessionId=task['acpx_record_id'],
                       sessionName=task['session'], requestId=request_id, write=task['write'],
                       expectedVersion=task['execution_version'])
        if task.get('timeout') is not None:
            options['timeoutMs'] = task['timeout'] * 1000
        options['permissionMode'] = self.permission_mode(task)
        for key in ('model', 'effort'):
            if task.get(key) is not None:
                options[key] = task[key]
        payload = dict(source=Path(__file__).with_name('shared-prompt.cjs').read_text(),
                       options=options, prompt=protocol_prompt(
                           self.prompt_guidance(task)+guidance(task)+task['prompt']))
        # Single quotes also avoid Windows PowerShell 5.1's native double-quote loss.
        bootstrap = ("(async()=>{try{process.stdout.on('error',()=>{process.exitCode=1;});"
                     "const p=JSON.parse(require('fs').readFileSync(0,'utf8'));"
                     "const m={exports:{}};new Function('require','module','exports',p.source)(require,m,m.exports);"
                     "process.exitCode=await m.exports.run(p.options,p.prompt);}catch{process.exitCode=1;}})();")
        return command(task['device'], task.get('shell', 'posix'), ['node', '-e', bootstrap]), json.dumps(payload).encode('utf-8'), request_id

    def run(self, store, task, emit):
        try:
            from .backends import TurnResult
        except ImportError:
            from backends import TurnResult
        task_id, turn = task["id"], task["turn"]
        proc = None
        execution_lock = None
        threads = []
        io_stop = threading.Event()
        def stop_transport():
            # This Popen child only: never a process group or the shared queue owner.
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:proc.wait(timeout=TRANSPORT_CLEANUP_TIMEOUT)
                except subprocess.TimeoutExpired:
                    proc.kill();proc.wait(timeout=TRANSPORT_CLEANUP_TIMEOUT)
        try:
            try:
                from .updates import ensure_current, device_lock
            except ImportError:
                from updates import ensure_current, device_lock
            # Version maintenance happens once per new task, not on replies/status.
            if 'version_check' not in task and (turn == 1 or task.get('needs_session', False)):
                checked = ensure_current(store, task)
                store.update(task_id, version_check=checked)
                emit('version', json.dumps(checked))
            # All turns, including replies, hold a shared installation lock until settlement.
            execution_lock=device_lock(task)
            execution_lock.__enter__()
            if store.get(task_id)['state']=='cancelling':
                return TurnResult('cancelled', 0, 'cancelled', 'Cancelled before provider launch')
            if turn == 1 or task.get('needs_session',False):
                store.update(task_id,session_creation_attempted=True)
                new = self.control(task,'new',timeout=60)
                if new.returncode:
                    raise RuntimeError('ACPX session creation failed (exit '+str(new.returncode)+')')
                created = json.loads(new.stdout)
                record_id = created.get('acpxRecordId')
                if not isinstance(record_id, str) or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', record_id):
                    raise RuntimeError('ACPX creation omitted a valid native record ID')
                store.update(task_id, acpx_created=True, needs_session=False, acpx_record_id=record_id)
            task = store.get(task_id)
            if not task.get('acpx_record_id'):
                # Resume a pre-upgrade task by reading its exact named session, never by
                # creating a replacement or guessing a provider ID.
                shown = self.control(task, 'show')
                if shown.returncode:
                    raise RuntimeError('Cannot resolve existing native session')
                record = json.loads(shown.stdout)
                record_id = record.get('acpxRecordId')
                if (record.get('name') != task['session'] or not isinstance(record_id, str)
                        or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', record_id)):
                    raise RuntimeError('Existing native session identity did not verify')
                task = store.update(task_id, acpx_record_id=record_id)
            # The shared installation lock is held. A task may have waited for a reply
            # across an allowed update, so its creation-time version is historical only.
            version = subprocess.run(command(task['device'], task.get('shell', 'posix'), ['acpx', '--version']),
                                     capture_output=True, timeout=20)
            installed = version.stdout.decode('utf-8').strip()
            if version.returncode or not re.fullmatch(r'\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?', installed):
                raise RuntimeError('Cannot verify the current locked ACPX installation')
            task = store.update(task_id, execution_version=installed)
            selected = self.apply_selection(task)
            if selected:
                emit('model', json.dumps(selected))
            self.pre_prompt(store, task, emit)
            task = stage(store, task, emit)
            argv, payload, request_id = self.transport(task)
            started_at = time.monotonic()
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            output = queue.Queue()
            received_lock = threading.Lock()
            received_acceptance = received_terminal = reader_ended = False
            write_done = threading.Event()
            write_failed = threading.Event()
            reader_failed = threading.Event()
            cleanup_expired = False
            os.set_blocking(proc.stdout.fileno(), False)
            os.set_blocking(proc.stdin.fileno(), False)
            os.set_blocking(proc.stderr.fileno(), False)
            pending_output = bytearray()
            def receive():
                nonlocal received_acceptance, received_terminal, reader_ended
                # Called under received_lock, including the final pre-deadline check.
                if reader_ended or received_terminal:return
                for _ in range(16):
                    try:chunk = os.read(proc.stdout.fileno(), 65536)
                    except BlockingIOError:return
                    if not chunk:
                        reader_ended = True
                    pending_output.extend(chunk)
                    while True:
                        end = pending_output.find(b'\n')
                        if end < 0:
                            if not reader_ended or not pending_output:break
                            end = len(pending_output)-1
                        line = bytes(pending_output[:end+1])
                        del pending_output[:end+1]
                        try:frame = json.loads(line.decode('utf-8','replace'))
                        except (json.JSONDecodeError,UnicodeError):frame = None
                        if isinstance(frame, dict) and frame.get('requestId') == request_id:
                            received_acceptance |= (frame.get('type') == 'callcli.shared.event'
                                                    and frame.get('event', {}).get('type') == 'prompt_started')
                            received_terminal |= frame.get('type') == 'callcli.shared.result'
                        output.put(line)
                        if received_terminal:return
                    if reader_ended:
                        output.put(None)
                        return
            def reader():
                try:
                    with selectors.DefaultSelector() as poller:
                        poller.register(proc.stdout, selectors.EVENT_READ)
                        while not io_stop.is_set():
                            if not poller.select(.25):continue
                            with received_lock:
                                if reader_ended:return
                                receive()
                                if reader_ended or received_terminal:return
                except (OSError, ValueError):
                    reader_failed.set()
            def drain_errors():
                # Discard private stderr without ever waiting for inherited pipe EOF.
                with selectors.DefaultSelector() as poller:
                    poller.register(proc.stderr, selectors.EVENT_READ)
                    while not io_stop.is_set():
                        if not poller.select(.1):continue
                        try:chunk = os.read(proc.stderr.fileno(), 65536)
                        except BlockingIOError:continue
                        if not chunk:return
            def write_payload():
                try:
                    with selectors.DefaultSelector() as poller:
                        poller.register(proc.stdin, selectors.EVENT_WRITE)
                        remaining = memoryview(payload)
                        while remaining and not io_stop.is_set():
                            if not poller.select(.1):continue
                            try:sent = os.write(proc.stdin.fileno(), remaining[:65536])
                            except BlockingIOError:continue
                            remaining = remaining[sent:]
                    proc.stdin.close()
                except (OSError, ValueError):
                    write_failed.set()
                finally:
                    write_done.set()
            threads=[threading.Thread(target=reader,daemon=True,name='callcli-transport-reader'),
                     threading.Thread(target=drain_errors,daemon=True,name='callcli-transport-stderr'),
                     threading.Thread(target=write_payload,daemon=True,name='callcli-transport-stdin')]
            for t in threads:t.start()
            decoder = EventDecoder(shared_request_id=request_id)
            ended = False
            cancelling = False
            stop_started = 0.0
            deadline = started_at+task['timeout']+90 if task.get('timeout') is not None else None
            startup_deadline = started_at+STARTUP_TIMEOUT if deadline is None else None
            startup_expired = False
            last_visible = started_at
            last_notice = started_at
            last_summary = 'No stage or tool status observed.'
            def consume(line):
                nonlocal ended, last_visible, last_summary
                if line is None:
                    ended = True
                    return
                try:event=json.loads(line.decode('utf-8','replace'))
                except (json.JSONDecodeError,UnicodeError):return
                decoded = decoder.feed(event)
                if decoder.shared_result_seen:ended = True
                public = event.get('event', {})
                activity = (event.get('type') == 'callcli.shared.event' and event.get('requestId') == request_id
                            and not decoder.shared_result_seen and
                            (public.get('type') == 'prompt_started' or
                             (public.get('type') == 'text_delta' and public.get('stream') == 'output'
                              and public.get('tag') == 'agent_message_chunk' and bool(public.get('text')))
                             or (public.get('type') == 'tool_call' and bool(decoded))))
                if activity:
                    last_visible = time.monotonic()
                for kind,text in decoded:
                    if kind in {'stage', 'tool'}:last_summary = text[:500]
                    emit(kind,text)
            while not ended:
                # Process already delivered frames before considering a deadline.
                for _ in range(64):
                    try:consume(output.get_nowait())
                    except queue.Empty:break
                    if ended:break
                if ended:break
                current=store.get(task_id)
                now = time.monotonic()
                if not cancelling and startup_deadline is not None and now > startup_deadline:
                    with received_lock:
                        receive()
                        startup_expired = (not received_acceptance and not received_terminal and not reader_ended
                                           and current['state'] != 'cancelling')
                if not received_terminal and (current['state']=='cancelling' or startup_expired or (deadline is not None and now>deadline)) and not cancelling:
                    cancelling=True;stop_started=time.monotonic()
                    result=self.control(task,'cancel',timeout=20)
                    if result.returncode:
                        raise RuntimeError('Cancel was not accepted; remote fate unknown')
                    if not write_done.is_set():
                        # Stop the local pipe reader before joining a blocked buffered writer.
                        stop_transport()
                        io_stop.set()
                        with received_lock:
                            if not reader_ended:receive()
                        for _ in range(64):
                            try:consume(output.get_nowait())
                            except queue.Empty:break
                        break
                if write_failed.is_set():
                    raise RuntimeError('Transport payload delivery failed; remote fate unknown')
                if reader_failed.is_set():
                    raise RuntimeError('Transport output observation failed; remote fate unknown')
                if cancelling and time.monotonic()-stop_started>25:
                    raise RuntimeError('Cancel accepted but prompt has not settled')
                if (not cancelling and current['state'] == 'running'
                        and now-last_notice >= QUIET_NOTICE_SECONDS):
                    with received_lock:
                        receive()
                        if not received_terminal and not reader_ended:
                            notice_kind = 'quiet' if now-last_visible >= QUIET_NOTICE_SECONDS else 'checkpoint'
                            emit('stage', 'Coordinator review requested ('+notice_kind+'); elapsed '
                                 +str(int(now-started_at))+'s. Inspect this task and its events, then choose to '
                                 'keep waiting, explicitly intervene, or cancel. Activity or silence alone does not '
                                 'establish success or a hang. No cancellation or closure requested. '
                                 'Last visible status: '+last_summary, quiet_notice=True,
                                 notice_kind=notice_kind, elapsed=int(now-started_at))
                            last_notice = now
                try:consume(output.get(timeout=.25))
                except queue.Empty:continue
            if received_terminal and not decoder.shared_result_seen:
                # Receipt is a finite input boundary; preserve every prior frame in order.
                while not decoder.shared_result_seen:
                    try:consume(output.get_nowait())
                    except queue.Empty:break
            for kind,text in decoder.finish():emit(kind,text)
            if not cleanup_expired:
                try:rc=proc.wait(timeout=TRANSPORT_CLEANUP_TIMEOUT)
                except subprocess.TimeoutExpired:cleanup_expired = True
            if cleanup_expired:
                io_stop.set()
                stop_transport()
                rc = proc.returncode
            state='cancelled' if cancelling else ('waiting_reply' if store.get(task_id)['question_id'] else 'completed')
            note = self.outcome_note(decoder, rc, state) if not cancelling else None
            if startup_expired and decoder.shared_result_seen and decoder.stop_reason != 'cancelled':
                cancelling = False
                state = 'waiting_reply' if store.get(task_id)['question_id'] else 'completed'
                note = self.outcome_note(decoder, rc, state)
            if startup_expired and not decoder.prompt_started and (not decoder.shared_result_seen or decoder.stop_reason == 'cancelled'):
                state = 'unknown'
                note = ('ACPX prompt acceptance not observed within '+str(STARTUP_TIMEOUT)+'s; '
                        'startup cancellation requested, execution outcome unknown. Reconcile the exact '
                        'session and artifacts before any authorized continuation; no automatic replay.')
            if cleanup_expired:
                state = 'unknown'
                note = ((note+' ') if note else '')+('Local transport cleanup did not settle after the authoritative result; '
                        'remote outcome must be reconciled; no automatic replay.')
            settled = rc == 0 and not decoder.failed and decoder.stop_reason in {'cancelled', 'end_turn'}
            if settled and not cancelling and decoder.stop_reason == 'end_turn' and state == 'completed' and not decoder.final_answer.strip() and not note:
                note = ('Protocol ended with no final answer (or none after the last tool event); '
                        'stage, skill reports and tool status are not completion evidence. '
                        'Outcome unknown; reconcile the exact session and artifacts; no automatic replay.')
            if ((cancelling and not settled) or
                    (not cancelling and (not settled or decoder.stop_reason!='end_turn' or
                                         (state != 'waiting_reply' and not decoder.final_answer.strip())))):
                # Failure can be an uncertain effect; retain the workspace reservation.
                state='unknown'
            if note:
                state='unknown'
            if decoder.error_category == 'auth_required':
                source = ('watchSession prompt error.code=-32000/data.authRequired=true'
                          if decoder.error_source == 'session_watch' else 'turn.result.error.detailCode=AUTH_REQUIRED')
                diagnostic = ('ACPX Shared Runtime '+source+': '
                              'runtime-reported authentication required; authentication origin unknown. '
                              'Preserve partial results; ask the operator to check authentication before '
                              'any explicitly authorized continuation; no automatic replay.')
                note = (note+' ' if note else '')+diagnostic
            elif decoder.error_category == 'timeout':
                diagnostic = ('ACPX Shared Runtime turn.result.error.code=TIMEOUT: '
                              'runtime-reported prompt deadline'+(' (configured timeout '+str(task['timeout'])+'s)' if task.get('timeout') is not None else '')+'. '
                              'Work may be partially applied; reconcile artifacts and external writes before '
                              'any authorized continuation; no automatic replay.')
                note = (note+' ' if note else '')+diagnostic
            elif decoder.error_category == 'connection_lost':
                diagnostic = ('ACPX Shared Runtime turn.result.error.detailCode=AGENT_DISCONNECTED: '
                              'runtime-reported agent connection loss; cause unknown. '
                              'Work may be partially applied; reconcile before any authorized continuation; '
                              'no automatic replay.')
                note = (note+' ' if note else '')+diagnostic
            return TurnResult(state, rc, decoder.stop_reason, note)
        finally:
            io_stop.set()
            stop_transport()
            for t in threads:t.join(timeout=TRANSPORT_CLEANUP_TIMEOUT)
            if proc is not None:
                for stream in [proc.stdin,proc.stdout,proc.stderr]:
                    if stream:stream.close()
            if execution_lock is not None:
                execution_lock.__exit__(None,None,None)
