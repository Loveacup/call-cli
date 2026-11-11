"""Native ownership through synthetic, metadata-only Hermes SQLite fixtures."""
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from test_plugin import Ctx, load_plugin


class NativeContinuityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root
        self.db = self.home/'state.db'
        with closing(sqlite3.connect(self.db)) as db:
            db.executescript('''
                CREATE TABLE schema_version(version INTEGER NOT NULL);
                INSERT INTO schema_version VALUES (32);
                CREATE TABLE sessions(id TEXT PRIMARY KEY, parent_session_id TEXT,
                    end_reason TEXT, ended_at REAL, model_config TEXT, source TEXT,
                    profile_name TEXT);
            ''')
        self.mod = load_plugin()
        self.ctx = Ctx()
        self.env = {'HERMES_SESSION_ID': 'before', 'HERMES_SESSION_KEY': 'route',
            'HERMES_SESSION_PLATFORM': 'telegram', 'HERMES_SESSION_CHAT_ID': 'chat',
            'HERMES_SESSION_THREAD_ID': 'topic'}
        self.c = self.mod.Coordinator(self.ctx, self.root/'plugin-data',
            lambda key, default='': self.env.get(key, default), hermes_home=self.home, hermes_root=self.home)

    def row(self, sid, parent=None, reason=None, ended=None, config=None, source='cli', profile=None):
        connection = sqlite3.connect(self.db)
        try:
            with connection:
                connection.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?)',
                    (sid, parent, reason, ended, config, source, profile))
        finally:
            connection.close()

    def start(self, **kwargs):
        return self.c.handle(dict(action='start', agent='omp', cwd=str(self.root),
            prompt='synthetic task', **kwargs))['task']

    def action(self, action, task, **kwargs):
        return self.c.handle(dict(action=action, task_id=task['id'], **kwargs))

    def test_committed_parent_stamp_allows_new_segment_without_rewriting_origin(self):
        task = self.start()
        self.row('before', reason='compression', ended=1)
        self.row('after', parent='before')
        self.env['HERMES_SESSION_ID'] = 'after'
        result = self.action('status', task)['task']
        self.assertEqual(result['origin']['session_id'], 'before')
        self.assertEqual([item['id'] for item in self.c.handle({'action': 'list'})['tasks']], [task['id']])
        self.assertEqual(self.action('cancel', task)['task']['state'], 'cancelled')

    def test_multihop_reply_events_and_close_keep_original_origin(self):
        task = self.start()
        store = self.mod.Store(self.root/'plugin-data/call-cli/tasks.sqlite3')
        store.event(task['id'], 'q', 'question', 'Which output?')
        store.update(task['id'], state='waiting_reply')
        self.row('before', reason='compression', ended=1)
        self.row('middle', parent='before', reason='compression', ended=2)
        self.row('after', parent='middle', config='{}')
        self.env['HERMES_SESSION_ID'] = 'after'
        self.assertEqual(self.action('events', task)['events'][0]['token'], 'q')
        resumed = self.action('reply', task, question_id='q', prompt='answer')['task']
        self.assertEqual((resumed['turn'], resumed['origin']), (2, task['origin']))
        store.update(task['id'], state='completed')
        result = SimpleNamespace(returncode=0, stdout=b'{"status":"no-session"}')
        with patch.object(self.mod, 'control', return_value=result), \
                patch('callcli_plugin.updates.device_lock', return_value=__import__('contextlib').nullcontext(True)):
            self.assertEqual(self.action('close', task)['task']['state'], 'closed')

    def test_unknown_and_pending_intervention_keep_existing_guards(self):
        task = self.start(write=True)
        store = self.mod.Store(self.root/'plugin-data/call-cli/tasks.sqlite3')
        store.update(task['id'], state='running')
        self.row('before', reason='compression', ended=1)
        self.row('after', parent='before')
        self.env['HERMES_SESSION_ID'] = 'after'
        self.action('intervene', task, command_id='one', prompt='revision')
        store.finish_turn(task['id'], 1, 'cancelled', 0, 'cancelled')
        self.assertEqual(self.action('status', task)['task']['turn'], 2)
        launches = len(self.ctx.calls)
        self.action('status', task)
        self.assertEqual(len(self.ctx.calls), launches)
        store.update(task['id'], state='unknown')
        with self.assertRaises(ValueError):
            self.action('intervene', task, command_id='two', prompt='no replay')
        with self.assertRaises(ValueError):
            self.start(write=True)
        self.assertEqual(self.action('cancel', task)['task']['state'], 'unknown')

    def test_each_other_origin_field_is_required_and_list_does_not_leak(self):
        task = self.start()
        self.row('before', reason='compression', ended=1)
        self.row('after', parent='before')
        self.env['HERMES_SESSION_ID'] = 'after'
        for key in ('HERMES_SESSION_KEY', 'HERMES_SESSION_PLATFORM',
                    'HERMES_SESSION_CHAT_ID', 'HERMES_SESSION_THREAD_ID'):
            original = self.env[key]
            with self.subTest(key=key):
                self.env[key] = 'other'
                with self.assertRaises(PermissionError):
                    self.action('cancel', task)
                self.assertEqual(self.c.handle({'action': 'list'})['tasks'], [])
                self.env[key] = original
        self.ctx.profile_name = 'other'
        with self.assertRaises(PermissionError):
            self.action('status', task)
        self.assertEqual(self.c.handle({'action': 'list'})['tasks'], [])

    def test_noncompression_and_fork_metadata_are_refused(self):
        cases = [
            ('child_stamp', None, None, None, 'compression', 1, 'cli'),
            ('reset', 'new_session', 1, None, None, None, 'cli'),
            ('uncommitted', 'compression', None, None, None, None, 'cli'),
            ('tool', 'compression', 1, None, None, None, 'tool'),
            ('malformed', 'compression', 1, '{bad', None, None, 'cli'),
            ('array', 'compression', 1, '[]', None, None, 'cli'),
        ] + [(key, 'compression', 1, json.dumps({key: 'before'}), None, None, 'cli')
             for key in ('_reset_from', '_branched_from', '_delegate_from')]
        for label, reason, ended, config, child_reason, child_end, source in cases:
            with self.subTest(case=label):
                before, after = label+'-before', label+'-after'
                self.env['HERMES_SESSION_ID'] = before
                task = self.start()
                self.row(before, reason=reason, ended=ended)
                self.row(after, parent=before, reason=child_reason, ended=child_end,
                    config=config, source=source)
                self.env['HERMES_SESSION_ID'] = after
                with self.assertRaises(PermissionError):
                    self.action('status', task)
                self.assertNotIn(task['id'], [t['id'] for t in self.c.handle({'action': 'list'})['tasks']])

    def test_reverse_unrelated_cycle_and_profile_row_are_refused(self):
        task = self.start()
        self.row('before', parent='after', reason='compression', ended=1)
        self.row('after', parent='before')
        self.env['HERMES_SESSION_ID'] = 'after'
        with self.assertRaises(PermissionError):
            self.action('status', task)
        connection = sqlite3.connect(self.db)
        with closing(connection), connection:
            connection.execute("UPDATE sessions SET parent_session_id=NULL WHERE id='before'")
            connection.execute("UPDATE sessions SET profile_name='other' WHERE id='after'")
        with self.assertRaises(PermissionError):
            self.action('status', task)
        self.env['HERMES_SESSION_ID'] = 'unrelated'
        with self.assertRaises(PermissionError):
            self.action('status', task)

    def test_missing_locked_corrupt_or_unknown_schema_fails_closed_but_exact_works(self):
        task = self.start()
        self.row('before', reason='compression', ended=1)
        self.row('after', parent='before')
        for alteration in ('locked', 'version', 'schema', 'corrupt', 'missing'):
            with self.subTest(alteration=alteration):
                original = self.db.read_bytes()
                lock = None
                try:
                    if alteration == 'locked':
                        lock = sqlite3.connect(self.db)
                        lock.execute('BEGIN EXCLUSIVE')
                    elif alteration in ('version', 'schema'):
                        with closing(sqlite3.connect(self.db)) as db, db:
                            db.execute('UPDATE schema_version SET version=999' if alteration == 'version'
                                else 'ALTER TABLE sessions RENAME COLUMN ended_at TO unknown_field')
                    elif alteration == 'corrupt':
                        self.db.write_bytes(b'not a database')
                    else:
                        self.db.unlink()
                    self.env['HERMES_SESSION_ID'] = 'after'
                    with self.assertRaises(PermissionError):
                        self.action('status', task)
                    self.env['HERMES_SESSION_ID'] = 'before'
                    self.assertEqual(self.action('status', task)['task']['id'], task['id'])
                finally:
                    if lock:
                        lock.rollback(); lock.close()
                    self.db.write_bytes(original)

    def test_uncommitted_publication_is_not_visible_and_rollback_does_not_grant(self):
        task = self.start()
        self.row('before')
        writer = sqlite3.connect(self.db)
        try:
            writer.execute("UPDATE sessions SET end_reason='compression',ended_at=1 WHERE id='before'")
            writer.execute("INSERT INTO sessions VALUES ('after','before',NULL,NULL,NULL,'cli','default')")
            self.env['HERMES_SESSION_ID'] = 'after'
            with self.assertRaises(PermissionError):
                self.action('status', task)
            writer.rollback()
            with self.assertRaises(PermissionError):
                self.action('status', task)
        finally:
            writer.close()

    def test_standalone_cannot_use_native_metadata_authority(self):
        from collab import Collaboration
        c = Collaboration(self.root/'plugin-data/call-cli/tasks.sqlite3', launch=lambda task: None)
        task = self.start()
        self.row('before', reason='compression', ended=1)
        self.row('after', parent='before')
        client = c.connect('hermes')['client_id']
        with self.assertRaises(PermissionError):
            c.status(client, task['id'])

    def test_wrong_direction_depth_limit_and_marker_inheritance_are_denied(self):
        task = self.start()
        self.row('before', reason='compression', ended=1)
        self.row('after', parent='before')
        self.env['HERMES_SESSION_ID'] = 'after'
        newer_task = self.start()
        self.env['HERMES_SESSION_ID'] = 'before'
        with self.assertRaises(PermissionError):
            self.action('status', newer_task)
        previous = 'before'
        for index in range(34):
            sid = 'hop-'+str(index)
            self.row(sid, parent=previous, reason='compression' if index < 33 else None,
                ended=index+2 if index < 33 else None)
            previous = sid
        self.env['HERMES_SESSION_ID'] = previous
        with self.assertRaises(PermissionError):
            self.action('status', task)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE sessions SET model_config=? WHERE id='after'",
                (json.dumps({'_delegate_from': 'earlier-ancestor'}),))
        self.env['HERMES_SESSION_ID'] = 'after'
        with self.assertRaises(PermissionError):
            self.action('status', task)

    def test_null_profile_requires_store_scope_and_canonical_marker_is_denied(self):
        task = self.start()
        self.row('before', reason='compression', ended=1)
        self.row('after', parent='before')
        self.env['HERMES_SESSION_ID'] = 'after'
        self.assertEqual(self.action('status', task)['task']['id'], task['id'])
        self.c.hermes_root = self.root/'another-installation'
        with self.assertRaises(PermissionError):
            self.action('status', task)
        self.c.hermes_root = self.home
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE sessions SET profile_name='default', model_config=? WHERE id='after'",
                (json.dumps({'_compressed_from': 'before'}),))
        with self.assertRaises(PermissionError):
            self.action('status', task)

    def test_only_metadata_projection_is_read_and_database_bytes_remain_unchanged(self):
        task = self.start()
        self.row('before', reason='compression', ended=1)
        self.row('after', parent='before', config='{"private_setting":"not returned"}')
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('CREATE TABLE messages(secret TEXT)')
            db.execute("INSERT INTO messages VALUES ('never query')")
        before = self.db.read_bytes()
        reads = set()
        connect = sqlite3.connect
        def audited(*args, **kwargs):
            db = connect(*args, **kwargs)
            if kwargs.get('uri'):
                def allow(action, table, column, database, trigger):
                    if action == sqlite3.SQLITE_READ:
                        reads.add((table, column))
                        allowed = {'sessions': {'id', 'parent_session_id', 'end_reason',
                            'ended_at', 'model_config', 'source', 'profile_name'},
                            'schema_version': {'version'}, 'sqlite_master': {'type', 'name'}}
                        if table not in allowed or column not in allowed[table]:
                            return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                db.set_authorizer(allow)
                # SQLite authorizer enforces the projection independent of SQL spelling.
            return db
        self.env['HERMES_SESSION_ID'] = 'after'
        with patch('callcli_plugin.native_continuity.sqlite3.connect', side_effect=audited):
            self.assertEqual(self.action('status', task)['task']['id'], task['id'])
        self.assertEqual(self.db.read_bytes(), before)
        self.assertIn(('sessions', 'parent_session_id'), reads)
        self.assertNotIn(('messages', 'secret'), reads)

    def test_native_registration_uses_host_effective_home_not_process_environment(self):
        import os
        import sys
        import types
        task = self.start()
        self.row('before', reason='compression', ended=1)
        self.row('after', parent='before')
        self.env['HERMES_SESSION_ID'] = 'after'
        constants = types.ModuleType('hermes_constants')
        constants.get_hermes_home = lambda: self.home
        constants.get_default_hermes_root = lambda **kwargs: self.root
        profiles = types.ModuleType('hermes_cli.profiles')
        profiles.get_active_profile_name = lambda: 'default'
        session = types.ModuleType('gateway.session_context')
        session.get_session_env = lambda key, default='': self.env.get(key, default)
        modules = {'hermes_constants': constants, 'hermes_cli.profiles': profiles,
            'gateway.session_context': session}
        with patch.dict(sys.modules, modules), patch.dict(os.environ, HERMES_HOME='/not-this-profile'):
            self.mod.register(self.ctx)
            handler = self.ctx.registration['handler']
            self.assertEqual(json.loads(handler({'action':'status', 'task_id':task['id']}))['task']['id'], task['id'])
            profiles.get_active_profile_name = lambda: 'other'
            self.assertEqual(json.loads(handler({'action':'status', 'task_id':task['id']}))['error_type'], 'PermissionError')

    def test_database_symlink_is_refused_before_other_profile_is_opened(self):
        task = self.start()
        self.db.unlink()
        other = self.root/'other-profile.db'
        other.write_bytes(b'must not be opened')
        self.db.symlink_to(other)
        self.env['HERMES_SESSION_ID'] = 'after'
        connect = sqlite3.connect
        def no_metadata_open(*args, **kwargs):
            if kwargs.get('uri'):
                raise AssertionError('symlink database opened')
            return connect(*args, **kwargs)
        with patch('callcli_plugin.native_continuity.sqlite3.connect', side_effect=no_metadata_open):
            with self.assertRaises(PermissionError):
                self.action('status', task)

    def test_cycle_beyond_task_origin_is_denied(self):
        task = self.start()
        self.row('before', parent='older', reason='compression', ended=1)
        self.row('older', parent='before', reason='compression', ended=1)
        self.row('after', parent='before')
        self.env['HERMES_SESSION_ID'] = 'after'
        with self.assertRaises(PermissionError):
            self.action('status', task)

    def test_named_profile_scope_uses_only_its_metadata_store(self):
        named = self.root/'profiles'/'named'
        named.mkdir(parents=True)
        self.db.rename(named/'state.db')
        self.db = named/'state.db'
        self.c.hermes_home = named
        self.ctx.profile_name = 'named'
        task = self.start()
        self.row('before', reason='compression', ended=1)
        self.row('after', parent='before', profile='named')
        self.env['HERMES_SESSION_ID'] = 'after'
        self.assertEqual(self.action('status', task)['task']['id'], task['id'])
        self.ctx.profile_name = 'default'
        with self.assertRaises(PermissionError):
            self.action('status', task)

    def test_unknown_equal_sources_do_not_grant_continuity(self):
        for source in ('unknown', 'future-native', ''):
            with self.subTest(source=source):
                before, after = 'before-'+source, 'after-'+source
                self.env['HERMES_SESSION_ID'] = before
                task = self.start()
                self.row(before, reason='compression', ended=1, source=source)
                self.row(after, parent=before, source=source)
                self.env['HERMES_SESSION_ID'] = after
                with self.assertRaises(PermissionError):
                    self.action('status', task)
                self.assertEqual(self.c.handle({'action': 'list'})['tasks'], [])

    def test_registration_keeps_task_root_but_rejects_unowned_metadata_home(self):
        import sys
        import types
        task = self.start()
        self.row('before', reason='compression', ended=1)
        self.row('after', parent='before')
        self.env['HERMES_SESSION_ID'] = 'after'
        constants = types.ModuleType('hermes_constants')
        constants.get_hermes_home = lambda: self.home
        constants.get_default_hermes_root = lambda **kw: self.root if kw else self.root/'launch-root'
        profiles = types.ModuleType('hermes_cli.profiles')
        profiles.get_active_profile_name = lambda: 'default'
        session = types.ModuleType('gateway.session_context')
        session.get_session_env = lambda key, default='': self.env.get(key, default)
        with patch.dict(sys.modules, {'hermes_constants': constants,
                'hermes_cli.profiles': profiles, 'gateway.session_context': session}):
            self.mod.register(self.ctx)
            handler = self.ctx.registration['handler']
            self.assertEqual(json.loads(handler({'action':'status', 'task_id':task['id']}))['error_type'], 'PermissionError')
            self.env['HERMES_SESSION_ID'] = 'before'
            profiles.get_active_profile_name = lambda: (_ for _ in ()).throw(RuntimeError('unavailable'))
            self.assertEqual(json.loads(handler({'action':'status', 'task_id':task['id']}))['task']['id'], task['id'])

    def test_corrupt_foreign_owner_does_not_break_authorized_list(self):
        task = self.start()
        store = self.mod.Store(self.root/'plugin-data/call-cli/tasks.sqlite3')
        with store.connect() as db:
            db.execute("INSERT INTO tasks VALUES ('corrupt','not-json','{}')")
        self.assertEqual([item['id'] for item in self.c.handle({'action':'list'})['tasks']], [task['id']])

    def test_cli_sid_fallback_key_does_not_weaken_five_field_match(self):
        self.env.pop('HERMES_SESSION_KEY')
        task = self.start()
        self.row('before', reason='compression', ended=1)
        self.row('after', parent='before')
        self.env['HERMES_SESSION_ID'] = 'after'
        with self.assertRaises(PermissionError):
            self.action('status', task)
        self.assertEqual(self.c.handle({'action':'list'})['tasks'], [])


if __name__ == '__main__':
    unittest.main()
