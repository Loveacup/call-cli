"""Single-policy operator release. No business continuation or owner transfer."""
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import time

try:
    from .bridge import Store, Origin
except ImportError:
    from bridge import Store, Origin


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class _ExistingStore(Store):
    def __init__(self, path):
        # Binding an existing store must not create tables, chmod or open SQLite.
        self.path = path


class BackendFailure(RuntimeError):
    def __init__(self, stage, reason_code):
        self.stage, self.reason_code = stage, reason_code
        super().__init__(reason_code)


class Release:
    @staticmethod
    def install(db):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='release_fences'").fetchone():
            return
        statements = [
            '''CREATE TABLE IF NOT EXISTS release_fences (
                task TEXT PRIMARY KEY, epoch INTEGER NOT NULL, data TEXT NOT NULL,
                receipt TEXT NOT NULL DEFAULT '{}')''',
            '''CREATE TABLE IF NOT EXISTS release_confirmations (
                nonce TEXT PRIMARY KEY, binding TEXT NOT NULL, expires REAL NOT NULL,
                consumed INTEGER NOT NULL DEFAULT 0)''',
            'CREATE TABLE IF NOT EXISTS task_revisions (task TEXT PRIMARY KEY, revision INTEGER NOT NULL)',
        ]
        # Only explicitly enrolled targets are counted. DELETE keeps enrollment,
        # so recreation cannot reset a confirmation's revision to zero.
        for name, operation, identity in (
                ('release_revision', 'UPDATE', 'OLD'),
                ('release_revision_insert', 'INSERT', 'NEW'),
                ('release_revision_delete', 'DELETE', 'OLD')):
            statements.append(f'''CREATE TRIGGER IF NOT EXISTS {name} AFTER {operation} ON tasks
                WHEN EXISTS (SELECT 1 FROM task_revisions WHERE task={identity}.id)
                BEGIN UPDATE task_revisions SET revision=revision+1 WHERE task={identity}.id; END''')
        statements.extend([
                '''CREATE TRIGGER IF NOT EXISTS release_task_update BEFORE UPDATE ON tasks
                WHEN EXISTS (SELECT 1 FROM release_fences WHERE task=OLD.id)
                  AND (NEW.id IS NOT OLD.id OR NEW.owner IS NOT OLD.owner OR NEW.data IS NOT
                       (SELECT data FROM release_fences WHERE task=OLD.id))
                BEGIN SELECT RAISE(ABORT, 'Task release fenced'); END''',
                '''CREATE TRIGGER IF NOT EXISTS release_task_delete BEFORE DELETE ON tasks
                WHEN EXISTS (SELECT 1 FROM release_fences WHERE task=OLD.id)
                BEGIN SELECT RAISE(ABORT, 'Task release fenced'); END''',
                '''CREATE TRIGGER IF NOT EXISTS release_task_insert BEFORE INSERT ON tasks
                WHEN EXISTS (SELECT 1 FROM release_fences WHERE task=NEW.id)
                BEGIN SELECT RAISE(ABORT, 'Task release fenced'); END''',
                '''CREATE TRIGGER IF NOT EXISTS release_event_insert BEFORE INSERT ON events
                WHEN EXISTS (SELECT 1 FROM release_fences WHERE task=NEW.task)
                BEGIN SELECT RAISE(ABORT, 'Task release fenced'); END''',
                '''CREATE TRIGGER IF NOT EXISTS release_event_update BEFORE UPDATE ON events
                WHEN EXISTS (SELECT 1 FROM release_fences WHERE task IN (OLD.task, NEW.task))
                BEGIN SELECT RAISE(ABORT, 'Task release fenced'); END''',
                '''CREATE TRIGGER IF NOT EXISTS release_event_delete BEFORE DELETE ON events
                WHEN EXISTS (SELECT 1 FROM release_fences WHERE task=OLD.task)
                BEGIN SELECT RAISE(ABORT, 'Task release fenced'); END''',
        ])
        for statement in statements:
            db.execute(statement)

    def __init__(self, home, policy, backend, *, clock=time.time):
        self.home = Path(home).resolve()
        self.policy = policy
        self.backend = backend
        self.clock = clock
        path = self.home / 'plugin-data' / 'call-cli' / 'tasks.sqlite3'
        if path.is_symlink() or not path.is_file() or path.resolve() != path:
            raise ValueError('Existing native task database required')
        self.store = _ExistingStore(path)

    @staticmethod
    def load_policy(home):
        path = Path(home) / 'plugin-data' / 'call-cli' / 'release-policy.json'
        flags = os.O_RDONLY | os.O_NOFOLLOW
        with os.fdopen(os.open(path, flags)) as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise PermissionError('Release policy must be a private operator-owned regular file')
            policy = json.load(handle)
        fields = {'task', 'owner', 'turn', 'record', 'acp', 'cwd', 'agent', 'session', 'submission'}
        if not isinstance(policy, dict) or set(policy) != fields or type(policy['turn']) is not int:
            raise ValueError('Invalid single-target release policy')
        if not isinstance(policy['owner'], dict) or policy['owner'].get('profile') != 'default':
            raise ValueError('Release policy requires the default-profile original owner')
        if any(not isinstance(policy[k], str) or not policy[k] for k in fields - {'turn', 'owner'}):
            raise ValueError('Invalid release target')
        return policy

    def _ended_owner(self):
        path = self.home / 'state.db'
        if path.is_symlink():
            raise ValueError('Owner metadata must be local')
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            if db.execute('SELECT version FROM schema_version').fetchall() != [(32,)]:
                raise ValueError('Unsupported owner metadata schema')
            row = db.execute('SELECT end_reason, ended_at, source, profile_name FROM sessions WHERE id=?',
                             (self.policy['owner']['session_id'],)).fetchone()
        if (not row or row[0] != 'agent_close' or not isinstance(row[1], (float, int))
                or not math.isfinite(row[1]) or row[1] <= 0 or row[2] != 'subagent'
                or row[3] not in (None, 'default')):
            raise ValueError('Original owner termination is unverified')
        return dict(reason=row[0], ended_at=row[1], source=row[2])

    def _snapshot(self, db):
        p = self.policy
        row = db.execute('SELECT owner,data FROM tasks WHERE id=?', (p['task'],)).fetchone()
        if not row or json.loads(row['owner']) != p['owner']:
            raise ValueError('Release owner mismatch')
        task = json.loads(row['data'])
        expected = dict(id=p['task'], origin=p['owner'], turn=p['turn'], acpx_record_id=p['record'],
                        cwd=p['cwd'], agent=p['agent'], session=p['session'], device='local', state='completed')
        if any(task.get(k) != v for k, v in expected.items()) or task.get('release_epoch'):
            raise ValueError('Release target mismatch or already fenced')
        revision = db.execute('SELECT revision FROM task_revisions WHERE task=?', (p['task'],)).fetchone()
        return task, dict(home=str(self.home), policy=digest(p), task=digest(task),
                          revision=revision[0] if revision else 0, owner_end=self._ended_owner(),
                          admission='authenticated-host-sensitive-http', operation='release-only')

    def prepare(self):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.install(db)
            fence = db.execute('SELECT receipt FROM release_fences WHERE task=?', (self.policy['task'],)).fetchone()
            if fence:
                return {'receipt': json.loads(fence[0])}
            db.execute('INSERT OR IGNORE INTO task_revisions VALUES (?,0)', (self.policy['task'],))
            task, binding = self._snapshot(db)
            nonce = secrets.token_urlsafe(32)
            expires = self.clock() + 120
            db.execute('INSERT INTO release_confirmations(nonce,binding,expires) VALUES (?,?,?)',
                       (digest(nonce), canonical(binding), expires))
        return dict(nonce=nonce, expires=expires, target={k: task[k] for k in
                    ('id', 'origin', 'turn', 'session', 'acpx_record_id', 'state')}, operation='release-only')

    def receipt(self):
        with closing(sqlite3.connect(self.store.path.as_uri() + '?mode=ro', uri=True)) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='release_fences'").fetchone():
                return {'state': 'not_prepared'}
            row = db.execute('SELECT receipt FROM release_fences WHERE task=?', (self.policy['task'],)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _publish(db, task, receipt):
        # Only the canonical fence projection can cross the SQL trigger. Old Store
        # connections cannot change metadata, owner or state, even with stale code.
        raw = json.dumps(task)
        db.execute('UPDATE release_fences SET data=?,receipt=? WHERE task=?',
                   (raw, canonical(receipt), task['id']))
        db.execute('UPDATE tasks SET data=? WHERE id=?', (raw, task['id']))

    def release(self, nonce):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='release_confirmations'").fetchone():
                raise ValueError('Release is not prepared')
            row = db.execute('SELECT * FROM release_confirmations WHERE nonce=?', (digest(nonce),)).fetchone()
            if not row:
                raise ValueError('Unknown confirmation')
            binding = json.loads(row['binding'])
            if binding.get('home') != str(self.home) or binding.get('policy') != digest(self.policy):
                raise ValueError('Confirmation belongs to another target')
            if row['consumed']:
                return self.receipt()
            if self.clock() >= row['expires']:
                raise ValueError('Confirmation expired')
            task, current = self._snapshot(db)
            if current != binding:
                raise ValueError('Release snapshot changed; prepare again')
            reserving = dict(task, state='unknown')
            self.store._check_writers(db, reserving)
            receipt = dict(operation='release-only', target=self.policy, binding=binding,
                           epoch=1, state='unknown', phase='admitted', admitted_at=self.clock())
            receipt['attempts'] = [dict(number=1, operation='close', stage='close',
                reason_code='in_progress', started_at=self.clock())]
            task.update(state='unknown', release_epoch=1)
            for command in task.get('commands', {}).values():
                if command.get('status') in {'pending', 'queued', 'target_started'}:
                    command.update(status='release-revoked', reason='operator release-only')
            entry = task.get('intervention', {})
            if entry.get('status') in {'pending', 'queued', 'target_started'}:
                entry.update(status='release-revoked', reason='operator release-only')
            db.execute('INSERT INTO release_fences(task,epoch,data,receipt) VALUES (?,1,?,?)',
                       (task['id'], json.dumps(task), canonical(receipt)))
            self._publish(db, task, receipt)
            db.execute('UPDATE release_confirmations SET consumed=1 WHERE nonce=?', (digest(nonce),))
            dispatch_snapshot = canonical(receipt)
        # Unknown is committed before any external effect. Retries never dispatch.
        try:
            proof = self.backend.close(self.policy)
            binding = dict(acpxRecordId=self.policy['record'], acpxSessionId=self.policy['acp'],
                           session=self.policy['session'], agent=self.policy['agent'], cwd=self.policy['cwd'])
            close, status, execution = (proof.get(k, {}) for k in ('close', 'status', 'execution'))
            if (proof.get('verified') is not True or close.get('action') != 'session_closed'
                    or any(close.get(k) != v for k,v in binding.items())
                    or status.get('action') != 'status_snapshot' or status.get('status') != 'no-session'
                    or status.get('binding') != binding
                    or execution.get('scope') != 'acpx-managed-execution'
                    or execution.get('source') != 'supported-record-close-and-session-turn-lock'):
                raise ValueError('Backend close not verified')
            receipt.update(state='released', phase='verified', proof=proof, finished_at=self.clock())
            receipt['attempts'][-1].update(stage='verify', reason_code='verified', finished_at=self.clock())
            task['state'] = 'closed'
        except Exception as exc:
            receipt.update(phase='hold', error=type(exc).__name__, finished_at=self.clock())
            stage = exc.stage if isinstance(exc, BackendFailure) else 'close'
            reason = exc.reason_code if isinstance(exc, BackendFailure) else 'close_uncertain'
            receipt.update(stage=stage, reason_code=reason)
            receipt['attempts'][-1].update(stage=stage, reason_code=reason, finished_at=self.clock())
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            latest = json.loads(db.execute('SELECT receipt FROM release_fences WHERE task=?', (task['id'],)).fetchone()[0])
            if latest.get('state') == 'released' or canonical(latest) != dispatch_snapshot:
                return latest
            self._publish(db, task, receipt)
        return receipt

    def reconcile(self):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT epoch,data,receipt FROM release_fences WHERE task=?',
                             (self.policy['task'],)).fetchone()
            if not row:
                raise ValueError('Reconciliation requires an existing release reservation')
            task, receipt = json.loads(row['data']), json.loads(row['receipt'])
            if receipt.get('target') != self.policy:
                raise ValueError('Reconciliation target mismatch')
            if receipt['state'] == 'released':
                return receipt
            if task['state'] != 'unknown' or task.get('release_epoch') != row['epoch']:
                raise ValueError('Reconciliation reservation mismatch')
            attempt = dict(number=len(receipt.get('attempts', []))+1, operation='probe',
                           stage='probe', reason_code='in_progress', started_at=self.clock())
            receipt.setdefault('attempts', []).append(attempt)
            self._publish(db, task, receipt)
            snapshot = canonical(receipt)
        proof = None
        try:
            proof = self.backend.probe(self.policy)
            binding = dict(acpxRecordId=self.policy['record'], acpxSessionId=self.policy['acp'],
                session=self.policy['session'], agent=self.policy['agent'], cwd=self.policy['cwd'])
            verified = (proof.get('verified') is True and proof.get('binding') == binding
                and proof.get('record', {}).get('closed') is True
                and proof.get('record', {}).get('lastRequestId') == self.policy.get('request_id', self.policy['task']+':'+str(self.policy['turn']))
                and proof.get('status') == 'no-session'
                and proof.get('source') == 'shared-runtime-exact-record-probe'
                and proof.get('arbitraryDetachedDescendantsProven') is False)
            reason = 'verified' if verified else 'proof_incomplete'
            stage = 'verify'
        except Exception as exc:
            verified = False
            reason = exc.reason_code if isinstance(exc, BackendFailure) else 'probe_unavailable'
            stage = exc.stage if isinstance(exc, BackendFailure) else 'probe'
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT epoch,data,receipt FROM release_fences WHERE task=?',
                                 (task['id'],)).fetchone()
            if (current['epoch'] != row['epoch'] or current['receipt'] != snapshot
                    or json.loads(current['data']) != task):
                raise ValueError('Reconciliation settlement conflict')
            attempt.update(stage=stage, reason_code=reason, finished_at=self.clock())
            receipt.update(phase='verified' if verified else 'hold', stage=stage, reason_code=reason)
            if verified:
                receipt.update(state='released', reconciliation=proof, reconciled_at=self.clock())
                task['state'] = 'closed'
            self._publish(db, task, receipt)
        return receipt


def close_task(store, origin, task_id, expected_turn=None, backend=None):
    """Common public close: no uncertain transport replay or text-only release."""
    task = store.get(task_id, origin)
    controller = Origin(**task.get('controller', task['origin'])).key()
    if not store._allows(controller, origin):
        raise PermissionError('Former controller has inspection rights only')
    same = getattr(store, '_same_principal', store._allows)(controller, origin)
    if expected_turn is None and same:
        expected_turn = task['turn']  # Legacy close has no business continuation intent.
    if type(expected_turn) is not int or expected_turn != task['turn']:
        raise ValueError('Close requires the observed expected_turn')
    if task['state'] == 'closed':
        return dict(state='released', phase='verified', operation='close', task=task_id)
    if task.get('agy_swarm') == 'native':
        # The ACP owner records this turn's observable worker/session settlement,
        # not proof that arbitrary descendant processes died.
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = store._owned(db, task_id, origin)
            if current != task:
                raise ValueError('Native close snapshot changed')
            if current['state'] not in {'completed', 'cancelled', 'unknown'}:
                raise ValueError('Native owner must settle before close')
            cleanup = current.get('native_cleanup', {})
            verified = (current['state'] in {'completed', 'cancelled'} and cleanup.get('session_unloaded') is True
                        and cleanup.get('turn') == current['turn'] and isinstance(cleanup.get('workers'), list)
                        and ((bool(cleanup['workers']) and current.get('native_result', {}).get('turn') == current['turn']
                              and cleanup.get('evidence') == 'native-add-kill-completed-and-session-unload')
                             or cleanup.get('no_prompt') is True))
            verified = verified or (current['state'] == 'cancelled'
                        and current.get('session_creation_attempted') is False
                        and not current.get('native_session_id') and not current.get('native_prompt_submitted'))
            if not verified:
                current['state'] = 'unknown'
                store._check_writers(db, current)
                store._save(db, current)
                return dict(state='unknown', phase='hold', stage='native-proof',
                            reason_code='native_workers_unproven', external_close_attempted=False)
            current.update(state='closed', prompt='', release_epoch=1)
            Release.install(db)
            receipt = dict(state='released', phase='verified', operation='close', task=task_id,
                           source='native-single-owner-cleanup', external_close_attempted=False)
            db.execute('INSERT INTO release_fences VALUES (?,?,?,?)',
                       (task_id, 1, json.dumps(current), canonical(receipt)))
            Release._publish(db, current, receipt)
        return receipt
    never_created = (task['state'] == 'cancelled' and task.get('acpx_created') is False
                     and task.get('session_creation_attempted') is False
                     and not task.get('acpx_record_id'))
    if never_created and same:
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = store._owned(db, task_id, origin)
            if current != task:
                raise ValueError('Close snapshot changed')
            current.update(state='closed', prompt='', release_epoch=1)
            Release.install(db)
            receipt = dict(state='released',phase='verified',operation='close',task=task_id,
                           source='session-creation-never-attempted')
            db.execute('INSERT INTO release_fences VALUES (?,?,?,?)',
                       (task_id,1,json.dumps(current),canonical(receipt)))
            Release._publish(db,current,receipt)
        return receipt
    if task['device'] != 'local' or not task.get('acpx_record_id'):
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = store._owned(db,task_id,origin)
            if current != task:
                raise ValueError('Close snapshot changed')
            if current['state'] not in {'completed','cancelled','unknown'}:
                raise ValueError('Task is not settled for closing')
            current['state'] = 'unknown'
            store._check_writers(db,current)
            store._save(db,current)
        return dict(state='unknown',phase='hold',stage='resolve',reason_code='exact_record_unavailable',
                    task=task_id,external_close_attempted=False)
    return recover_native(store, origin, task_id, expected_turn, 'close', backend)


def recover_native(store, origin, task_id, expected_turn, operation, backend=None):
    """Host-authenticated exact-task recovery; no policy file or model identity."""
    if store.get(task_id, origin).get('agy_swarm') == 'native':
        raise ValueError('Native swarm cannot use ACPX exact-record recovery; retain native worker HOLD')
    backend = backend or NativeBackend()
    task = store.get(task_id, origin)
    if expected_turn is None or type(expected_turn) is not int or task['turn'] != expected_turn:
        raise ValueError('Recovery requires the observed expected_turn')
    if operation not in {'close', 'reconcile'}:
        raise ValueError('Unsupported recovery operation')
    if not store._allows(Origin(**task.get('controller', task['origin'])).key(), origin):
        raise PermissionError('Former controller has inspection rights only')
    if task['state'] == 'unknown':
        operation = 'reconcile'  # Unknown effects never authorize another close.
    if operation == 'reconcile' and task['state'] not in {'unknown','closed'}:
        raise ValueError('Reconcile requires unknown/HOLD; healthy business remains unchanged')
    if operation == 'close' and task['state'] == 'waiting_reply':
        raise ValueError('Pending questions must settle before ended-controller recovery close')
    if task['device'] != 'local' or not task.get('acpx_record_id'):
        raise ValueError('Exact local backend record required for recovery')
    policy = dict(task=task_id, owner=task['origin'], turn=task['turn'], record=task['acpx_record_id'],
                  session=task['session'], agent=task['agent'], cwd=task['cwd'],
                  request_id=task.get('backend_request_id', task['id']+':'+str(task['turn'])))
    with store.connect() as db:
        existing = (db.execute('SELECT receipt FROM release_fences WHERE task=?', (task_id,)).fetchone()
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='release_fences'").fetchone() else None)
    if existing:
        policy = json.loads(existing[0])['target']
    else:
        policy['acp'] = backend.resolve(policy)['acp']
    recovery = object.__new__(Release)
    recovery.home, recovery.policy, recovery.backend, recovery.clock = store.path.parent, policy, backend, time.time
    recovery.store = store
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current = store.get(task_id, origin)
        if current != task:
            raise ValueError('Recovery snapshot changed')
        if not store._allows(Origin(**current.get('controller', current['origin'])).key(), origin):
            raise PermissionError('Recovery controller authority changed')
        if existing and json.loads(existing[0]).get('state') == 'released':
            return json.loads(existing[0])
        if current['state'] not in {'completed','waiting_reply','cancelled','unknown'}:
            raise ValueError('Active execution must settle before recovery; reservation retained')
        Release.install(db)
        fence = db.execute('SELECT receipt FROM release_fences WHERE task=?', (task_id,)).fetchone()
        if not fence:
            current.update(state='unknown', release_epoch=1)
            store._check_writers(db, current)
            for entry in current.get('commands', {}).values():
                if entry.get('status') in {'pending','queued','target_started'}:
                    entry['status'] = 'release-revoked'
            if current.get('intervention', {}).get('status') in {'pending','queued','target_started'}:
                current['intervention']['status'] = 'release-revoked'
            receipt = dict(operation='native-recovery',target=policy,epoch=1,state='unknown',
                           phase='hold',attempts=[],admission='trusted-native-origin',binding_source='record-resolved',
                           arbitraryDetachedDescendantsProven=False)
            db.execute('INSERT INTO release_fences VALUES (?,?,?,?)',
                       (task_id,1,json.dumps(current),canonical(receipt)))
            Release._publish(db,current,receipt)
        elif operation == 'close':
            # Existing uncertainty is reconciled, never another external close.
            operation = 'reconcile'
    if operation == 'close':
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data,receipt FROM release_fences WHERE task=?',(task_id,)).fetchone()
            reserved, receipt = json.loads(row[0]), json.loads(row[1])
            if receipt['state'] == 'released' or receipt.get('attempts'):
                return receipt
            receipt['attempts'].append(dict(number=1,operation='close',stage='close',
                                           reason_code='in_progress',started_at=time.time()))
            Release._publish(db,reserved,receipt)
            close_snapshot = canonical(receipt)
        try:
            backend.close(policy)
            reason, stage = 'close_returned', 'verify'
        except Exception as exc:
            reason = exc.reason_code if isinstance(exc,BackendFailure) else 'close_uncertain'
            stage = exc.stage if isinstance(exc,BackendFailure) else 'close'
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            latest = json.loads(db.execute('SELECT receipt FROM release_fences WHERE task=?',(task_id,)).fetchone()[0])
            if canonical(latest) != close_snapshot:
                return latest
            receipt['attempts'][-1].update(stage=stage,reason_code=reason,finished_at=time.time())
            Release._publish(db,reserved,receipt)
    return recovery.reconcile()


class NativeBackend:
    def close(self, policy):
        return self._run(policy, False)

    def probe(self, policy):
        return self._run(policy, True)

    def resolve(self, policy):
        return self._run(policy, 'resolve')

    def _run(self, policy, probe):
        import subprocess
        try:
            from .updates import device_lock
        except ImportError:
            from updates import device_lock
        task = dict(device='local')
        with device_lock(task):
            operation = probe if isinstance(probe, str) else 'probe' if probe else 'close'
            result = subprocess.run(['node', str(Path(__file__).with_name('release-backend.cjs')), operation],
                                    input=canonical(policy).encode(), capture_output=True, timeout=45)
        try:
            value = json.loads(result.stdout)
        except (ValueError, UnicodeError):
            raise BackendFailure('probe' if probe else 'close', 'invalid_backend_response') from None
        if result.returncode != 0:
            allowed = {'record_binding_mismatch','name_scope_mismatch',
                       'name_scope_replaced_or_reopened','backend_unavailable'}
            reason = value.get('reason_code') if isinstance(value, dict) else None
            stage = value.get('stage') if isinstance(value, dict) else None
            raise BackendFailure(stage if stage in {'resolve','close','verify','probe'} else 'verify',
                                 reason if reason in allowed else 'backend_unavailable')
        return value
