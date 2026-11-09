"""Small durable task directory; ACPX and Hermes remain the execution owners."""
from __future__ import annotations
import dataclasses
import hashlib
import json
import os
import posixpath
import re
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

    def create(self, origin, device, agent, cwd, prompt, write, resource='', device_key=None, skills=None):
        task_id = uuid.uuid4().hex[:16]
        data = dict(id=task_id, session='call-'+task_id, origin=dataclasses.asdict(origin),
                    device=device, device_key=device_key or device, agent=agent, cwd=cwd, prompt=prompt, write=write,
                    resource=resource, state='queued', turn=1, process_id='', question_id='', questions=[], answers={})
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._check_writers(db, data)
            if skills is not None:
                try:
                    from .skills import snapshot
                except ImportError:
                    from skills import snapshot
                data['skills'] = snapshot(skills, self.path, task_id)
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
    def _target_commands(task, status):
        # JSON reloads separate intervention from commands; update both explicitly.
        for command_id, entry in task.get('commands', {}).items():
            if (entry.get('target_turn') == task['turn']
                    and entry.get('status') in {'queued', 'target_started'}):
                entry['status'] = status
                if task.get('intervention', {}).get('command_id') == command_id:
                    task['intervention']['status'] = status

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

    def claim_launch(self, task_id, turn):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = json.loads(db.execute('SELECT data FROM tasks WHERE id=?', (task_id,)).fetchone()[0])
            if (task['turn'] != turn or task['state'] != 'queued'
                    or task.get('launch_turn') == turn):
                raise ValueError('Turn already launched or not queued')
            task.update(launch_turn=turn, launch_id=uuid.uuid4().hex)
            self._save(db, task)
        return task

    def finish_launch(self, task_id, turn, launch_id, process_id=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = json.loads(db.execute('SELECT data FROM tasks WHERE id=?', (task_id,)).fetchone()[0])
            if task['turn'] == turn and task.get('launch_id') == launch_id:
                if process_id is not None:
                    task['process_id'] = process_id
                elif task['state'] in {'queued', 'running', 'cancelling'}:
                    # A missing dispatch receipt is uncertain, never permission to replay.
                    self._target_commands(task, 'target_unknown')
                    task['state'] = 'unknown'
                self._save(db, task)
        return task

    def event(self, task_id, token, kind, text='', **metadata):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM tasks WHERE id=?', (task_id,)).fetchone()
            task = json.loads(row[0])
            if metadata.get('turn',task['turn']) != task['turn']:
                return False
            if db.execute('SELECT 1 FROM events WHERE task=? AND token=?', (task_id, token)).fetchone():
                return False
            if kind == 'question':
                complete = getattr(text, 'identity_complete', True)
                if not complete:
                    metadata['question_note'] = 'Question exceeded line buffer; complete identity unavailable; answer each token separately.'
                label = re.match(r'^(Q[1-9][0-9]*) \S', text) if complete else None
                if label:
                    # Logical identity is explicit and turn-scoped, never inferred from wording.
                    fingerprint = hashlib.sha256(text.encode('utf-8')).hexdigest()
                    metadata.update(turn=task['turn'], question_label=label[1],
                                    question_sha256=fingerprint, question_length=len(text))
                    prior = [json.loads(row[0]) for row in db.execute(
                        "SELECT data FROM events WHERE task=? AND json_extract(data,'$.turn')=? "
                        "AND json_extract(data,'$.question_label')=? AND json_extract(data,'$.kind')='question'",
                        (task_id, task['turn'], label[1]))]
                    if any(event.get('question_sha256') == fingerprint and
                           event.get('question_length') == len(text) for event in prior):
                        return False
                    if prior:
                        metadata['question_note'] = 'Question label reused with different or unverified body; answer each token separately.'
            # Dedupe happens before mutating task state.
            event = dict(token=token, kind=kind, text=text[:8000], **metadata)
            inserted = db.execute('INSERT OR IGNORE INTO events VALUES (?,?,?)', (task_id, token, json.dumps(event)))
            if not inserted.rowcount:
                return False
            if kind == 'skill':
                parts = text.split(None, 2)
                ids = {s['id'] for s in task.get('skills', [])}
                if (len(parts) >= 2 and parts[0] in ids and
                        parts[1] in {'read', 'adopted', 'not_adopted', 'not_applicable', 'unavailable'}):
                    task.setdefault('skill_reports', {}).setdefault(str(task['turn']), {})[parts[0]] = dict(
                        status=parts[1], reason=parts[2][:4000] if len(parts) == 3 else '', token=token)
                    self._save(db, task)
            if kind == 'question':
                task['question_id'] = token
                task.setdefault('questions', []).append(token)
                if task['state'] == 'queued':
                    task['state'] = 'waiting_reply'
                self._save(db, task)
        return inserted.lastrowid

    def reply(self, task_id, origin, question_id, prompt):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row or row['owner'] != origin.key():
                raise PermissionError('Task belongs to another Hermes session/topic')
            task = json.loads(row['data'])
            if (task['state'] not in {'waiting_reply','completed'}
                    or task.get('intervention', {}).get('status') == 'pending'):
                raise ValueError('Previous turn is not settled or intervention is pending')
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

    @staticmethod
    def _prepare_legacy_hermes_session(db, task):
        fields = ('needs_session', 'acpx_created', 'session_creation_attempted')
        if any(key in task for key in fields) or task.get('acpx_record_id'):
            return
        # Only this runner-authored first-turn event proves creation was never
        # attempted. Missing fields, a cancelled state or no process ID do not.
        proven = False
        if task['turn'] == 1 and task['state'] == 'cancelled':
            for row in db.execute('SELECT data FROM events WHERE task=?', (task['id'],)):
                event = json.loads(row[0])
                if (event.get('turn') == 1 and event.get('kind') == 'cancelled'
                        and event.get('text') == 'Cancelled before provider launch'):
                    proven = True
                    break
        if not proven:
            raise ValueError('Legacy session creation is unverified; intervention refused. Use a new start after reconciling the old task.')
        task.update(needs_session=True, acpx_created=False, session_creation_attempted=False)

    def intervene(self, task_id, origin, prompt, command_id=None, *, legacy_hermes=False):
        if not prompt.strip():
            raise ValueError('Nonempty intervention required')
        command_id = command_id or str(uuid.uuid4())
        launch = False
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = self._owned(db, task_id, origin)
            commands = task.setdefault('commands', {})
            if command_id in commands:
                if commands[command_id]['prompt'] != prompt:
                    raise ValueError('command_id already used for a different prompt')
                return task, False
            if task.get('intervention', {}).get('status') == 'pending':
                raise ValueError('Another intervention is pending; wait or cancel it first')
            if task['state'] not in {'queued', 'running', 'cancelling', 'cancelled', 'completed', 'waiting_reply'}:
                raise ValueError('Task fate is not settled; intervention refused')
            if legacy_hermes:
                self._prepare_legacy_hermes_session(db, task)
            if task['state'] == 'queued':
                self._target_commands(task, 'superseded')
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
                self._resume_intervention(db, task, entry)
                launch = True
            self._save(db, task)
        return task, launch

    def _resume_intervention(self, db, task, entry):
        task.update(turn=task['turn']+1, state='queued', prompt=entry['prompt'],
                    process_id='', runner_pid=0, question_id='', questions=[], answers={})
        entry.update(status='queued', target_turn=task['turn'])
        task['commands'][entry['command_id']] = entry
        self._check_writers(db, task)

    def advance_intervention(self, task_id, origin):
        launch = False
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = self._owned(db, task_id, origin)
            entry = task.get('intervention', {})
            if (entry.get('status') == 'pending' and entry['source_turn'] == task['turn']
                    and task['state'] in {'cancelled', 'completed', 'waiting_reply'}):
                self._resume_intervention(db, task, entry)
                self._save(db, task)
                launch = True
        return task, launch

    def request_cancel(self,task_id,origin):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task=self._owned(db,task_id,origin)
            entry = task.get('intervention', {})
            if entry.get('status') in {'pending', 'queued'}:
                entry['status'] = 'withdrawn'
                task['commands'][entry['command_id']] = entry
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
            entry = task.get('intervention', {})
            if entry.get('status') == 'pending':
                entry['status'] = 'withdrawn'
                task['commands'][entry['command_id']] = entry
            self._target_commands(task, 'target_unknown')
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

    def finish_turn(self,task_id,turn,state,rc,stop_reason,note=None,*,text=None,answer_tail=''):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task=json.loads(db.execute('SELECT data FROM tasks WHERE id=?',(task_id,)).fetchone()[0])
            if task['turn']!=turn or task['state'] not in {'running','cancelling'}:return False
            if state=='completed' and task['question_id']:state='waiting_reply'
            task.update(state=state,exit_code=rc,stop_reason=stop_reason,runner_pid=0,prompt='')
            status = ('target_completed' if state in {'completed', 'waiting_reply'}
                      and rc == 0 and stop_reason == 'end_turn' else
                      'target_cancelled' if state == 'cancelled' else 'target_unknown')
            self._target_commands(task, status)
            if task.get('skills'):
                reports = task.get('skill_reports', {}).get(str(turn), {})
                task['skill_report_missing'] = [s['id'] for s in task['skills']
                                                if s['mode'] == 'required' and s['id'] not in reports]
                task['skill_verification'] = 'unverified' if task['skill_report_missing'] else 'reported'
            # Per-turn: never carry an earlier turn's diagnosis forward.
            task.pop('settlement_note',None)
            if note:task['settlement_note']=note
            # Persist before another caller can advance the turn or close the task.
            event = dict(token=str(turn)+':settled', kind=state,
                         text=(text if text is not None else note or 'Prompt exited '+str(rc))[:8000],
                         turn=turn, question_ids=task.get('questions', []), answer_tail=answer_tail)
            if task.get('intervention', {}).get('status') == 'pending':
                event['next_action'] = 'Call status/events (Hermes) or wait (CLI) to resume the authorized intervention'
            inserted = db.execute('INSERT INTO events VALUES (?,?,?)',
                                  (task_id, event['token'], json.dumps(event)))
            event['seq'] = inserted.lastrowid
            self._save(db,task)
        return event

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
