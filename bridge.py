"""Small durable task directory; ACPX and Hermes remain the execution owners."""
from __future__ import annotations
import dataclasses
import json
import os
import posixpath
import sqlite3
import uuid
from pathlib import Path
from contextlib import contextmanager


@dataclasses.dataclass(frozen=True)
class Origin:
    profile: str
    session_id: str
    session_key: str
    platform: str = 'cli'
    chat_id: str = ''
    thread_id: str = ''

    def key(self):
        return json.dumps(dataclasses.asdict(self), sort_keys=True)


def capture_origin(get_env, profile):
    sid = get_env('HERMES_SESSION_ID', '')
    key = get_env('HERMES_SESSION_KEY', '') or sid
    if not sid or not key:
        raise ValueError('No host session identity; use a live Hermes session')
    return Origin(profile, sid, key, get_env('HERMES_SESSION_PLATFORM','') or 'cli',
                  get_env('HERMES_SESSION_CHAT_ID',''), get_env('HERMES_SESSION_THREAD_ID',''))


class EventDecoder:
    """Only public assistant messages and minimal tool status reach the event log."""
    def __init__(self, shared_request_id=None):
        self.text = ''
        self.pending = ''
        self.stop_reason = None
        self.failed = False
        self.prompt_id = None
        self.read_callbacks = set()
        self.shared_request_id = shared_request_id
        self.shared_result_seen = False

    def _shared_message(self, frame):
        # The facade emits a terminal frame only after the native turn.result settles.
        if frame.get('requestId') != self.shared_request_id or self.shared_result_seen:
            self.failed = True
            return None
        if frame.get('type') == 'callcli.shared.result':
            self.shared_result_seen = True
            pair = (frame.get('status'), frame.get('stopReason'))
            if pair in {('completed', 'end_turn'), ('cancelled', 'cancelled')}:
                self.stop_reason = frame['stopReason']
            else:
                self.failed = True
            return None
        if frame.get('type') != 'callcli.shared.event':
            self.failed = True
            return None
        event = frame.get('event', {})
        if (event.get('type') == 'text_delta' and event.get('stream') == 'output'
                and event.get('tag') == 'agent_message_chunk' and isinstance(event.get('text'), str)):
            update = {'sessionUpdate': 'agent_message_chunk',
                      'content': {'type': 'text', 'text': event['text']}}
        elif event.get('type') == 'tool_call':
            update = {'sessionUpdate': 'tool_call_update',
                      'toolCallId': event.get('toolCallId', ''), 'status': event.get('status', '')}
        else:
            return None
        return {'params': {'update': update}}

    def feed(self, event):
        if self.shared_request_id is not None:
            message = self._shared_message(event)
            if message is None:
                return []
        else:
            message = event.get('message', event)
        self._legacy_rpc(message)
        update = message.get('params', {}).get('update', {})
        kind = update.get('sessionUpdate')
        if kind in {'tool_call','tool_call_update'}:
            # Text blocks often omit a trailing newline. A tool boundary ends
            # the preceding public block; publish it before the tool runs.
            out = self.finish()
            return out + [('tool', str(update.get('toolCallId',''))+' '+str(update.get('status','')))]
        if kind != 'agent_message_chunk':
            return []
        content = update.get('content', {})
        chunk = content.get('text','') if isinstance(content,dict) else ''
        self.text = (self.text + chunk)[-64000:]
        self.pending = (self.pending + chunk)[-64000:]
        out = []
        while '\n' in self.pending:
            line, self.pending = self.pending.split('\n',1)
            out.extend(self.line(line))
        return out

    def _legacy_rpc(self, message):
        rpc_id, method = message.get('id'), message.get('method')
        if method == 'session/prompt':
            self.prompt_id = rpc_id
        if method and rpc_id is not None:
            self.read_callbacks.discard(rpc_id)
            if method == 'fs/read_text_file':
                self.read_callbacks.add(rpc_id)
        if message.get('error'):
            # A failed read callback may be recovered by the agent. Only ignore
            # a correlated read response distinct from the active prompt ID;
            # unclassified errors and bidirectional ID collisions fail closed.
            recovered_read = (self.prompt_id is not None and rpc_id != self.prompt_id
                              and rpc_id in self.read_callbacks)
            if not recovered_read:
                self.failed = True
        if 'error' in message or 'result' in message:
            self.read_callbacks.discard(rpc_id)
        result = message.get('result', {})
        if isinstance(result,dict) and 'stopReason' in result:
            self.stop_reason = result['stopReason']

    @staticmethod
    def line(line):
        line = line.strip()
        for prefix,kind in [('CALLCLI_STAGE ','stage'),('CALLCLI_QUESTION ','question')]:
            if line.startswith(prefix):
                return [(kind,line[len(prefix):][:8000])]
        return [('answer',line[:8000])] if line else []

    def finish(self):
        out = self.line(self.pending)
        self.pending = ''
        return out


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, owner TEXT, data TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS events (task TEXT, token TEXT, data TEXT, PRIMARY KEY(task,token))')
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, origin, device, agent, cwd, prompt, write, resource='', device_key=None):
        task_id = uuid.uuid4().hex[:16]
        data = dict(id=task_id, session='call-'+task_id, origin=dataclasses.asdict(origin),
                    device=device, device_key=device_key or device, agent=agent, cwd=cwd, prompt=prompt, write=write,
                    resource=resource, state='queued', turn=1, process_id='', question_id='', questions=[], answers={})
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._check_writers(db, data)
            db.execute('INSERT INTO tasks VALUES (?,?,?)', (task_id, origin.key(), json.dumps(data)))
        return data

    @staticmethod
    def _check_writers(db, task):
        if not task['write']:
            return
        def norm(value):
            return posixpath.normpath(value.replace(chr(92), '/')).rstrip('/').casefold()
        for row in db.execute('SELECT data FROM tasks WHERE id != ?', (task['id'],)):
            other = json.loads(row['data'])
            if not other['write'] or other['state'] not in {'queued','running','waiting_reply','cancelling','closing','unknown'}:
                continue
            shared = task['resource'] and task['resource'] == other['resource']
            left, right = norm(task['cwd']), norm(other['cwd'])
            overlaps = task.get('device_key',task['device']) == other.get('device_key',other['device']) and (left == right or left.startswith(right+'/') or right.startswith(left+'/'))
            if shared or overlaps:
                raise ValueError('Workspace is already owned by task '+other['id'])

    @staticmethod
    def _save(db, task):
        db.execute('UPDATE tasks SET data=? WHERE id=?', (json.dumps(task), task['id']))

    def update(self, task_id, **changes):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = json.loads(db.execute('SELECT data FROM tasks WHERE id=?', (task_id,)).fetchone()[0])
            task.update(changes)
            self._save(db, task)
        return task

    def event(self, task_id, token, kind, text='', **metadata):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM tasks WHERE id=?', (task_id,)).fetchone()
            task = json.loads(row[0])
            if metadata.get('turn',task['turn']) != task['turn']:
                return False
            # Dedupe happens before mutating task state.
            event = dict(token=token, kind=kind, text=text[:8000], **metadata)
            count = db.execute('INSERT OR IGNORE INTO events VALUES (?,?,?)', (task_id, token, json.dumps(event))).rowcount
            if not count:
                return False
            if kind == 'question':
                task['question_id'] = token
                task.setdefault('questions', []).append(token)
                if task['state'] == 'queued':
                    task['state'] = 'waiting_reply'
                self._save(db, task)
        return True

    def reply(self, task_id, origin, question_id, prompt):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row or row['owner'] != origin.key():
                raise PermissionError('Task belongs to another Hermes session/topic')
            task = json.loads(row['data'])
            if task['state'] not in {'waiting_reply','completed'}:
                raise ValueError('Previous turn is not settled')
            questions = task.get('questions', [])
            if questions and question_id not in questions:
                raise ValueError('Reply must name the pending question_id')
            if questions:
                task.setdefault('answers', {})[question_id] = prompt
                questions.remove(question_id)
                if questions:
                    task['question_id'] = questions[0]
                    self._save(db, task)
                    return task
                answers = list(task['answers'].items())
                prompt = answers[0][1] if len(answers)==1 else '\n'.join(k+': '+v for k,v in answers)
            task.update(turn=task['turn']+1, prompt=prompt, state='queued', question_id='', process_id='')
            task.update(questions=[], answers={})
            self._check_writers(db, task)
            self._save(db, task)
        return task

    @staticmethod
    def _owned(db,task_id,origin):
        row=db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
        if not row or row['owner']!=origin.key():
            raise PermissionError('Task belongs to another Hermes session/topic')
        return json.loads(row['data'])

    def request_cancel(self,task_id,origin):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task=self._owned(db,task_id,origin)
            if task['state']=='queued':task.update(state='cancelled',prompt='')
            elif task['state']=='running':task['state']='cancelling'
            self._save(db,task)
        return task

    def begin_close(self,task_id,origin):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task=self._owned(db,task_id,origin)
            if task['state'] not in {'completed','waiting_reply','cancelled','unknown'}:
                raise ValueError('Task is not settled for closing')
            task['state']='closing'
            self._check_writers(db,task)
            self._save(db,task)
        return task

    def finish_close(self,task_id,turn,success):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task=json.loads(db.execute('SELECT data FROM tasks WHERE id=?',(task_id,)).fetchone()[0])
            if task['turn']!=turn or task['state']!='closing':return False
            task.update(state='closed' if success else 'unknown',prompt='')
            self._save(db,task)
        return True

    def finish_turn(self,task_id,turn,state,rc,stop_reason):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task=json.loads(db.execute('SELECT data FROM tasks WHERE id=?',(task_id,)).fetchone()[0])
            if task['turn']!=turn or task['state'] not in {'running','cancelling'}:return False
            if state=='completed' and task['question_id']:state='waiting_reply'
            task.update(state=state,exit_code=rc,stop_reason=stop_reason,runner_pid=0,prompt='')
            self._save(db,task)
        return True

    def events(self, task_id, origin, after=0):
        self.get(task_id, origin)
        with self.connect() as db:
            return [dict(seq=r['rowid'], **json.loads(r['data'])) for r in db.execute(
                'SELECT rowid,data FROM events WHERE task=? AND rowid>? ORDER BY rowid', (task_id, after))]

    def list(self, origin):
        with self.connect() as db:
            return [json.loads(r['data']) for r in db.execute('SELECT data FROM tasks WHERE owner=?', (origin.key(),))]

    def get(self, task_id, origin=None):
        with self.connect() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not row:
            raise KeyError('Unknown call-cli task')
        if origin is not None and row['owner'] != origin.key():
            raise PermissionError('Task belongs to another Hermes session/topic')
        return json.loads(row['data'])
