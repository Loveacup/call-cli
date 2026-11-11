"""Native-only continuation via bounded, read-only Hermes session metadata.

Schema 32 is the inspected contract. Parent end_reason, not child end_reason,
records compression publication. No SessionDB import (it probes broader state).
"""
from contextlib import closing
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
    def __init__(self, path, hermes_home=None, hermes_root=None):
        self.hermes_home = Path(hermes_home) if hermes_home is not None else None
        self.hermes_root = Path(hermes_root) if hermes_root is not None else None
        super().__init__(path)

    def _allows(self, owner, origin):
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

    def _compression_ancestor(self, ancestor, current, profile):
        if not ancestor or not current or self.hermes_root is None:
            return False
        try:
            home, root = self.hermes_home.resolve(), self.hermes_root.resolve()
            store_profile = ('default' if home == root else home.name
                if home.parent == root/'profiles' and re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', home.name) else None)
            if store_profile is None or store_profile != profile:
                return False
            path = home/'state.db'
            if path.is_symlink() or path.resolve().parent != home:
                return False
            uri = path.as_uri() + '?mode=ro'
            with closing(sqlite3.connect(uri, uri=True, timeout=.1, isolation_level=None)) as db:
                db.execute('PRAGMA query_only=ON')
                db.execute('BEGIN')  # All hops observe one committed metadata snapshot.
                if db.execute('SELECT version FROM schema_version').fetchall() != [(32,)]:
                    return False
                if db.execute("SELECT type FROM sqlite_master WHERE name='sessions'").fetchall() != [('table',)]:
                    return False
                columns = {row[1]: row for row in db.execute('PRAGMA table_info(sessions)')}
                expected = {'id': 'TEXT', 'parent_session_id': 'TEXT', 'end_reason': 'TEXT',
                    'ended_at': 'REAL', 'model_config': 'TEXT', 'source': 'TEXT', 'profile_name': 'TEXT'}
                if any(name not in columns or columns[name][2] != kind for name, kind in expected.items()):
                    return False
                if [name for name, row in columns.items() if row[5]] != ['id']:
                    return False
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
                    ''', (current, store_profile, profile)).fetchone()
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
                  json_extract(owner, '$.profile') = ?
                  AND json_extract(owner, '$.session_key') = ?
                  AND json_extract(owner, '$.platform') = ?
                  AND json_extract(owner, '$.chat_id') = ?
                  AND json_extract(owner, '$.thread_id') = ? ELSE 0 END''',
                (origin.profile, origin.session_key, origin.platform, origin.chat_id, origin.thread_id)).fetchall()
        return [json.loads(row['data']) for row in rows if self._allows(row['owner'], origin)]
