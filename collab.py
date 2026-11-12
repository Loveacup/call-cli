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
from backends import BACKENDS, prepare_start

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
                                 '--db', str(self.store.path), '--task', task['id'],
                                 '--turn', str(task['turn']), '--launch-id', task['launch_id'],
                                 '--epoch', str(task.get('control_epoch', 1))],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, close_fds=True,
                                start_new_session=True)

    def _dispatch(self, task):
        task = self.store.claim_launch(task['id'], task['turn'], task.get('control_epoch', 1))
        try:
            self.launch(task)
        except Exception as exc:
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current = self.store._owned(db, task['id'], Origin(**task['origin']))
                if current['turn'] == task['turn'] and current['state'] == 'queued':
                    current.update(state='unknown', launch_error=type(exc).__name__)
                    self.store._target_commands(current, 'target_unknown')
                    self.store._save(db, current)

    def connect(self, runtime):
        if runtime not in {'omp', 'codex', 'pi', 'hermes'}:
            raise ValueError('runtime must be omp, codex, pi or hermes')
        client = dict(client_id=str(uuid.uuid4()), declared_runtime=runtime)
        with self.store.connect() as conn:
            conn.execute('INSERT INTO clients VALUES (?,?)', (client['client_id'], runtime))
        return client

    def start(self, client, *, prompt, cwd, device='local', agent='codex',
              remote_shell='posix', write=False, resource='', timeout=None, model=None, effort=None,
              agy_approval=None, skills=None):
        from transport import device_identity
        origin = self.origin(client)
        if remote_shell not in {'posix', 'powershell'}:
            raise ValueError('Invalid agent or remote shell')
        if (not prompt.strip() or not cwd or (timeout is not None and
                (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 86400))):
            raise ValueError('Nonempty prompt/cwd and optional timeout in (0,86400] required')
        fields = prepare_start(agent, model, effort, agy_approval)
        from pathlib import PurePosixPath, PureWindowsPath
        path_type=Path if device=='local' else (PureWindowsPath if remote_shell=='powershell' else PurePosixPath)
        if not path_type(cwd).is_absolute():
            raise ValueError('cwd must be absolute on the execution device; disable shell path conversion')
        task = self.store.create(origin, device, agent, cwd, prompt, write, resource,
                                 device_key=device_identity(device), skills=skills,
                                 initiating_surface='omp-extension' if origin.profile == 'omp-extension' else 'standalone')
        task = self.store.update(task['id'], shell=remote_shell, timeout=timeout,
                                 needs_session=True, acpx_created=False, session_creation_attempted=False, **fields)
        self._dispatch(task)
        return self.status(client, task['id'])

    def status(self, client, task_id):
        origin = self.origin(client)
        task = self.store.get(task_id, origin)
        from recovery import recovery_card
        host = 'omp-extension' if origin.profile == 'omp-extension' else 'standalone'
        return dict(task, recovery=recovery_card(task, self.store.path, host=host))

    def events(self, client, task_id, after=0):
        return self.store.events(task_id, self.origin(client), after)

    def list(self, client):
        return self.store.list(self.origin(client))

    def intervene(self, client, task_id, prompt, *, command_id=None, clear_timeout=False, expected_turn=None):
        task, launch = self.store.intervene(task_id, self.origin(client), prompt, command_id,
                                           clear_timeout=clear_timeout, expected_turn=expected_turn)
        if launch:
            self._dispatch(task)
        return self.status(client, task_id)

    def _advance(self, client, task_id):
        task, launch = self.store.advance_intervention(task_id, self.origin(client))
        if launch or (task['state'] == 'queued' and task.get('launch_turn') != task['turn']):
            try:
                self._dispatch(task)
            except ValueError:
                pass  # Another bounded poll may have claimed this exact turn.
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

    def reply(self, client, task_id, question_id, prompt, *, clear_timeout=False, expected_turn=None, command_id=None):
        task = self.store.reply(task_id, self.origin(client), question_id, prompt,
                                clear_timeout=clear_timeout, require_question=True,
                                expected_turn=expected_turn, command_id=command_id)
        if task['state'] == 'queued' and task.get('launch_turn') != task['turn']:
            self._dispatch(task)
        return self.status(client, task_id)

    def cancel(self, client, task_id):
        self.store.request_cancel(task_id, self.origin(client))
        return self.status(client, task_id)

    def close(self, client, task_id):
        from release import close_task
        origin = self.origin(client)
        receipt = close_task(self.store, origin, task_id)
        if receipt['state'] == 'released':
            from skills import cleanup
            cleanup(self.store, self.store.get(task_id, origin))
        from recovery import public_close_receipt
        return dict(self.status(client, task_id), close_receipt=public_close_receipt(receipt))

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
    start.add_argument('--agent', choices=list(BACKENDS), default='codex')
    start.add_argument('--model', help='Exact advertised ACP model ID; required for AGY')
    start.add_argument('--effort', help='Exact advertised thought_level value (AGY)')
    start.add_argument('--agy-approval', choices=['approve-all', 'approve-reads'],
                       help='AGY only: new tasks default to approve-all; approve-reads denies shell/write requests')
    start.add_argument('--cwd', required=True)
    start.add_argument('--remote-shell', choices=['posix', 'powershell'], default='posix')
    start.add_argument('--write', action='store_true')
    start.add_argument('--resource', default='')
    start.add_argument('--timeout', type=lambda value: None if value == 'none' else float(value),
                       help='Uncommon explicit hard execution limit in seconds, not a review reminder; omitted or none means no execution deadline')
    start.add_argument('--prompt', required=True)
    start.add_argument('--skill', action='append', default=[], metavar='MODE=PATH', help='Snapshot a coordinator-side skill directory')
    start.add_argument('--target-skill', action='append', default=[], metavar='MODE=PATH', help='Snapshot an existing execution-device skill directory')
    start.add_argument('--skills-json', help='JSON array of selections including optional source provenance')
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
            p.add_argument('--clear-timeout', action='store_true',
                           help='Clear inherited execution limit on the next settled continuation')
            p.add_argument('--expected-turn', type=int, help='Observed turn being answered; rejects stale intent when supplied')
            p.add_argument('--command-id', help='Stable intent ID; reuse unchanged for retry-safe submission')
        if action == 'reply':
            p.add_argument('--question-id', required=True)
    opts = vars(parser.parse_args(argv))
    action, db, client = opts.pop('action'), opts.pop('db'), opts.pop('client')
    polling_token = _polling.set(action == 'wait')
    deadline_token = _wait_deadline.set(
        time.monotonic() + min(5, max(0, opts['timeout'])) if action == 'wait' else None)
    try:
        if action != 'connect' and not client:
            raise ValueError('--client is required for this action')
        c = Collaboration(db)
        if action == 'start':
            skills = json.loads(opts.pop('skills_json') or '[]')
            if not isinstance(skills, list):
                raise ValueError('--skills-json must be an array')
            for key, location in [('skill', 'origin'), ('target_skill', 'target')]:
                for value in opts.pop(key):
                    mode, sep, path = value.partition('=')
                    if not sep:
                        raise ValueError('Use MODE=PATH for skill selections')
                    skills.append(dict(mode=mode, path=path, location=location))
            if skills:
                opts['skills'] = skills
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
