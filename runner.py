"""ACPX transport. Runs as a native Hermes tracked background process, not a daemon."""
from __future__ import annotations
import argparse
import base64
import json
import os
from pathlib import Path
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
try:
    from .bridge import Store, EventDecoder
except ImportError:
    from bridge import Store, EventDecoder


def command(device, shell, argv):
    if device == 'local':
        return argv
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',device):
        raise ValueError('Use an existing SSH host alias, not shell text')
    if shell == 'powershell':
        literal = lambda v: "'"+str(v).replace("'","''")+"'"
        script = "$ProgressPreference='SilentlyContinue'; [Console]::OutputEncoding=[System.Text.Encoding]::UTF8; & "+' '.join(literal(v) for v in argv)+"; exit $LASTEXITCODE"
        remote = 'powershell -NoProfile -EncodedCommand '+base64.b64encode(script.encode('utf-16le')).decode()
    else:
        remote = 'export PATH=/opt/homebrew/bin:$HOME/.local/bin:$HOME/.bun/bin:$PATH; exec '+shlex.join(argv)
    return ['ssh','-T','-o','BatchMode=yes','-o','ConnectTimeout=8',device,remote]


def protocol_prompt(text):
    return ('You are participating in a new CLI collaboration session. Work only within the stated scope; do not commit, push, deploy, or change unrelated files. '
            'Do not use memory for this operational task. For meaningful stages emit a public line CALLCLI_STAGE followed by a short status. '
            'If you need an answer, emit CALLCLI_QUESTION followed by the question on a single line and end this turn. '
            'Do not include private reasoning. End with actual results, verification and remaining issues.\n\n'+text)


def shared_prompt_transport(task):
    """Send our small facade and private prompt on stdin, including over existing SSH."""
    request_id = task['id'] + ':' + str(task['turn'])
    options = dict(cwd=task['cwd'], sessionId=task['acpx_record_id'],
                   sessionName=task['session'], requestId=request_id, write=task['write'],
                   timeoutMs=task.get('timeout', 900) * 1000,
                   expectedVersion=task['execution_version'])
    payload = dict(source=Path(__file__).with_name('shared-prompt.cjs').read_text(),
                   options=options, prompt=protocol_prompt(task['prompt']))
    # Single quotes also avoid Windows PowerShell 5.1's native double-quote loss.
    bootstrap = ("(async()=>{try{process.stdout.on('error',()=>{process.exitCode=1;});"
                 "const p=JSON.parse(require('fs').readFileSync(0,'utf8'));"
                 "const m={exports:{}};new Function('require','module','exports',p.source)(require,m,m.exports);"
                 "process.exitCode=await m.exports.run(p.options,p.prompt);}catch{process.exitCode=1;}})();")
    return command(task['device'], task.get('shell', 'posix'), ['node', '-e', bootstrap]), json.dumps(payload).encode('utf-8'), request_id


def device_identity(device):
    if device=='local':return 'local'
    command(device,'posix',['true'])
    result=subprocess.run(['ssh','-G',device],capture_output=True,text=True,timeout=10)
    if result.returncode:raise ValueError('Could not resolve SSH host configuration')
    fields=dict(line.split(None,1) for line in result.stdout.splitlines() if len(line.split(None,1))==2)
    return 'ssh:'+fields['user']+'@'+fields['hostname'].lower()+':'+fields.get('port','22')


def control(task, action, timeout=20):
    args = ['acpx','--cwd',task['cwd'],'--format','json',task['agent']]
    if action == 'new':
        args += ['sessions','new','--name',task['session']]
    elif action in {'cancel','status'}:
        args += [action,'-s',task['session']]
    elif action == 'close':
        args += ['sessions','close',task['session']]
    elif action == 'show':
        args += ['sessions','show',task['session']]
    else:
        raise ValueError('Unknown control action')
    return subprocess.run(command(task['device'],task.get('shell','posix'),args),capture_output=True,timeout=timeout)


def run_task(db_path, task_id):
    store = Store(db_path)
    task = store.get(task_id)
    turn = task['turn']
    counter = 0
    def emit(kind,text,**extra):
        nonlocal counter
        counter += 1
        token = str(turn)+':'+str(counter)
        if store.event(task_id,token,kind,text,turn=turn,**extra):
            # Public text only. Hermes's native completion/heartbeat pins its parent session.
            if kind != 'tool':
                print(json.dumps(dict(task_id=task_id,turn=turn,event=kind,text=text[:4000],event_id=token,**extra)),flush=True)
    # Claim exactly the queued turn; duplicate launches never replay provider work.
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current = json.loads(db.execute('SELECT data FROM tasks WHERE id=?',(task_id,)).fetchone()[0])
        if current['state'] != 'queued' or current['turn'] != turn:
            raise ValueError('Turn already claimed or not queued')
        current.update(state='running',runner_pid=os.getpid())
        store._save(db,current)
    task = store.get(task_id)
    emit('started',task['device']+'/'+task['agent'])
    proc = None
    execution_lock = None
    try:
        try:
            from .updates import ensure_current, device_lock
        except ImportError:
            from updates import ensure_current, device_lock
        # Version maintenance happens once per new task, not on replies/status.
        if turn == 1:
            emit('version',json.dumps(ensure_current(store,task)))
        # All turns, including replies, hold a shared installation lock until settlement.
        execution_lock=device_lock(task)
        execution_lock.__enter__()
        if store.get(task_id)['state']=='cancelling':
            store.finish_turn(task_id,turn,'cancelled',0,'cancelled')
            emit('cancelled','Cancelled before provider launch')
            return 0
        if turn == 1 or task.get('needs_session',False):
            store.update(task_id,session_creation_attempted=True)
            new = control(task,'new',timeout=60)
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
            shown = control(task, 'show')
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
        argv, payload, request_id = shared_prompt_transport(task)
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
                result=control(task,'cancel',timeout=20)
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
        settled = rc == 0 and not decoder.failed and decoder.stop_reason in {'cancelled', 'end_turn'}
        if ((cancelling and not settled) or
                (not cancelling and (not settled or decoder.stop_reason!='end_turn' or not decoder.text.strip()))):
            # Failure can be an uncertain effect; retain the workspace reservation.
            state='unknown'
        store.finish_turn(task_id,turn,state,rc,decoder.stop_reason)
        emit(state,'Prompt exited '+str(rc))
        return 0 if state in {'completed','waiting_reply','cancelled'} else 1
    except Exception as exc:
        # Never infer remote worker death from an SSH transport failure.
        store.finish_turn(task_id,turn,'unknown',None,None)
        emit('unknown',type(exc).__name__+': '+str(exc)[:500])
        return 1
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


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--db',required=True)
    parser.add_argument('--task',required=True)
    opts=parser.parse_args()
    raise SystemExit(run_task(opts.db,opts.task))
