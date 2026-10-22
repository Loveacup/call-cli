"""Standalone online collaboration coordinator (POSIX control host).

Client IDs are local capability handles, not authenticated native CLI identities.
Only public runner events are retained; no raw subprocess logs are captured here.
"""
from __future__ import annotations
import json
import uuid
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from bridge import Store, Origin
from runner import control as runner_control
from updates import device_lock

DEFAULT_DB = Path.home()/'.hermes/plugin-data/call-cli/tasks.sqlite3'


_polling = ContextVar('collab_polling', default=False)
_wait_deadline = ContextVar('collab_wait_deadline', default=None)


class _Store(Store):
    @contextmanager
    def connect(self):
        with super().connect() as db:
            if _polling.get():
                db.execute('PRAGMA busy_timeout=0')
            yield db


class Collaboration:
    def __init__(self, db=DEFAULT_DB, *, launch=None, control=None, lock=None):
        self.store = _Store(db)
        self.launch = launch or self._launch
        self.control = control or runner_control
        self.lock = lock or device_lock
        with self.store.connect() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS clients (id TEXT PRIMARY KEY, declared_runtime TEXT NOT NULL)')

    def _launch(self, task):
        import subprocess
        import sys
        return subprocess.Popen([sys.executable, str(Path(__file__).with_name('runner.py')),
                                 '--db', str(self.store.path), '--task', task['id']],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, close_fds=True,
                                start_new_session=True)

    def _dispatch(self, task):
        try:
            self.launch(task)
        except Exception as exc:
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current = self.store._owned(db, task['id'], Origin(**task['origin']))
                if current['turn'] == task['turn'] and current['state'] == 'queued':
                    current.update(state='unknown', launch_error=type(exc).__name__)
                    self.store._save(db, current)

    def connect(self, runtime):
        if runtime not in {'omp', 'codex', 'pi', 'hermes'}:
            raise ValueError('runtime must be omp, codex, pi or hermes')
        client = dict(client_id=str(uuid.uuid4()), declared_runtime=runtime)
        with self.store.connect() as conn:
            conn.execute('INSERT INTO clients VALUES (?,?)', (client['client_id'], runtime))
        return client

    def start(self, client, *, prompt, cwd, device='local', agent='codex',
              remote_shell='posix', write=False, resource='', timeout=900):
        from runner import device_identity
        origin = self.origin(client)
        if agent not in {'omp', 'codex', 'pi'} or remote_shell not in {'posix', 'powershell'}:
            raise ValueError('Invalid agent or remote shell')
        if not prompt.strip() or not cwd or not 0 < timeout <= 86400:
            raise ValueError('Nonempty prompt/cwd and timeout in (0,86400] required')
        from pathlib import PurePosixPath, PureWindowsPath
        path_type=Path if device=='local' else (PureWindowsPath if remote_shell=='powershell' else PurePosixPath)
        if not path_type(cwd).is_absolute():
            raise ValueError('cwd must be absolute on the execution device; disable shell path conversion')
        task = self.store.create(origin, device, agent, cwd, prompt, write, resource,
                                 device_key=device_identity(device))
        task = self.store.update(task['id'], shell=remote_shell, timeout=timeout,
                                 needs_session=True, acpx_created=False, session_creation_attempted=False)
        self._dispatch(task)
        return self.status(client, task['id'])

    def status(self, client, task_id):
        return self.store.get(task_id, self.origin(client))

    def events(self, client, task_id, after=0):
        return self.store.events(task_id, self.origin(client), after)

    def list(self, client):
        return self.store.list(self.origin(client))

    def intervene(self, client, task_id, prompt, *, command_id=None):
        origin = self.origin(client)
        if not prompt.strip():
            raise ValueError('Nonempty intervention required')
        command_id = command_id or str(uuid.uuid4())
        launch = False
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = self.store._owned(db, task_id, origin)
            commands = task.setdefault('commands', {})
            if command_id in commands:
                if commands[command_id]['prompt'] != prompt:
                    raise ValueError('command_id already used for a different prompt')
                return task
            if task.get('intervention', {}).get('status') == 'pending':
                raise ValueError('Another intervention is pending; wait or cancel it first')
            if task['state'] not in {'queued', 'running', 'cancelling', 'cancelled', 'completed', 'waiting_reply'}:
                raise ValueError('Task fate is not settled; intervention refused')
            entry = dict(command_id=command_id, prompt=prompt, source_turn=task['turn'], status='pending')
            commands[command_id] = entry
            task['intervention'] = entry
            if task['state'] == 'queued':
                task['prompt'] = prompt
                entry.update(status='queued', target_turn=task['turn'])
            elif task['state'] in {'running', 'cancelling'}:
                # The existing runner owns cancel transport and settlement confirmation.
                task['state'] = 'cancelling'
            else:
                self._resume(db, task, entry)
                launch = True
            self.store._save(db, task)
        if launch:
            self._dispatch(task)
        return self.status(client, task_id)

    def _resume(self, db, task, entry):
        task.update(turn=task['turn']+1, state='queued', prompt=entry['prompt'],
                    process_id='', runner_pid=0, question_id='', questions=[], answers={})
        entry.update(status='queued', target_turn=task['turn'])
        task['commands'][entry['command_id']] = entry
        self.store._check_writers(db, task)

    def _advance(self, client, task_id):
        origin = self.origin(client)
        launch = False
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = self.store._owned(db, task_id, origin)
            entry = task.get('intervention', {})
            if (entry.get('status') == 'pending' and entry['source_turn'] == task['turn']
                    and task['state'] in {'cancelled', 'completed', 'waiting_reply'}):
                self._resume(db, task, entry)
                self.store._save(db, task)
                launch = True
        if launch:
            self._dispatch(task)
        return self.status(client, task_id)

    def wait(self, client, task_id, *, after=0, timeout=5):
        import time
        deadline = _wait_deadline.get()
        if deadline is None:
            deadline = time.monotonic() + min(5, max(0, timeout))
        token = _polling.set(True)
        try:
            while True:
                busy = False
                try:
                    task = self._advance(client, task_id)
                    events = self.events(client, task_id, after)
                except sqlite3.OperationalError as exc:
                    if 'locked' not in str(exc):
                        raise
                    busy = True
                    # An exclusive SQLite commit can also briefly block reads.
                    # Never wait 15 seconds or invent a state when it is unreadable.
                    task, events = None, []
                    try:
                        task = self.status(client, task_id)
                        events = self.events(client, task_id, after)
                    except sqlite3.OperationalError as read_exc:
                        if 'locked' not in str(read_exc):
                            raise
                remaining = deadline-time.monotonic()
                if busy or events or task['state'] not in {'queued', 'running', 'cancelling'} or remaining <= 0:
                    return dict(task=task, events=events, after=events[-1]['seq'] if events else after, busy=busy)
                time.sleep(min(.05, remaining))
        finally:
            _polling.reset(token)

    def reply(self, client, task_id, question_id, prompt):
        origin = self.origin(client)
        launch = False
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = self.store._owned(db, task_id, origin)
            questions = task.get('questions', [])
            if (task['state'] not in {'waiting_reply', 'completed'} or not question_id
                    or question_id not in questions or not prompt.strip()
                    or task.get('intervention', {}).get('status') == 'pending'):
                raise ValueError('Reply requires an unanswered question_id in a settled turn')
            task.setdefault('answers', {})[question_id] = prompt
            questions.remove(question_id)
            if questions:
                task['question_id'] = questions[0]
            else:
                answers = list(task['answers'].items())
                text = answers[0][1] if len(answers) == 1 else '\n'.join(k+': '+v for k, v in answers)
                task.update(turn=task['turn']+1, state='queued', prompt=text,
                            process_id='', runner_pid=0, question_id='', questions=[], answers={})
                self.store._check_writers(db, task)
                launch = True
            self.store._save(db, task)
        if launch:
            self._dispatch(task)
        return self.status(client, task_id)

    def cancel(self, client, task_id):
        origin = self.origin(client)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = self.store._owned(db, task_id, origin)
            entry = task.get('intervention', {})
            if entry.get('status') in {'pending', 'queued'}:
                entry['status'] = 'withdrawn'
                task['commands'][entry['command_id']] = entry
            if task['state'] == 'queued':
                task.update(state='cancelled', prompt='')
            elif task['state'] == 'running':
                task['state'] = 'cancelling'
            self.store._save(db, task)
        return self.status(client, task_id)

    def close(self, client, task_id):
        origin = self.origin(client)
        before = self.store.get(task_id, origin)
        task = self.store.begin_close(task_id, origin)
        success = False
        try:
            with self.lock(task, exclusive=False) as acquired:
                if acquired:
                    never_claimed = (task.get('acpx_created') is False
                                     and task.get('session_creation_attempted') is False
                                     and task['turn'] == before['turn']
                                     and before['state'] == 'cancelled')
                    if never_claimed:
                        success = True
                    elif self.control(task, 'close', timeout=20).returncode == 0:
                        result = self.control(task, 'status', timeout=20)
                        if result.returncode == 0:
                            try:
                                snapshot = json.loads(result.stdout)
                            except (ValueError, TypeError, UnicodeError):
                                snapshot = None
                            success = (isinstance(snapshot, dict)
                                       and snapshot.get('action') == 'status_snapshot'
                                       and snapshot.get('status') == 'no-session')
        finally:
            self.store.finish_close(task_id, task['turn'], success)
        return self.status(client, task_id)

    def origin(self, client):
        with self.store.connect() as conn:
            row = conn.execute('SELECT declared_runtime FROM clients WHERE id=?', (client,)).fetchone()
        if not row:
            raise PermissionError('Unknown client handle')
        return Origin('standalone', client, client, row[0])


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=str(DEFAULT_DB))
    parser.add_argument('--client', help='Capability handle returned by connect, not a native session ID')
    sub = parser.add_subparsers(dest='action', required=True)
    connect = sub.add_parser('connect')
    connect.add_argument('--runtime', choices=['omp', 'codex', 'pi', 'hermes'], required=True,
                         help='Declared caller runtime (not verified native provenance)')
    start = sub.add_parser('start')
    start.add_argument('--device', default='local')
    start.add_argument('--agent', choices=['omp', 'codex', 'pi'], default='codex')
    start.add_argument('--cwd', required=True)
    start.add_argument('--remote-shell', choices=['posix', 'powershell'], default='posix')
    start.add_argument('--write', action='store_true')
    start.add_argument('--resource', default='')
    start.add_argument('--timeout', type=float, default=900)
    start.add_argument('--prompt', required=True)
    sub.add_parser('list')
    for action in ['status', 'events', 'wait', 'reply', 'intervene', 'cancel', 'close']:
        p = sub.add_parser(action)
        p.add_argument('--task', required=True)
        if action in {'events', 'wait'}:
            p.add_argument('--after', type=int, default=0)
        if action == 'wait':
            p.add_argument('--timeout', type=float, default=5)
        if action in {'reply', 'intervene'}:
            p.add_argument('--prompt', required=True)
        if action == 'reply':
            p.add_argument('--question-id', required=True)
        if action == 'intervene':
            p.add_argument('--command-id', help='Stable ID for retry-safe submission')
    opts = vars(parser.parse_args(argv))
    action, db, client = opts.pop('action'), opts.pop('db'), opts.pop('client')
    polling_token = _polling.set(action == 'wait')
    deadline_token = _wait_deadline.set(
        time.monotonic() + min(5, max(0, opts['timeout'])) if action == 'wait' else None)
    try:
        if action != 'connect' and not client:
            raise ValueError('--client is required for this action')
        c = Collaboration(db)
        if action == 'connect':
            result = c.connect(**opts)
        else:
            if not client:
                raise ValueError('--client is required for this action')
            if 'task' in opts:
                opts['task_id'] = opts.pop('task')
            result = getattr(c, action)(client, **opts)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        if action == 'wait' and isinstance(exc, sqlite3.OperationalError) and 'locked' in str(exc):
            print(json.dumps(dict(task=None, events=[], after=opts.get('after', 0), busy=True)))
            return 0
        # Avoid raw transport exception messages, stderr and environment leakage.
        print(json.dumps(dict(error=type(exc).__name__, message='Action failed; inspect owned task status before retrying')))
        return 1
    finally:
        _wait_deadline.reset(deadline_token)
        _polling.reset(polling_token)


if __name__ == '__main__':
    raise SystemExit(main())
