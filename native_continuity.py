"""Native-only continuation via bounded, read-only Hermes session metadata.

Schema 32 is the inspected contract. Parent end_reason, not child end_reason,
records compression publication. No SessionDB import (it probes broader state).
"""
from contextlib import closing, contextmanager
import dataclasses
import json
import math
import re
from pathlib import Path
import sqlite3

from .bridge import Origin, Store

# Inspected native interactive surfaces and built-in messaging routes. New or
# plugin-defined sources need explicit compatibility review, not a truthy label.
_NATIVE_SOURCES = frozenset({'cli', 'tui', 'desktop', 'acp', 'oneshot', 'local',
    'telegram', 'discord', 'whatsapp', 'whatsapp_cloud', 'slack', 'signal',
    'mattermost', 'matrix', 'email', 'sms', 'dingtalk', 'api_server', 'webhook',
    'msgraph_webhook', 'feishu', 'wecom', 'wecom_callback', 'weixin', 'bluebubbles',
    'qqbot', 'yuanbao', 'relay'})


class NativeStore(Store):
    def __init__(self, path, hermes_home=None, hermes_root=None, *, action=None):
        self.hermes_home = Path(hermes_home) if hermes_home is not None else None
        self.hermes_root = Path(hermes_root) if hermes_root is not None else None
        self.action = action
        super().__init__(path)

    def _same_principal(self, owner, origin):
        if super()._allows(owner, origin):
            return True
        if self.hermes_home is None:
            return False
        try:
            previous = Origin(**json.loads(owner))
        except (TypeError, ValueError):
            return False
        if any(getattr(previous, key) != getattr(origin, key) for key in
               ('profile', 'session_key', 'platform', 'chat_id', 'thread_id')):
            return False
        return self._compression_ancestor(previous.session_id, origin.session_id, origin.profile)

    def _allows(self, owner, origin):
        if self._same_principal(owner, origin):
            return True
        if self.action not in {'list', 'status', 'events', 'reply', 'intervene', 'cancel', 'close', 'reconcile'}:
            return False
        try:
            previous = Origin(**json.loads(owner))
        except (TypeError, ValueError):
            return False
        if any(getattr(previous, key) != getattr(origin, key) for key in
               ('profile', 'platform', 'chat_id', 'thread_id')):
            return False
        sid_route = (previous.platform == 'cli' and not previous.chat_id and not previous.thread_id
            and previous.session_key == previous.session_id and origin.session_key == origin.session_id)
        if previous.session_key != origin.session_key and not sid_route:
            return False
        try:
            with self._metadata(origin.profile) as db:
                rows = db.execute('''SELECT id, end_reason, ended_at, source FROM sessions
                    WHERE id IN (?, ?) AND COALESCE(profile_name, ?) = ?''',
                    (previous.session_id, origin.session_id, origin.profile, origin.profile)).fetchall()
                sessions = {row[0]: row[1:] for row in rows}
                old, current = sessions.get(previous.session_id), sessions.get(origin.session_id)
                return bool(old and current and previous.session_id != origin.session_id
                    and old[0] in {'agent_close', 'cli_close', 'tui_close'}
                    and old[2] in _NATIVE_SOURCES | {'subagent'}
                    and isinstance(old[1], (int, float)) and math.isfinite(old[1]) and old[1] > 0
                    and current[0] is None and current[1] is None
                    and current[2] in _NATIVE_SOURCES)
        except (sqlite3.Error, OSError, ValueError):
            return False

    def _owned(self, db, task_id, origin):
        task = super()._owned(db, task_id, origin)
        controller = Origin(**task.get('controller', task['origin'])).key()
        if not self._same_principal(controller, origin):
            if self.action == 'close':
                raise ValueError('Ended-owner close requires execution reconciliation and release fencing')
            if task['state'] not in {'completed', 'waiting_reply', 'cancelled'}:
                raise ValueError('Ended-controller task is read-only until reconciled; reservation retained')
        return task

    def _admit_controller(self, task, origin, expected_turn, command_id):
        controller = Origin(**task.get('controller', task['origin'])).key()
        if self._same_principal(controller, origin):
            return
        if expected_turn is None or not command_id:
            raise ValueError('Cross-controller continuation requires expected_turn and command_id')
        if task['state'] not in {'completed', 'waiting_reply', 'cancelled'} or task.get('release_epoch'):
            raise ValueError('Controller transfer requires a settled unfenced task')
        if task.get('answers') and self.action == 'reply':
            raise ValueError('Partial answers belong to previous controller; explicit intervention required')
        task.update(controller=dataclasses.asdict(origin),
                    control_epoch=task.get('control_epoch', 1)+1)

    def get(self, task_id, origin=None):
        task = super().get(task_id)
        if origin is not None and not (self._same_principal(Origin(**task['origin']).key(), origin)
                or self._allows(Origin(**task.get('controller', task['origin'])).key(), origin)):
            raise PermissionError('Task belongs to another Hermes controller/route')
        return task

    def advance_intervention(self, task_id, origin):
        task = self.get(task_id, origin)
        if not self._same_principal(Origin(**task.get('controller', task['origin'])).key(), origin):
            return task, False  # Inspection is never authority to resume old business.
        return super().advance_intervention(task_id, origin)

    @contextmanager
    def _metadata(self, profile):
        if self.hermes_home is None or self.hermes_root is None:
            raise ValueError('Trusted metadata home unavailable')
        home, root = self.hermes_home.resolve(), self.hermes_root.resolve()
        store_profile = ('default' if home == root else home.name
            if home.parent == root/'profiles' and re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', home.name) else None)
        if store_profile is None or store_profile != profile:
            raise ValueError('Metadata profile mismatch')
        path = home/'state.db'
        if path.is_symlink() or path.resolve().parent != home:
            raise ValueError('Metadata database must be local to trusted home')
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.1, isolation_level=None)) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            if db.execute('SELECT version FROM schema_version').fetchall() != [(32,)]:
                raise ValueError('Unsupported metadata schema')
            if db.execute("SELECT type FROM sqlite_master WHERE name='sessions'").fetchall() != [('table',)]:
                raise ValueError('Invalid sessions table')
            columns = {row[1]: row for row in db.execute('PRAGMA table_info(sessions)')}
            expected = {'id': 'TEXT', 'parent_session_id': 'TEXT', 'end_reason': 'TEXT',
                'ended_at': 'REAL', 'model_config': 'TEXT', 'source': 'TEXT', 'profile_name': 'TEXT'}
            if (any(name not in columns or columns[name][2] != kind for name, kind in expected.items())
                    or [name for name, row in columns.items() if row[5]] != ['id']):
                raise ValueError('Unsupported sessions schema')
            yield db

    def _compression_ancestor(self, ancestor, current, profile):
        if not ancestor or not current or self.hermes_root is None:
            return False
        try:
            with self._metadata(profile) as db:
                seen = set()
                child = None
                found = False
                for _ in range(33):
                    if current in seen:
                        return False
                    seen.add(current)
                    row = db.execute('''
                        SELECT parent_session_id, end_reason, ended_at,
                            CASE WHEN model_config IS NULL THEN 1
                                 WHEN json_valid(model_config) THEN
                                     json_type(model_config) = 'object'
                                     AND json_type(model_config, '$._reset_from') IS NULL
                                     AND json_type(model_config, '$._branched_from') IS NULL
                                     AND json_type(model_config, '$._delegate_from') IS NULL
                                     AND json_type(model_config, '$._compressed_from') IS NULL
                                 ELSE 0 END,
                            source
                        FROM sessions WHERE id = ? AND COALESCE(profile_name, ?) = ?
                    ''', (current, profile, profile)).fetchone()
                    if row is None:
                        return False
                    parent, reason, ended, clean, source = row
                    if clean != 1 or source not in _NATIVE_SOURCES:
                        return False
                    if child is None:
                        if ended is not None or reason is not None:
                            return False
                    elif (reason != 'compression' or not isinstance(ended, (int, float))
                          or not math.isfinite(ended) or ended <= 0 or source != child[4]):
                        return False
                    if current == ancestor:
                        found = child is not None
                    if parent is None:
                        return found
                    if not isinstance(parent, str) or not parent:
                        return False
                    child, current = row, parent
        except (sqlite3.Error, OSError, ValueError):
            return False
        return False

    def list(self, origin):
        # Only same-route owner identities are candidates; no foreign task data
        # leaves the Store. Each returned row passes the same authorization check.
        with self.connect() as db:
            rows = db.execute('''SELECT owner, data FROM tasks
                WHERE CASE WHEN json_valid(owner) THEN
                  json_extract(owner, '$.profile') = ? ELSE 0 END''', (origin.profile,)).fetchall()
        return [json.loads(row['data']) for row in rows if
                self._same_principal(row['owner'], origin) or self._allows(
                    Origin(**json.loads(row['data']).get('controller', json.loads(row['data'])['origin'])).key(), origin)]
