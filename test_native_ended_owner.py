"""Native action permissions over synthetic host and task SQLite databases."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from test_plugin import Ctx, load_plugin


class EndedOwnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root/'state.db'
        with closing(sqlite3.connect(self.db)) as db:
            db.executescript('''CREATE TABLE schema_version(version INTEGER NOT NULL);
                INSERT INTO schema_version VALUES (32);
                CREATE TABLE sessions(id TEXT PRIMARY KEY, parent_session_id TEXT,
                    end_reason TEXT, ended_at REAL, model_config TEXT, source TEXT,
                    profile_name TEXT);''')
        self.mod = load_plugin()
        self.ctx = Ctx()
        self.env = {'HERMES_SESSION_ID': 'old', 'HERMES_SESSION_KEY': 'route',
            'HERMES_SESSION_PLATFORM': 'telegram', 'HERMES_SESSION_CHAT_ID': 'chat',
            'HERMES_SESSION_THREAD_ID': 'topic'}
        self.c = self.mod.Coordinator(self.ctx, self.root/'plugin-data',
            lambda key, default='': self.env.get(key, default),
            hermes_home=self.root, hermes_root=self.root)
        self.store = self.mod.Store(self.root/'plugin-data/call-cli/tasks.sqlite3')

    def row(self, sid, reason=None, ended=None, source='cli', profile=None):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('INSERT INTO sessions VALUES (?,NULL,?,?,NULL,?,?)',
                (sid, reason, ended, source, profile))

    def start(self, **kwargs):
        return self.c.handle(dict(action='start', agent='omp', cwd=str(self.root),
            prompt='synthetic task', **kwargs))['task']

    def action(self, action, task, **kwargs):
        return self.c.handle(dict(action=action, task_id=task['id'], **kwargs))

    def end_owner(self):
        self.row('old', 'agent_close', 1, 'subagent')
        self.row('current')
        self.env['HERMES_SESSION_ID'] = 'current'

    def test_ended_unrelated_owner_can_discover_inspect_and_read_events(self):
        task = self.start(write=True)
        self.store.update(task['id'], state='unknown')
        self.store.event(task['id'], 'progress', 'stage', 'inspect me')
        self.end_owner()
        self.assertEqual(self.action('status', task)['task']['origin'], task['origin'])
        self.assertEqual([t['id'] for t in self.c.handle({'action': 'list'})['tasks']], [task['id']])
        self.assertEqual(self.action('events', task)['events'][0]['text'], 'inspect me')
        self.assertEqual(self.store.get(task['id'])['state'], 'unknown')


    def test_inspection_does_not_resume_pending_business(self):
        task = self.start(write=True)
        owner = self.mod.capture_origin(lambda k, d='': self.env.get(k, d), 'default')
        self.store.update(task['id'], state='running')
        self.store.intervene(task['id'], owner, 'old queued revision', 'old-command')
        self.store.finish_turn(task['id'], 1, 'cancelled', 0, 'cancelled')
        before = self.store.get(task['id'])
        self.end_owner()
        self.assertEqual(self.action('status', task)['task']['turn'], 1)
        self.action('events', task)
        self.assertEqual(self.store.get(task['id']), before)
        self.assertEqual(len(self.ctx.calls), 1)

    def test_active_unknown_and_closing_are_read_only_and_keep_writer(self):
        task = self.start(write=True)
        self.end_owner()
        for state in ('queued', 'running', 'cancelling', 'closing', 'unknown'):
            with self.subTest(state=state):
                self.store.update(task['id'], state=state)
                before = self.store.get(task['id'])
                self.assertEqual(self.action('status', task)['task']['state'], state)
                for action, args in [('reply', {'prompt': 'no replay'}),
                        ('intervene', {'prompt': 'no replay', 'command_id': state}),
                        ('cancel', {}), ('close', {})]:
                    with self.assertRaises((ValueError, PermissionError)):
                        self.action(action, task, **args)
                with self.assertRaises(ValueError):
                    self.start(write=True)
                self.assertEqual(self.store.get(task['id']), before)

    def test_settled_explicit_reply_rechecks_writers_and_old_turn_cannot_win(self):
        task = self.start(write=True)
        self.store.update(task['id'], state='completed')
        self.end_owner()
        blocker = self.start(write=True)
        with self.assertRaises(ValueError):
            self.action('reply', task, prompt='authorized new work', expected_turn=1, command_id='resume')
        self.action('cancel', blocker)
        resumed = self.action('reply', task, prompt='authorized new work', expected_turn=1, command_id='resume')['task']
        self.assertEqual((resumed['turn'], resumed['state'], resumed['origin']), (2, 'queued', task['origin']))
        self.assertFalse(self.store.finish_turn(task['id'], 1, 'completed', 0, 'end_turn'))
        with self.assertRaises(ValueError):
            self.action('reply', task, prompt='second writer')

    def test_active_missing_uncertain_and_reopened_owner_are_denied(self):
        task = self.start()
        self.row('current')
        self.env['HERMES_SESSION_ID'] = 'current'
        for reason, ended in [(None, None), ('agent_close', None), ('agent_close', -1),
                ('agent_close', float('inf')), ('compression', 1), ('ws_disconnect', 1),
                ('idle_timeout', 1), ('new_session', 1), ('unknown-close', 1)]:
            with self.subTest(reason=reason, ended=ended):
                with closing(sqlite3.connect(self.db)) as db, db:
                    db.execute('DELETE FROM sessions WHERE id=?', ('old',))
                with self.assertRaises(PermissionError):
                    self.action('status', task)
                self.row('old', reason, ended, 'subagent')
                with self.assertRaises(PermissionError):
                    self.action('status', task)
                self.assertEqual(self.c.handle({'action': 'list'})['tasks'], [])

    def test_profile_route_and_model_supplied_identity_do_not_grant_access(self):
        task = self.start()
        self.end_owner()
        for key in ('HERMES_SESSION_KEY', 'HERMES_SESSION_PLATFORM',
                    'HERMES_SESSION_CHAT_ID', 'HERMES_SESSION_THREAD_ID'):
            original = self.env[key]
            self.env[key] = 'foreign'
            with self.assertRaises(PermissionError):
                self.action('status', task)
            self.assertEqual(self.c.handle({'action': 'list'})['tasks'], [])
            self.env[key] = original
        self.ctx.profile_name = 'foreign'
        with self.assertRaises(PermissionError):
            self.action('status', task)
        self.ctx.profile_name = 'default'
        with self.assertRaises(ValueError):
            self.action('status', task, origin=task['origin'])
        with self.assertRaises(KeyError):
            self.action('status', {'id': 'missing'})

    def test_reopen_revokes_exception_and_uncommitted_close_does_not_grant(self):
        task = self.start()
        self.end_owner()
        self.action('status', task)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE sessions SET ended_at=NULL,end_reason=NULL WHERE id='old'")
        with self.assertRaises(PermissionError):
            self.action('status', task)
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute("UPDATE sessions SET ended_at=2,end_reason='agent_close' WHERE id='old'")
            with self.assertRaises(PermissionError):
                self.action('status', task)
            writer.rollback()

    def test_close_requires_execution_reconciliation_not_just_ended_owner(self):
        task = self.start(write=True)
        self.store.update(task['id'], state='completed')
        self.end_owner()
        with self.assertRaises(ValueError):
            self.action('close', task)
        self.assertEqual(self.store.get(task['id'])['state'], 'completed')

    def test_missing_incompatible_or_busy_metadata_fails_closed(self):
        task = self.start()
        self.end_owner()
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('UPDATE schema_version SET version=999')
        with self.assertRaises(PermissionError):
            self.action('status', task)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('UPDATE schema_version SET version=32')
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute('BEGIN EXCLUSIVE')
            with self.assertRaises(PermissionError):
                self.action('status', task)
            writer.rollback()
        self.db.unlink()
        with self.assertRaises(PermissionError):
            self.action('status', task)

    def test_cli_sid_keys_allow_ended_owner_but_not_arbitrary_route_keys(self):
        self.env = {'HERMES_SESSION_ID': 'old'}
        task = self.start()
        self.end_owner()
        self.assertEqual(self.action('status', task)['task']['id'], task['id'])
        self.assertEqual([t['id'] for t in self.c.handle({'action': 'list'})['tasks']], [task['id']])
        self.env['HERMES_SESSION_KEY'] = 'not-a-host-sid-key'
        with self.assertRaises(PermissionError):
            self.action('status', task)

    def test_old_and_current_owner_compete_for_only_one_settled_successor(self):
        from concurrent.futures import ThreadPoolExecutor
        task = self.start(write=True)
        owner = self.mod.capture_origin(lambda k, d='': self.env.get(k, d), 'default')
        self.store.update(task['id'], state='completed')
        self.end_owner()
        current = self.mod.capture_origin(lambda k, d='': self.env.get(k, d), 'default')
        native = self.mod.Store(self.store.path, self.root, self.root, action='reply')
        def reply(store, origin):
            try:
                return store.reply(task['id'], origin, '', 'one successor', expected_turn=1, command_id='race')['turn']
            except (ValueError, PermissionError):
                return 'rejected'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda args: reply(*args), [(native, current), (self.store, owner)]))
        self.assertCountEqual(results, [2, 'rejected'])
        self.assertEqual((self.store.get(task['id'])['state'], self.store.get(task['id'])['turn']), ('queued', 2))

    def test_explicit_intervention_and_cancel_only_on_settled_task(self):
        task = self.start()
        owner = self.mod.capture_origin(lambda k, d='': self.env.get(k, d), 'default')
        self.store.update(task['id'], state='running', needs_session=True,
                          acpx_created=False, session_creation_attempted=False)
        self.store.intervene(task['id'], owner, 'withdraw this', 'pending')
        self.store.finish_turn(task['id'], 1, 'cancelled', 0, 'cancelled')
        self.end_owner()
        self.assertEqual(self.action('cancel', task)['task']['intervention']['status'], 'withdrawn')
        resumed = self.action('intervene', task, prompt='explicit revision', command_id='new', expected_turn=1)['task']
        self.assertEqual((resumed['turn'], resumed['state'], resumed['origin']), (2, 'queued', task['origin']))

    def test_explicit_close_reasons_named_profile_and_metadata_scope(self):
        named = self.root/'profiles/named'
        named.mkdir(parents=True)
        self.db.rename(named/'state.db')
        self.db = named/'state.db'
        self.c.hermes_home = named
        self.ctx.profile_name = 'named'
        task = self.start()
        self.row('current', profile='named')
        self.env['HERMES_SESSION_ID'] = 'current'
        for reason in ('agent_close', 'cli_close', 'tui_close'):
            with self.subTest(reason=reason):
                with closing(sqlite3.connect(self.db)) as db, db:
                    db.execute('DELETE FROM sessions WHERE id=?', ('old',))
                self.row('old', reason, 1, profile='named')
                self.assertEqual(self.action('status', task)['task']['id'], task['id'])
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE sessions SET profile_name='foreign' WHERE id='old'")
        with self.assertRaises(PermissionError):
            self.action('status', task)
        self.assertEqual(self.c.handle({'action': 'list'})['tasks'], [])

if __name__ == '__main__':
    unittest.main()
