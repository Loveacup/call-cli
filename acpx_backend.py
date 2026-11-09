"""ACPX execution adapter shared by OMP, Codex, Pi and AGY."""
import json
from pathlib import Path
import queue
import re
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
                       timeoutMs=task.get('timeout', 900) * 1000,
                       expectedVersion=task['execution_version'])
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
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            proc.stdin.write(payload);proc.stdin.close()
            output = queue.Queue()
            def reader():
                for line in proc.stdout:
                    output.put(line)
                output.put(None)
            def drain_errors():
                # Do not persist arbitrary adapter stderr, which may include configuration or plugin data.
                for _ in proc.stderr:
                    pass
            threads=[threading.Thread(target=reader,daemon=True),threading.Thread(target=drain_errors,daemon=True)]
            for t in threads:t.start()
            decoder = EventDecoder(shared_request_id=request_id)
            ended = False
            cancelling = False
            stop_started = 0.0
            deadline = time.monotonic()+task.get('timeout',900)+90
            while not ended:
                current=store.get(task_id)
                if (current['state']=='cancelling' or time.monotonic()>deadline) and not cancelling:
                    cancelling=True;stop_started=time.monotonic()
                    result=self.control(task,'cancel',timeout=20)
                    if result.returncode:
                        raise RuntimeError('Cancel was not accepted; remote fate unknown')
                if cancelling and time.monotonic()-stop_started>25:
                    raise RuntimeError('Cancel accepted but prompt has not settled')
                try:line=output.get(timeout=.25)
                except queue.Empty:continue
                if line is None:ended=True;continue
                try:event=json.loads(line.decode('utf-8','replace'))
                except (json.JSONDecodeError,UnicodeError):continue
                for kind,text in decoder.feed(event):emit(kind,text)
            for kind,text in decoder.finish():emit(kind,text)
            rc=proc.wait(timeout=10)
            for t in threads:t.join(timeout=2)
            state='cancelled' if cancelling else ('waiting_reply' if store.get(task_id)['question_id'] else 'completed')
            note = self.outcome_note(decoder, rc, state) if not cancelling else None
            settled = rc == 0 and not decoder.failed and decoder.stop_reason in {'cancelled', 'end_turn'}
            if ((cancelling and not settled) or
                    (not cancelling and (not settled or decoder.stop_reason!='end_turn' or not decoder.text.strip()))):
                # Failure can be an uncertain effect; retain the workspace reservation.
                state='unknown'
            if note:
                state='unknown'
            return TurnResult(state, rc, decoder.stop_reason, note)
        finally:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill();proc.wait()
            if proc is not None:
                for stream in [proc.stdout,proc.stderr]:
                    if stream:stream.close()
            if execution_lock is not None:
                execution_lock.__exit__(None,None,None)
