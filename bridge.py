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

    def create(self, origin, device, agent, cwd, prompt, write, resource='', device_key=None, skills=None, *, initiating_surface=None):
        task_id = uuid.uuid4().hex[:16]
        data = dict(id=task_id, session='call-'+task_id, origin=dataclasses.asdict(origin),
                    device=device, device_key=device_key or device, agent=agent, cwd=cwd, prompt=prompt, write=write,
                    resource=resource, state='queued', turn=1, process_id='', question_id='', questions=[], answers={},
                    controller=dataclasses.asdict(origin), control_epoch=1, initiating_surface=initiating_surface or 'unknown')
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
        current = json.loads(db.execute('SELECT data FROM tasks WHERE id=?', (task['id'],)).fetchone()[0])
        if current['state'] == 'closed' or current.get('release_epoch'):
            raise ValueError('Task is permanently fenced')
        if current['turn'] != task['turn']:
            task.pop('close_diagnostic',None)
        if task.get('agy_swarm') == 'native' and current['turn'] != task['turn']:
            if current.get('native_cleanup', {}).get('session_unloaded') is True:
                task['native_predecessor_session_id'] = current.get('native_session_id')
                task.pop('native_session_id', None)
                task['needs_session'] = True
            for key in ('native_result', 'native_cleanup', 'native_cancel', 'native_snapshot',
                        'native_workers', 'native_roster', 'native_prompt_submitted', 'native_phase', 'native_observation',
                        'native_input', 'interaction_seq', 'native_interaction_next', 'native_cancel_id',
                        'native_unload_observation', 'native_owner_exit', 'native_cancel_cleanup'):
                task.pop(key, None)
        db.execute('UPDATE tasks SET data=? WHERE id=?', (json.dumps(task), task['id']))

    def update(self, task_id, **changes):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = json.loads(db.execute('SELECT data FROM tasks WHERE id=?', (task_id,)).fetchone()[0])
            task.update(changes)
            self._save(db, task)
        return task

    def claim_launch(self, task_id, turn, epoch=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = json.loads(db.execute('SELECT data FROM tasks WHERE id=?', (task_id,)).fetchone()[0])
            if (task['turn'] != turn or task['state'] != 'queued'
                    or (epoch is not None and task.get('control_epoch', 1) != epoch)
                    or task.get('launch_turn') == turn):
                raise ValueError('Turn already launched or not queued')
            task.update(launch_turn=turn, launch_id=uuid.uuid4().hex)
            self._save(db, task)
        return task

    def finish_launch(self, task_id, turn, launch_id, process_id=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = json.loads(db.execute('SELECT data FROM tasks WHERE id=?', (task_id,)).fetchone()[0])
            if (task['state'] != 'closed' and not task.get('release_epoch')
                    and task['turn'] == turn and task.get('launch_id') == launch_id):
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
            if task['state'] == 'closed' or task.get('release_epoch'):
                return False
            if metadata.get('turn',task['turn']) != task['turn']:
                return False
            if metadata.get('quiet_notice') and task['state'] != 'running':
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
                        (task_id, task['turn'], label[1]))
                        if json.loads(row[0]).get('interaction_seq', 0) == metadata.get('interaction_seq', 0)]
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

    @staticmethod
    def _native_binding(task):
        return dict(turn=task['turn'], launch_id=task.get('launch_id'), epoch=task.get('control_epoch', 1))

    def dialogue(self, task_id, origin, prompt, *, expected_turn, command_id, question_id=None):
        if type(expected_turn) is not int or expected_turn < 1 or not isinstance(command_id, str) or not command_id.strip():
            raise ValueError('dialogue requires expected_turn and stable command_id')
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError('dialogue requires nonempty prompt')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = self._owned(db, task_id, origin)
            # Dialogue never admits a new controller, including ended-owner transfer.
            controller = Origin(**task.get('controller', task['origin'])).key()
            same = getattr(self, '_same_principal', self._allows)
            if not same(controller, origin):
                raise PermissionError('Dialogue belongs to the current controller')
            intent = dict(prompt=prompt, expected_turn=expected_turn, question_id=question_id)
            commands = task.setdefault('commands', {})
            old = commands.get(command_id)
            if old is not None:
                if old.get('kind') != 'dialogue' or old.get('intent') != intent:
                    raise ValueError('command_id already used with different parameters')
                return task
            self._check_intent(task, expected_turn)
            cap = task.get('native_input', {})
            binding = self._native_binding(task)
            if (task.get('release_epoch') or task['state'] != 'running'):
                raise ValueError('Native dialogue unavailable or fenced; retain HOLD')
            if (task['agent'] != 'agy' or task.get('agy_swarm') != 'native'
                    or not task.get('runner_pid') or not binding['launch_id']
                    or not cap.get('accepting') or any(cap.get(k) != v for k,v in binding.items())):
                raise ValueError('Native dialogue unsupported: no live bound owner input capability')
            question = None
            if question_id is not None:
                if question_id not in task.get('questions', []):
                    raise ValueError('dialogue requires an unanswered question_id')
                row = db.execute('SELECT data FROM events WHERE task=? AND token=?', (task_id, question_id)).fetchone()
                event = json.loads(row[0]) if row else {}
                if event.get('kind') != 'question' or event.get('turn', task['turn']) != task['turn']:
                    raise ValueError('Original question event unavailable; do not guess answer binding')
                question = dict(id=question_id, label=event.get('question_label'),
                    interaction_seq=event.get('interaction_seq', 0), text=event['text'],
                    sha256=event.get('question_sha256'), length=event.get('question_length'))
                if any(c.get('kind') == 'dialogue' and c.get('question_id') == question_id
                       and c.get('status') in {'accepted', 'claimed', 'send_started', 'returned'} for c in commands.values()):
                    raise ValueError('Question already has an admitted answer')
            seq = task.get('native_interaction_next', task.get('interaction_seq', 0))+1
            task['native_interaction_next'] = seq
            commands[command_id] = dict(kind='dialogue', command_id=command_id, intent=intent,
                prompt=prompt, question_id=question_id, question=question, status='accepted', interaction_seq=seq, **binding)
            self._save(db, task)
        return task

    def _native_owned(self, db, task_id, turn, launch_id, epoch):
        task = json.loads(db.execute('SELECT data FROM tasks WHERE id=?', (task_id,)).fetchone()[0])
        if (self._native_binding(task) != dict(turn=turn, launch_id=launch_id, epoch=epoch)
                or task.get('release_epoch') or task['state'] not in {'running', 'cancelling'}):
            raise ValueError('Native owner identity is stale or fenced')
        return task

    def native_prompt_begin(self, task_id, turn, launch_id, epoch):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task=self._native_owned(db,task_id,turn,launch_id,epoch)
            if task['state']!='running': return False
            task.update(native_phase='foreground',native_prompt_submitted=True)
            self._save(db,task)
            return True

    def native_cleanup_owner(self, task_id, turn, launch_id, epoch):
        token = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = self._native_owned(db, task_id, turn, launch_id, epoch)
            db.execute('CREATE TABLE IF NOT EXISTS native_cleanup_receipts (task TEXT, turn INTEGER, launch TEXT, epoch INTEGER, token_hash TEXT, receipt TEXT, PRIMARY KEY(task,turn,launch,epoch))')
            db.execute('INSERT INTO native_cleanup_receipts VALUES (?,?,?,?,?,?)',
                (task_id, turn, launch_id, epoch, hashlib.sha256(token.encode()).hexdigest(), '{}'))
        return token

    def native_cancel_seal(self, task_id, turn, launch_id, epoch):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = self._native_owned(db, task_id, turn, launch_id, epoch)
            if task['state'] != 'cancelling':
                raise ValueError('Native cancellation not requested')
            task.setdefault('native_input', {})['accepting'] = False
            task.setdefault('native_cancel_id', uuid.uuid4().hex)
            for entry in task.get('commands', {}).values():
                if entry.get('kind')=='dialogue' and entry.get('status')=='accepted':
                    entry['status']='not_sent'
            self._save(db, task)
        return task

    def native_cancel_cleanup_state(self, task_id, turn, launch_id, epoch, cancel_id, status, workers):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task=self._native_owned(db,task_id,turn,launch_id,epoch)
            if task['state']!='cancelling' or task.get('native_cancel_id')!=cancel_id:
                raise ValueError('Native cleanup cancellation identity changed')
            previous=task.get('native_cancel_cleanup')
            if status=='send_started':
                if previous: raise ValueError('Native cancellation cleanup already attempted; no replay')
            elif (status not in {'returned','unknown'} or not previous or previous['status']!='send_started'
                    or previous['cancel_id']!=cancel_id or previous['workers']!=workers):
                raise ValueError('Native cleanup send transition invalid')
            task['native_cancel_cleanup']=dict(task=task_id,session_id=task.get('native_session_id'),
                turn=turn,launch_id=launch_id,epoch=epoch,cancel_id=cancel_id,status=status,workers=workers)
            self._save(db,task)

    def native_cleanup_commit(self, task_id, turn, launch_id, epoch, token, receipt):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = self._native_owned(db, task_id, turn, launch_id, epoch)
            row = db.execute('SELECT token_hash,receipt FROM native_cleanup_receipts WHERE task=? AND turn=? AND launch=? AND epoch=?',
                (task_id,turn,launch_id,epoch)).fetchone()
            if (not row or row['token_hash'] != hashlib.sha256(token.encode()).hexdigest() or row['receipt'] != '{}'
                    or any(receipt.get(k)!=v for k,v in dict(task=task_id,turn=turn,launch_id=launch_id,epoch=epoch,
                        session_id=task.get('native_session_id')).items()) or receipt.get('session_unloaded') is not True):
                raise ValueError('Native cleanup owner evidence mismatch')
            if receipt.get('kind')=='verified-cancellation' and (task['state']!='cancelling'
                    or task.get('native_cancel_id')!=receipt.get('cancel_id')):
                raise ValueError('Native cleanup cancel intent mismatch')
            task['native_cleanup'] = receipt
            db.execute('UPDATE native_cleanup_receipts SET receipt=? WHERE task=? AND turn=? AND launch=? AND epoch=?',
                (json.dumps(receipt),task_id,turn,launch_id,epoch))
            self._save(db, task)
            if receipt.get('kind')=='verified-cancellation':
                task.setdefault('native_input',{})['accepting']=False
                if task.get('native_observation'): task['native_observation']['input_available']=False
                task['native_phase']='cancelled'
            if receipt.get('kind')=='verified-cancellation':
                self._finish_turn(db,task,'cancelled',0,'cancelled',
                    'Cancellation cleanup verified; business result not claimed')

    def native_owner_diagnostics(self, task_id, turn, launch_id, epoch, **diagnostic):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task=json.loads(db.execute('SELECT data FROM tasks WHERE id=?',(task_id,)).fetchone()[0])
            if (self._native_binding(task)!=dict(turn=turn,launch_id=launch_id,epoch=epoch)
                    or task['state']=='closed' or task.get('release_epoch')): return False
            task.update(diagnostic)
            self._save(db,task)
            return True

    def native_cancel_notice(self, task_id, turn, launch_id, epoch):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='native_cleanup_receipts'").fetchone(): return None
            row=db.execute('SELECT receipt FROM native_cleanup_receipts WHERE task=? AND turn=? AND launch=? AND epoch=?',
                (task_id,turn,launch_id,epoch)).fetchone()
            if not row or json.loads(row[0]).get('kind')!='verified-cancellation': return None
            event_row=db.execute('SELECT rowid,data FROM events WHERE task=? AND token=?',
                (task_id,str(turn)+':settled')).fetchone()
            if not event_row: return None
            event=json.loads(event_row['data'])
            if event.get('kind')!='cancelled': return None
            db.execute('CREATE TABLE IF NOT EXISTS native_cancel_notices (task TEXT, turn INTEGER, launch TEXT, epoch INTEGER, PRIMARY KEY(task,turn,launch,epoch))')
            claimed=db.execute('INSERT OR IGNORE INTO native_cancel_notices VALUES (?,?,?,?)',(task_id,turn,launch_id,epoch))
            if not claimed.rowcount: return None
            return dict(seq=event_row['rowid'],**event)

    @staticmethod
    def native_cleanup_verified(db, task):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='native_cleanup_receipts'").fetchone():
            return None
        row = db.execute('SELECT receipt FROM native_cleanup_receipts WHERE task=? AND turn=? AND launch=? AND epoch=?',
            (task['id'],task['turn'],task.get('launch_id'),task.get('control_epoch',1))).fetchone()
        receipt = json.loads(row[0]) if row else {}
        if (not receipt or receipt != task.get('native_cleanup') or receipt.get('session_id')!=task.get('native_session_id')
                or receipt.get('session_unloaded') is not True):
            return None
        return receipt

    def native_input_open(self, task_id, turn, launch_id, epoch):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = self._native_owned(db, task_id, turn, launch_id, epoch)
            if task['state']=='cancelling':
                return
            if task.get('native_input'):
                raise ValueError('Native input owner already registered; do not replay')
            task.update(native_input=dict(accepting=True, **self._native_binding(task)), interaction_seq=0, native_interaction_next=0)
            self._save(db, task)

    def native_dialogue_claim(self, task_id, turn, launch_id, epoch):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = self._native_owned(db, task_id, turn, launch_id, epoch)
            if task['state'] != 'running' or not task.get('native_input', {}).get('accepting'):
                return None
            commands = [c for c in task.get('commands', {}).values() if c.get('kind') == 'dialogue'
                        and all(c.get(k) == v for k,v in self._native_binding(task).items())]
            if any(c['status'] in {'claimed', 'send_started', 'unknown'} for c in commands):
                return None
            entry = next((c for c in commands if c['status'] == 'accepted'), None)
            if entry is None:
                return None
            entry['status'] = 'claimed'
            task['interaction_seq'] = entry['interaction_seq']
            task.pop('native_result', None)
            self._save(db, task)
            return dict(entry)

    def native_dialogue_state(self, task_id, turn, launch_id, epoch, command_id, status):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = self._native_owned(db, task_id, turn, launch_id, epoch)
            entry = task['commands'][command_id]
            expected = {'send_started':'claimed', 'returned':'send_started', 'cancelled':'send_started'}
            if (status not in expected or entry['status'] != expected[status]
                    or (status == 'send_started' and task['state'] != 'running')
                    or any(entry.get(k) != v for k,v in self._native_binding(task).items())):
                raise ValueError('Native dialogue transition is stale')
            entry['status'] = status
            if status == 'returned' and entry.get('question_id') in task.get('questions', []):
                question = entry['question_id']
                task.setdefault('answers', {})[question] = entry['prompt']
                task['questions'].remove(question)
                task['question_id'] = task['questions'][0] if task['questions'] else ''
            self._save(db, task)

    def native_input_seal(self, task_id, turn, launch_id, epoch, interaction_seq):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = self._native_owned(db, task_id, turn, launch_id, epoch)
            if (task['state'] != 'running' or task.get('interaction_seq', 0) != interaction_seq
                    or task.get('questions') or any(c.get('kind') == 'dialogue'
                        and c.get('turn') == turn and c['status'] in {'accepted','claimed','send_started','unknown'}
                        for c in task.get('commands', {}).values())):
                return False
            task['native_input']['accepting'] = False
            task['native_input']['settling'] = True
            self._save(db, task)
            return True

    def native_input_end(self, task_id, turn, launch_id, epoch):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = self._native_owned(db, task_id, turn, launch_id, epoch)
            task.setdefault('native_input', {})['accepting'] = False
            if task.get('native_observation'):
                task['native_observation']['input_available'] = False
            for entry in task.get('commands', {}).values():
                if entry.get('kind') == 'dialogue' and all(entry.get(k) == v for k,v in self._native_binding(task).items()):
                    if entry['status'] in {'claimed','send_started'}:
                        entry['status'] = 'unknown'
                    elif entry['status'] == 'accepted':
                        entry['status'] = 'not_sent'
            self._save(db, task)

    def reply(self, task_id, origin, question_id, prompt, *, clear_timeout=False, require_question=False,
              expected_turn=None, command_id=None):
        if type(clear_timeout) is not bool:
            raise ValueError('clear_timeout must be boolean')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = self._owned(db, task_id, origin)
            replay = task.get('reply_commands', {}).get(command_id) if command_id else None
            intent = dict(prompt=prompt, question_id=question_id, expected_turn=expected_turn,
                          clear_timeout=clear_timeout)
            if replay is not None:
                if replay != intent:
                    raise ValueError('command_id already used with different parameters')
                return task
            self._check_intent(task, expected_turn)
            self._admit_controller(task, origin, expected_turn, command_id)
            if command_id:
                task.setdefault('reply_commands', {})[command_id] = intent
            if (task['state'] not in {'waiting_reply','completed'}
                    or task.get('intervention', {}).get('status') == 'pending'):
                raise ValueError('Previous turn is not settled or intervention is pending')
            questions = task.get('questions', [])
            if not questions and (task.get('control_epoch', 1) > 1 or task.get('continuation_version', 1) >= 2) and (expected_turn is None or not command_id):
                raise ValueError('Versioned continuation requires expected_turn and command_id')
            if require_question and (not question_id or question_id not in questions or not prompt.strip()):
                raise ValueError('Reply requires an unanswered question_id in a settled turn')
            if questions and question_id not in questions:
                raise ValueError('Reply must name the pending question_id')
            if clear_timeout:
                task['clear_timeout_pending'] = True
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
            if task.pop('clear_timeout_pending', False):
                task['timeout'] = None
            self._check_writers(db, task)
            self._save(db, task)
        return task

    def _allows(self, owner, origin):
        return owner == origin.key()

    @staticmethod
    def _check_intent(task, expected_turn):
        if expected_turn is not None and (type(expected_turn) is not int or expected_turn != task['turn']):
            raise ValueError('Observed turn is stale')

    def _admit_controller(self, task, origin, expected_turn, command_id):
        if not self._allows(Origin(**task.get('controller', task['origin'])).key(), origin):
            raise PermissionError('Task belongs to another controller')

    def _owned(self,db,task_id,origin):
        row=db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
        if not row:
            raise PermissionError('Task belongs to another Hermes session/topic')
        task = json.loads(row['data'])
        if not self._allows(Origin(**task.get('controller', task['origin'])).key(), origin):
            raise PermissionError('Task belongs to another controller')
        return task

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

    def intervene(self, task_id, origin, prompt, command_id=None, *, legacy_hermes=False, clear_timeout=False,
                  expected_turn=None):
        if type(clear_timeout) is not bool:
            raise ValueError('clear_timeout must be boolean')
        if not prompt.strip():
            raise ValueError('Nonempty intervention required')
        command_id = command_id or str(uuid.uuid4())
        launch = False
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = self._owned(db, task_id, origin)
            commands = task.setdefault('commands', {})
            if command_id in commands:
                if (commands[command_id]['prompt'] != prompt or
                        commands[command_id].get('clear_timeout', False) != clear_timeout
                        or commands[command_id].get('expected_turn') != expected_turn):
                    raise ValueError('command_id already used with different parameters')
                return task, False
            if task.get('agy_swarm')=='native' and task['state']=='running' and task.get('native_input',{}).get('settling'):
                raise ValueError('Native owner is settling; inspect final outcome before intervention')
            self._check_intent(task, expected_turn)
            self._admit_controller(task, origin, expected_turn, command_id)
            if task.get('intervention', {}).get('status') == 'pending':
                raise ValueError('Another intervention is pending; wait or cancel it first')
            if task['state'] not in {'queued', 'running', 'cancelling', 'cancelled', 'completed', 'waiting_reply'}:
                raise ValueError('Task fate is not settled; intervention refused')
            if task['state'] == 'queued' and clear_timeout:
                # A queued turn may already have a native process with a captured deadline.
                raise ValueError('Cannot clear timeout while queued; wait for settlement or cancel before continuing')
            if legacy_hermes:
                self._prepare_legacy_hermes_session(db, task)
            if task['state'] == 'queued':
                self._target_commands(task, 'superseded')
            entry = dict(command_id=command_id, prompt=prompt, source_turn=task['turn'],
                         status='pending', clear_timeout=clear_timeout, expected_turn=expected_turn)
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
        task.pop('clear_timeout_pending', None)
        if entry.get('clear_timeout', False):
            task['timeout'] = None
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
            if task.get('agy_swarm')=='native' and task['state']=='running' and task.get('native_input',{}).get('settling'):
                raise ValueError('Native owner is settling; inspect final outcome before cancellation')
            entry = task.get('intervention', {})
            if entry.get('status') in {'pending', 'queued'}:
                entry['status'] = 'withdrawn'
                task['commands'][entry['command_id']] = entry
            task.pop('clear_timeout_pending', None)
            if task['state']=='queued':task.update(state='cancelled',prompt='')
            elif task['state']=='running':task['state']='cancelling'
            if task.get('agy_swarm')=='native' and task['state']=='cancelling':
                task.setdefault('native_input', {})['accepting']=False
                task.setdefault('native_cancel_id',uuid.uuid4().hex)
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
            task.pop('clear_timeout_pending', None)
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
            return self._finish_turn(db,task,state,rc,stop_reason,note,text=text,answer_tail=answer_tail)

    def _finish_turn(self,db,task,state,rc,stop_reason,note=None,*,text=None,answer_tail=''):
        task_id,turn=task['id'],task['turn']
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
        task.pop('settlement_note',None)
        if note:task['settlement_note']=note
        event = dict(token=str(turn)+':settled', kind=state,
                     text=(text if text is not None else note or 'Prompt exited '+str(rc))[:8000],
                     turn=turn, question_ids=task.get('questions', []), answer_tail=answer_tail)
        try:
            from .recovery import recovery_card
        except ImportError:
            from recovery import recovery_card
        event['recovery'] = recovery_card(task, self.path)
        if task.get('intervention', {}).get('status') == 'pending':
            event['next_action'] = 'Call status/events (Hermes) or wait (CLI) to resume the authorized intervention'
        if state == 'unknown' and task['agent'] in {'agy','omp'}:
            event.update(attention_required=True, notification_scope='originating-session',
                         next_action=task['agent'].upper()+' outcome unknown. Inspect this exact task and artifacts now; preserve HOLD. '
                                     'Do not replay automatically; reconcile before any authorized continuation.',
                         delivery='event-generated; host receipt and consumption unverified')
        inserted = db.execute('INSERT INTO events VALUES (?,?,?)',(task_id,event['token'],json.dumps(event)))
        event['seq'] = inserted.lastrowid
        event['recovery']['seq'] = inserted.lastrowid
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
        if origin is not None and not self._allows(row['owner'], origin):
            raise PermissionError('Task belongs to another Hermes session/topic')
        return json.loads(row['data'])
