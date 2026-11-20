import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import collab
from bridge import Origin, Store
from collab import Collaboration


def _git(cwd, *args):
    return subprocess.run(
        ['git', '-C', str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repo(repo_dir):
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ['git', 'init', '-b', 'main', str(repo_dir)],
        capture_output=True,
        text=True,
        check=True,
    )
    _git(repo_dir, 'config', 'user.name', 'Test User')
    _git(repo_dir, 'config', 'user.email', 'test@example.com')
    (repo_dir / 'app.txt').write_text('base v1\n', encoding='utf-8')
    pkg = repo_dir / 'packages' / 'app'
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / 'README.md').write_text('app readme v1\n', encoding='utf-8')
    _git(repo_dir, 'add', 'app.txt', 'packages/app/README.md')
    _git(repo_dir, 'commit', '-m', 'initial commit')
    return _git(repo_dir, 'rev-parse', 'HEAD').stdout.strip()


class StoreWorktreeIntegrationTests(unittest.TestCase):
    """Empirical proof of unmodified bridge.Store and collab.Collaboration with sibling worktrees."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_root = Path(self.tmp.name).resolve()
        self.repo_root = self.tmp_root / 'repo'
        self.initial_commit = _init_repo(self.repo_root)

        # Uncommitted dirty files in main repo must stay isolated from worktrees.
        (self.repo_root / 'dirty.txt').write_text('untracked dirty\n', encoding='utf-8')
        (self.repo_root / 'app.txt').write_text('dirty edit in main\n', encoding='utf-8')

        self.wt_a = self.tmp_root / 'wt-a'
        self.wt_b = self.tmp_root / 'wt-b'
        _git(self.repo_root, 'worktree', 'add', '-b', 'branch-a', str(self.wt_a), 'HEAD')
        _git(self.repo_root, 'worktree', 'add', '-b', 'branch-b', str(self.wt_b), 'HEAD')

        self.store = Store(self.tmp_root / 'tasks.sqlite3')
        self.origin_a = Origin('default', 'sess-a', 'telegram:chat:topic-a', 'telegram', 'chat', 'topic-a')
        self.origin_b = Origin('default', 'sess-b', 'telegram:chat:topic-b', 'telegram', 'chat', 'topic-b')

    def test_sibling_outside_repo_worktrees_allow_concurrent_writers(self):
        t_a = self.store.create(self.origin_a, 'local', 'codex', str(self.wt_a), 'edit a', True)
        t_b = self.store.create(self.origin_b, 'local', 'codex', str(self.wt_b), 'edit b', True)
        self.assertEqual(t_a['cwd'], str(self.wt_a))
        self.assertEqual(t_b['cwd'], str(self.wt_b))

        sub_store = Store(self.tmp_root / 'tasks-subdir.sqlite3')
        s_a = sub_store.create(
            self.origin_a, 'local', 'codex', str(self.wt_a / 'packages' / 'app'), 'edit sub a', True
        )
        s_b = sub_store.create(
            self.origin_b, 'local', 'codex', str(self.wt_b / 'packages' / 'app'), 'edit sub b', True
        )
        self.assertEqual(s_a['cwd'], str(self.wt_a / 'packages' / 'app'))
        self.assertEqual(s_b['cwd'], str(self.wt_b / 'packages' / 'app'))

        mixed_store = Store(self.tmp_root / 'tasks-mixed.sqlite3')
        m_a = mixed_store.create(self.origin_a, 'local', 'codex', str(self.wt_a), 'root a', True)
        m_b = mixed_store.create(
            self.origin_b, 'local', 'codex', str(self.wt_b / 'packages' / 'app'), 'sub b', True
        )
        self.assertEqual((m_a['cwd'], m_b['cwd']), (str(self.wt_a), str(self.wt_b / 'packages' / 'app')))

    def test_same_worktree_and_parent_child_subdirs_block_concurrent_writers(self):
        first = self.store.create(self.origin_a, 'local', 'codex', str(self.wt_a), 'owner a', True)
        with self.assertRaisesRegex(ValueError, f"Workspace is already owned by task {first['id']}"):
            self.store.create(self.origin_b, 'local', 'codex', str(self.wt_a), 'dup same', True)

        with self.assertRaisesRegex(ValueError, f"Workspace is already owned by task {first['id']}"):
            self.store.create(
                self.origin_b, 'local', 'codex', str(self.wt_a / 'packages' / 'app'), 'child blocked', True
            )

        child_store = Store(self.tmp_root / 'tasks-child-first.sqlite3')
        child = child_store.create(
            self.origin_a, 'local', 'codex', str(self.wt_a / 'packages' / 'app'), 'child owner', True
        )
        with self.assertRaisesRegex(ValueError, f"Workspace is already owned by task {child['id']}"):
            child_store.create(self.origin_b, 'local', 'codex', str(self.wt_a), 'parent blocked', True)

    def test_in_repo_worktree_collides_with_repo_root_in_both_directions(self):
        in_repo_wt = self.repo_root / '.agents' / 'worktrees' / 'sub'
        _git(self.repo_root, 'worktree', 'add', '-b', 'in-repo-sub', str(in_repo_wt), 'HEAD')

        repo_task = self.store.create(self.origin_a, 'local', 'codex', str(self.repo_root), 'repo root', True)
        with self.assertRaisesRegex(ValueError, f"Workspace is already owned by task {repo_task['id']}"):
            self.store.create(self.origin_b, 'local', 'codex', str(in_repo_wt), 'in-repo blocked', True)

        # Outside-repo sibling worktree does not conflict with repo_root writer.
        sibling_task = self.store.create(self.origin_b, 'local', 'codex', str(self.wt_a), 'sibling ok', True)
        self.assertEqual(sibling_task['cwd'], str(self.wt_a))

        rev_store = Store(self.tmp_root / 'tasks-inrepo-first.sqlite3')
        inrepo_task = rev_store.create(self.origin_a, 'local', 'codex', str(in_repo_wt), 'in-repo owner', True)
        with self.assertRaisesRegex(ValueError, f"Workspace is already owned by task {inrepo_task['id']}"):
            rev_store.create(self.origin_b, 'local', 'codex', str(self.repo_root), 'repo root blocked', True)

    def test_write_false_relay_coexists_with_writer_in_either_order(self):
        writer = self.store.create(self.origin_a, 'local', 'codex', str(self.wt_a), 'writer', True)
        relay_root = self.store.create(self.origin_b, 'local', 'omp', str(self.wt_a), 'inspect root', False)
        relay_sub = self.store.create(
            self.origin_b, 'local', 'omp', str(self.wt_a / 'packages' / 'app'), 'inspect sub', False
        )
        self.assertTrue(writer['write'])
        self.assertFalse(relay_root['write'])
        self.assertFalse(relay_sub['write'])

        relay_first = self.store.create(self.origin_a, 'local', 'omp', str(self.wt_b), 'inspect first', False)
        writer_after = self.store.create(self.origin_b, 'local', 'codex', str(self.wt_b), 'writer after', True)
        relay_second = self.store.create(self.origin_a, 'local', 'omp', str(self.wt_b), 'inspect second', False)
        self.assertFalse(relay_first['write'])
        self.assertTrue(writer_after['write'])
        self.assertFalse(relay_second['write'])
        with self.assertRaisesRegex(ValueError, f"Workspace is already owned by task {writer_after['id']}"):
            self.store.create(self.origin_a, 'local', 'codex', str(self.wt_b), 'second writer blocked', True)

    def test_serial_continuation_and_reuse_on_same_sibling_worktree(self):
        t1 = self.store.create(self.origin_a, 'local', 'codex', str(self.wt_a), 'turn 1', True)
        self.store.update(t1['id'], state='running')
        self.store.finish_turn(t1['id'], 1, 'completed', 0, 'end_turn')

        # New task can reuse wt_a once t1 is completed.
        t2 = self.store.create(self.origin_b, 'local', 'codex', str(self.wt_a), 'new task after finish', True)
        self.assertEqual(t2['cwd'], str(self.wt_a))
        self.store.update(t2['id'], state='running')
        self.store.finish_turn(t2['id'], 1, 'completed', 0, 'end_turn')
        self.store.begin_close(t2['id'], self.origin_b)
        with self.assertRaisesRegex(ValueError, f"Workspace is already owned by task {t2['id']}"):
            self.store.create(self.origin_a, 'local', 'codex', str(self.wt_a), 'blocked while closing', True)
        self.assertTrue(self.store.finish_close(t2['id'], 1, True))

        # New task after close succeeds, then tests same-task reply & intervene continuations.
        t3 = self.store.create(self.origin_a, 'local', 'codex', str(self.wt_a), 'reused task', True)
        self.store.update(t3['id'], state='running')
        self.assertTrue(self.store.event(t3['id'], '1:q', 'question', 'Q1 Target file?', turn=1))
        self.store.finish_turn(t3['id'], 1, 'completed', 0, 'end_turn')
        self.assertEqual(self.store.get(t3['id'])['state'], 'waiting_reply')
        with self.assertRaisesRegex(ValueError, f"Workspace is already owned by task {t3['id']}"):
            self.store.create(self.origin_b, 'local', 'codex', str(self.wt_a), 'blocked during wait_reply', True)

        replied = self.store.reply(
            t3['id'], self.origin_a, '1:q', 'app.txt', expected_turn=1, command_id='reply-1'
        )
        self.assertEqual((replied['turn'], replied['state'], replied['cwd']), (2, 'queued', str(self.wt_a)))
        with self.assertRaisesRegex(ValueError, f"Workspace is already owned by task {t3['id']}"):
            self.store.create(self.origin_b, 'local', 'codex', str(self.wt_a), 'blocked after reply', True)

        # Intervene after completed.
        self.store.update(t3['id'], state='running')
        self.store.finish_turn(t3['id'], 2, 'completed', 0, 'end_turn')
        resumed, launch = self.store.intervene(
            t3['id'], self.origin_a, 'follow up after completed', command_id='iv-1', expected_turn=2
        )
        self.assertTrue(launch)
        self.assertEqual((resumed['turn'], resumed['state'], resumed['cwd']), (3, 'queued', str(self.wt_a)))
        with self.assertRaisesRegex(ValueError, f"Workspace is already owned by task {t3['id']}"):
            self.store.create(
                self.origin_b, 'local', 'codex', str(self.wt_a / 'packages' / 'app'), 'blocked after iv1', True
            )

        # Intervene after cancelled.
        self.store.update(t3['id'], state='running')
        self.store.finish_turn(t3['id'], 3, 'cancelled', 0, 'cancelled')
        resumed2, launch2 = self.store.intervene(
            t3['id'], self.origin_a, 'retry after cancel', command_id='iv-2', expected_turn=3
        )
        self.assertTrue(launch2)
        self.assertEqual((resumed2['turn'], resumed2['state'], resumed2['cwd']), (4, 'queued', str(self.wt_a)))
        with self.assertRaisesRegex(ValueError, f"Workspace is already owned by task {t3['id']}"):
            self.store.create(self.origin_b, 'local', 'codex', str(self.wt_a), 'blocked after iv2', True)

    def test_dirty_files_in_main_repo_not_inherited_by_sibling_worktrees(self):
        self.assertTrue((self.repo_root / 'dirty.txt').exists())
        self.assertEqual((self.repo_root / 'app.txt').read_text(encoding='utf-8'), 'dirty edit in main\n')
        for wt in (self.wt_a, self.wt_b):
            self.assertFalse((wt / 'dirty.txt').exists())
            self.assertEqual((wt / 'app.txt').read_text(encoding='utf-8'), 'base v1\n')
            self.assertEqual(
                (wt / 'packages' / 'app' / 'README.md').read_text(encoding='utf-8'),
                'app readme v1\n',
            )

    def test_collaboration_start_preserves_subdir_path_and_writer_locks(self):
        launched = []
        collab = Collaboration(
            self.tmp_root / 'collab.sqlite3',
            launch=lambda task: launched.append(task.copy()),
        )
        client_a = collab.connect('hermes')['client_id']
        client_b = collab.connect('codex')['client_id']

        sub_a = str(self.wt_a / 'packages' / 'app')
        sub_b = str(self.wt_b / 'packages' / 'app')
        task_a = collab.start(client_a, prompt='work in wt-a subdir', cwd=sub_a, agent='codex', write=True)
        task_b = collab.start(client_b, prompt='work in wt-b subdir', cwd=sub_b, agent='codex', write=True)

        self.assertEqual(len(launched), 2)
        self.assertEqual(task_a['cwd'], sub_a)
        self.assertEqual(task_a['runtime_links']['cwd'], sub_a)
        self.assertEqual(task_b['cwd'], sub_b)
        self.assertEqual(task_b['runtime_links']['cwd'], sub_b)

        with self.assertRaisesRegex(ValueError, f"Workspace is already owned by task {task_a['id']}"):
            collab.start(client_b, prompt='parent blocked', cwd=str(self.wt_a), agent='codex', write=True)
        with self.assertRaisesRegex(ValueError, f"Workspace is already owned by task {task_a['id']}"):
            collab.start(client_b, prompt='same subdir blocked', cwd=sub_a, agent='codex', write=True)

        relay = collab.start(client_b, prompt='read-only check', cwd=sub_a, agent='codex', write=False)
        self.assertEqual(relay['cwd'], sub_a)
        self.assertFalse(relay['write'])


class GitWorktreeHelperTests(unittest.TestCase):
    """Tests for the thin pure-stdlib git_worktree.py helper and CLI."""

    def setUp(self):
        import git_worktree  # Imported lazily so RED run shows Store tests PASS and helper tests FAIL RED.

        self.gw = git_worktree
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_root = Path(self.tmp.name).resolve()
        self.repo_root = self.tmp_root / 'repo'
        self.initial_commit = _init_repo(self.repo_root)

    def test_prepare_sibling_worktree_subdir_and_fixed_base_commit(self):
        wt_dir = self.tmp_root / 'wt-feat'
        info = self.gw.prepare_sibling_worktree(
            self.repo_root,
            wt_dir,
            branch='feat/alpha',
            start_point='main',
            target_subdir='packages/app',
            device='local',
        )
        self.assertEqual(info['managed_by'], 'Hermes')
        self.assertEqual(info['device'], 'local')
        self.assertEqual(info['repo_root'], str(self.repo_root))
        self.assertEqual(info['worktree_root'], str(wt_dir))
        self.assertEqual(info['cwd'], str(wt_dir / 'packages' / 'app'))
        self.assertEqual(info['relative_subdir'], 'packages/app')
        self.assertEqual(info['branch'], 'feat/alpha')
        self.assertEqual(info['base_commit'], self.initial_commit)
        self.assertFalse(info['reused'])

        # Advancing main later must not change the recorded fixed base_commit or wt HEAD.
        (self.repo_root / 'app.txt').write_text('main advanced\n', encoding='utf-8')
        _git(self.repo_root, 'add', 'app.txt')
        _git(self.repo_root, 'commit', '-m', 'advance main')
        new_main_head = _git(self.repo_root, 'rev-parse', 'HEAD').stdout.strip()
        wt_head = _git(wt_dir, 'rev-parse', 'HEAD').stdout.strip()
        self.assertNotEqual(new_main_head, self.initial_commit)
        self.assertEqual(wt_head, info['base_commit'])

    def test_prepare_rejects_in_repo_equal_and_parent_paths_fail_closed(self):
        bad_targets = [
            self.repo_root,
            self.repo_root / '.agents' / 'worktrees' / 'x',
            self.repo_root.parent,
        ]
        for idx, bad_dir in enumerate(bad_targets):
            with self.subTest(bad_dir=str(bad_dir)):
                with self.assertRaises(self.gw.WorktreeError):
                    self.gw.prepare_sibling_worktree(
                        self.repo_root,
                        bad_dir,
                        branch=f'bad-{idx}',
                    )
        self.assertFalse((self.repo_root / '.agents').exists())

    def test_prepare_rejects_invalid_target_subdir_without_fallback(self):
        for idx, bad_sub in enumerate([
            '/tmp/absolute',
            '../outside',
            'packages/../../outside',
            'packages/missing',
            'app.txt',
        ]):
            wt_dir = self.tmp_root / f'wt-bad-sub-{idx}'
            with self.subTest(target_subdir=bad_sub):
                with self.assertRaises(self.gw.WorktreeError):
                    self.gw.prepare_sibling_worktree(
                        self.repo_root,
                        wt_dir,
                        branch=f'bad-sub-{idx}',
                        target_subdir=bad_sub,
                    )
                self.assertFalse(wt_dir.exists())

    def test_prepare_reuse_semantics_and_invalid_inputs(self):
        wt_dir = self.tmp_root / 'wt-reuse'
        created = self.gw.prepare_sibling_worktree(
            self.repo_root,
            wt_dir,
            branch='feat/reuse',
            target_subdir='packages/app',
        )
        self.assertFalse(created['reused'])

        # reuse=False must reject existing worktree directory.
        with self.assertRaises(self.gw.WorktreeError):
            self.gw.prepare_sibling_worktree(self.repo_root, wt_dir, branch='feat/other', reuse=False)

        # reuse=True succeeds when repo and branch match.
        reused = self.gw.prepare_sibling_worktree(
            self.repo_root,
            wt_dir,
            branch='feat/reuse',
            target_subdir='packages/app',
            reuse=True,
        )
        self.assertTrue(reused['reused'])
        self.assertEqual(reused['worktree_root'], str(wt_dir))
        self.assertEqual(reused['cwd'], str(wt_dir / 'packages' / 'app'))
        self.assertEqual(reused['base_commit'], self.initial_commit)

        # reuse=True rejects wrong branch.
        with self.assertRaises(self.gw.WorktreeError):
            self.gw.prepare_sibling_worktree(self.repo_root, wt_dir, branch='feat/wrong', reuse=True)

        # reuse=True rejects plain non-worktree directory and nonexistent directory.
        plain_dir = self.tmp_root / 'plain-dir'
        plain_dir.mkdir()
        with self.assertRaises(self.gw.WorktreeError):
            self.gw.prepare_sibling_worktree(self.repo_root, plain_dir, branch='feat/reuse', reuse=True)
        with self.assertRaises(self.gw.WorktreeError):
            self.gw.prepare_sibling_worktree(
                self.repo_root, self.tmp_root / 'missing-wt', branch='feat/reuse', reuse=True
            )

        # reuse=True rejects a worktree belonging to a different Git repo.
        other_repo = self.tmp_root / 'other-repo'
        _init_repo(other_repo)
        other_wt = self.tmp_root / 'other-wt'
        self.gw.prepare_sibling_worktree(other_repo, other_wt, branch='feat/reuse')
        with self.assertRaises(self.gw.WorktreeError):
            self.gw.prepare_sibling_worktree(self.repo_root, other_wt, branch='feat/reuse', reuse=True)

        # Invalid start_point and duplicate branch fail closed.
        with self.assertRaises(self.gw.WorktreeError):
            self.gw.prepare_sibling_worktree(
                self.repo_root, self.tmp_root / 'wt-bad-start', branch='feat/bad-start', start_point='no-such-ref'
            )
        with self.assertRaises(self.gw.WorktreeError):
            self.gw.prepare_sibling_worktree(
                self.repo_root, self.tmp_root / 'wt-dup-branch', branch='feat/reuse'
            )

    def test_inspect_worktree_state_distinguishes_all_four_states_and_target_ref(self):
        _git(self.repo_root, 'branch', 'integration-branch', 'main')
        wt_dir = self.tmp_root / 'wt-inspect'
        prep = self.gw.prepare_sibling_worktree(self.repo_root, wt_dir, branch='feat/inspect', start_point='main')
        base = prep['base_commit']

        # Clean baseline with target_ref=None.
        s0 = self.gw.inspect_worktree_state(wt_dir, base_commit=base)
        self.assertEqual(s0['uncommitted_files'], [])
        self.assertEqual(s0['branch_commits'], [])
        self.assertIsNone(s0['integrated'])
        self.assertFalse(s0['verified'])

        # Modified tracked file + untracked file: preserve leading spaces in porcelain lines.
        (wt_dir / 'app.txt').write_text('modified in wt\n', encoding='utf-8')
        (wt_dir / 'notes.txt').write_text('untracked in wt\n', encoding='utf-8')
        s_dirty = self.gw.inspect_worktree_state(wt_dir, base_commit=base, target_ref='main')
        self.assertIn(' M app.txt', s_dirty['uncommitted_files'])
        self.assertIn('?? notes.txt', s_dirty['uncommitted_files'])
        self.assertEqual(s_dirty['branch_commits'], [])
        self.assertFalse(s_dirty['integrated'])
        self.assertFalse(s_dirty['verified'])

        # Commit app.txt and add a second commit; clean tree, unmerged into main/integration-branch.
        (wt_dir / 'notes.txt').unlink()
        _git(wt_dir, 'add', 'app.txt')
        _git(wt_dir, 'commit', '-m', 'commit 1')
        c1 = _git(wt_dir, 'rev-parse', 'HEAD').stdout.strip()
        (wt_dir / 'extra.txt').write_text('second\n', encoding='utf-8')
        _git(wt_dir, 'add', 'extra.txt')
        _git(wt_dir, 'commit', '-m', 'commit 2')
        c2 = _git(wt_dir, 'rev-parse', 'HEAD').stdout.strip()

        s_committed = self.gw.inspect_worktree_state(wt_dir, base_commit=base, target_ref='main')
        self.assertEqual(s_committed['uncommitted_files'], [])
        self.assertEqual(s_committed['branch_commits'], [c1, c2])
        self.assertFalse(s_committed['integrated'])
        self.assertFalse(s_committed['verified'])

        # Merge into integration-branch (not main), then dirty the worktree -> integrated must still be False.
        _git(self.repo_root, 'checkout', 'integration-branch')
        _git(self.repo_root, 'merge', '--no-ff', 'feat/inspect', '-m', 'merge into integration-branch')
        _git(self.repo_root, 'checkout', 'main')

        (wt_dir / 'stray.txt').write_text('dirty after merge\n', encoding='utf-8')
        s_merged_dirty = self.gw.inspect_worktree_state(
            wt_dir, base_commit=base, target_ref='integration-branch'
        )
        self.assertEqual(s_merged_dirty['uncommitted_files'], ['?? stray.txt'])
        self.assertEqual(s_merged_dirty['branch_commits'], [c1, c2])
        self.assertFalse(s_merged_dirty['integrated'])
        self.assertFalse(s_merged_dirty['verified'])

        # Remove stray dirty file: integrated is True against integration-branch, False against main.
        (wt_dir / 'stray.txt').unlink()
        s_int = self.gw.inspect_worktree_state(wt_dir, base_commit=base, target_ref='integration-branch')
        s_main = self.gw.inspect_worktree_state(wt_dir, base_commit=base, target_ref='main')
        self.assertTrue(s_int['integrated'])
        self.assertFalse(s_main['integrated'])
        self.assertFalse(s_int['verified'])

        # Explicit verified flag is echoed without inferring from integration/commits.
        s_verified = self.gw.inspect_worktree_state(
            wt_dir, base_commit=base, target_ref='integration-branch', verified=True
        )
        self.assertTrue(s_verified['integrated'])
        self.assertTrue(s_verified['verified'])

    def test_verify_cleanup_safe_gates_and_non_force_execution(self):
        _git(self.repo_root, 'branch', 'integration-branch', 'main')
        wt_dir = self.tmp_root / 'wt-clean'
        self.gw.prepare_sibling_worktree(self.repo_root, wt_dir, branch='feat/cleanup', start_point='main')

        # Main worktree is always blocked.
        main_res = self.gw.verify_cleanup_safe(self.repo_root, target_ref='main')
        self.assertNotIn('default_retain', main_res)
        self.assertFalse(main_res['safe'])
        self.assertEqual(main_res['disposition'], 'retained_with_reason')
        self.assertEqual(main_res['retain_reasons'], main_res['reasons'])
        self.assertIsNone(main_res['command'])
        self.assertTrue(any('main' in r.lower() for r in main_res['reasons']))

        # Missing/invalid target_ref blocks cleanup.
        for bad_ref in (None, '', 'nonexistent-ref'):
            res = self.gw.verify_cleanup_safe(wt_dir, target_ref=bad_ref)
            self.assertNotIn('default_retain', res)
            self.assertFalse(res['safe'])
            self.assertEqual(res['disposition'], 'retained_with_reason')
            self.assertIsNone(res['command'])
            self.assertTrue(res['retain_reasons'])

        # Active writer blocks cleanup even when clean and merged.
        writer_res = self.gw.verify_cleanup_safe(wt_dir, target_ref='main', has_active_writer=True)
        self.assertNotIn('default_retain', writer_res)
        self.assertFalse(writer_res['safe'])
        self.assertEqual(writer_res['disposition'], 'retained_with_reason')
        self.assertIsNone(writer_res['command'])
        self.assertTrue(any('writer' in r.lower() for r in writer_res['reasons']))

        # Uncommitted dirty file blocks cleanup.
        (wt_dir / 'app.txt').write_text('dirty change\n', encoding='utf-8')
        dirty_res = self.gw.verify_cleanup_safe(wt_dir, target_ref='main')
        self.assertFalse(dirty_res['safe'])
        self.assertEqual(dirty_res['disposition'], 'retained_with_reason')
        self.assertIsNone(dirty_res['command'])
        self.assertTrue(dirty_res['retain_reasons'])

        # Commit change on branch (clean tree, unmerged into integration-branch or main).
        _git(wt_dir, 'add', 'app.txt')
        _git(wt_dir, 'commit', '-m', 'cleanup feature commit')
        unmerged_res = self.gw.verify_cleanup_safe(wt_dir, target_ref='integration-branch')
        self.assertFalse(unmerged_res['safe'])
        self.assertEqual(unmerged_res['disposition'], 'retained_with_reason')
        self.assertIsNone(unmerged_res['command'])
        self.assertTrue(unmerged_res['retain_reasons'])

        # Merge into integration-branch only: blocked against main, blocked if unverified, allowed when verified.
        _git(self.repo_root, 'checkout', 'integration-branch')
        _git(self.repo_root, 'merge', '--no-ff', 'feat/cleanup', '-m', 'merge cleanup branch')
        _git(self.repo_root, 'checkout', 'main')

        main_blocked = self.gw.verify_cleanup_safe(wt_dir, target_ref='main')
        self.assertFalse(main_blocked['safe'])
        self.assertEqual(main_blocked['disposition'], 'retained_with_reason')
        self.assertIsNone(main_blocked['command'])

        unverified_res = self.gw.verify_cleanup_safe(wt_dir, target_ref='integration-branch', verified=False)
        self.assertFalse(unverified_res['safe'])
        self.assertEqual(unverified_res['disposition'], 'retained_with_reason')
        self.assertIsNone(unverified_res['command'])
        self.assertTrue(any('verif' in r.lower() for r in unverified_res['retain_reasons']))

        safe_res = self.gw.verify_cleanup_safe(wt_dir, target_ref='integration-branch')
        self.assertNotIn('default_retain', safe_res)
        self.assertTrue(safe_res['safe'])
        self.assertEqual(safe_res['disposition'], 'ready_to_clean')
        self.assertEqual(safe_res['reasons'], [])
        self.assertEqual(safe_res['retain_reasons'], [])
        self.assertEqual(
            safe_res['command'],
            ['git', '-C', str(self.repo_root), 'worktree', 'remove', str(wt_dir)],
        )
        self.assertNotIn('--force', safe_res['command'])
        self.assertNotIn('-f', safe_res['command'])
        self.assertIsNotNone(safe_res['branch_cleanup_command'])
        self.assertNotIn('-D', safe_res['branch_cleanup_command'])
        self.assertNotIn('--force', safe_res['branch_cleanup_command'])

        # Executing the emitted non-force command cleanly removes the worktree.
        subprocess.run(safe_res['command'], capture_output=True, text=True, check=True)
        self.assertFalse(wt_dir.exists())

    def test_cli_smoke_prepare_inspect_check_cleanup_and_errors(self):
        script = str(Path(__file__).with_name('git_worktree.py'))
        wt_dir = self.tmp_root / 'wt-cli'

        prep_proc = subprocess.run(
            [
                sys.executable, '-B', script, 'prepare',
                '--repo', str(self.repo_root),
                '--worktree', str(wt_dir),
                '--branch', 'feat/cli',
                '--subdir', 'packages/app',
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        prep_data = json.loads(prep_proc.stdout)
        self.assertEqual(prep_data['managed_by'], 'Hermes')
        self.assertEqual(prep_data['cwd'], str(wt_dir / 'packages' / 'app'))
        self.assertFalse(prep_data['reused'])

        (wt_dir / 'cli.txt').write_text('cli commit\n', encoding='utf-8')
        _git(wt_dir, 'add', 'cli.txt')
        _git(wt_dir, 'commit', '-m', 'cli commit')

        inspect_proc = subprocess.run(
            [
                sys.executable, '-B', script, 'inspect',
                '--worktree', str(wt_dir),
                '--base-commit', prep_data['base_commit'],
                '--target-ref', 'main',
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        inspect_data = json.loads(inspect_proc.stdout)
        self.assertEqual(len(inspect_data['branch_commits']), 1)
        self.assertFalse(inspect_data['integrated'])
        self.assertFalse(inspect_data['verified'])

        cleanup_blocked = subprocess.run(
            [
                sys.executable, '-B', script, 'check-cleanup',
                '--worktree', str(wt_dir),
                '--target-ref', 'main',
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        cleanup_data = json.loads(cleanup_blocked.stdout)
        self.assertNotIn('default_retain', cleanup_data)
        self.assertEqual(cleanup_data['disposition'], 'retained_with_reason')
        self.assertTrue(cleanup_data['retain_reasons'])
        self.assertFalse(cleanup_data['safe'])
        self.assertIsNone(cleanup_data['command'])

        # WorktreeError returns non-zero exit with valid JSON on stderr.
        err_proc = subprocess.run(
            [
                sys.executable, '-B', script, 'prepare',
                '--repo', str(self.repo_root),
                '--worktree', str(self.repo_root / '.agents' / 'worktrees' / 'bad'),
                '--branch', 'feat/bad',
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(err_proc.returncode, 0)
        err_data = json.loads(err_proc.stderr)
        self.assertEqual(err_data['error'], 'WorktreeError')
        self.assertTrue(err_data['message'])

    def test_plain_dir_workflow_and_scoped_init_task_repo(self):
        # 1. Plain directory executes directly via collab without forcing Git creation.
        plain_dir = self.tmp_root / 'plain-task'
        plain_dir.mkdir()
        (plain_dir / 'input.txt').write_text('hello plain\n', encoding='utf-8')
        db_path = self.tmp_root / 'plain.sqlite3'

        def fake_launch(task):
            (Path(task['cwd']) / 'output.txt').write_text('done plain\n', encoding='utf-8')
            c_instance.store.update(task['id'], state='running')
            c_instance.store.finish_turn(task['id'], task['turn'], 'completed', 0, 'end_turn')

        c_instance = Collaboration(db_path, launch=fake_launch)

        def invoke_collab(*argv):
            buf = io.StringIO()
            with redirect_stdout(buf), patch.object(collab, 'Collaboration', return_value=c_instance):
                rc = collab.main(['--db', str(db_path), *argv])
            return rc, json.loads(buf.getvalue())

        rc, cli = invoke_collab('connect', '--runtime', 'hermes')
        self.assertEqual(rc, 0)
        rc, task = invoke_collab(
            '--client', cli['client_id'], 'start',
            '--cwd', str(plain_dir),
            '--prompt', 'write plain output',
            '--agent', 'codex',
            '--write',
        )
        self.assertEqual(rc, 0)
        self.assertEqual(task['state'], 'completed')
        self.assertFalse((plain_dir / '.git').exists())
        self.assertEqual((plain_dir / 'output.txt').read_text(encoding='utf-8'), 'done plain\n')

        # 2. init_task_repo refuses host roots and existing Git repos fail-closed.
        for bad_root in (Path('/'), Path.home(), Path('/tmp')):
            if bad_root.exists():
                with self.subTest(bad_root=str(bad_root)):
                    with self.assertRaises(self.gw.WorktreeError):
                        self.gw.init_task_repo(bad_root)
        with self.assertRaises(self.gw.WorktreeError):
            self.gw.init_task_repo(self.repo_root)
        with self.assertRaises(self.gw.WorktreeError):
            self.gw.init_task_repo(self.repo_root / 'packages' / 'app')

        # 3. Scoped init_task_repo on populated plain dir skips noise and enables prepare_sibling_worktree.
        (plain_dir / '__pycache__').mkdir()
        (plain_dir / '__pycache__' / 'x.pyc').write_bytes(b'pyc')
        (plain_dir / '.pytest_cache').mkdir()
        (plain_dir / '.pytest_cache' / 'v').write_text('cache', encoding='utf-8')
        (plain_dir / '.DS_Store').write_bytes(b'ds')
        init_res = self.gw.init_task_repo(plain_dir, branch='main')
        self.assertTrue((plain_dir / '.git').is_dir())
        self.assertEqual(init_res['branch'], 'main')
        self.assertTrue(init_res['base_commit'])
        tracked = _git(plain_dir, 'ls-files').stdout.splitlines()
        self.assertIn('input.txt', tracked)
        self.assertIn('output.txt', tracked)
        self.assertFalse(any('__pycache__' in p or '.pytest_cache' in p or p.endswith('.DS_Store') for p in tracked))

        wt_from_init = self.tmp_root / 'wt-from-init'
        prep = self.gw.prepare_sibling_worktree(plain_dir, wt_from_init, branch='feat/init')
        self.assertEqual(prep['base_commit'], init_res['base_commit'])

        # 4. Scoped init_task_repo on empty directory succeeds with initial commit.
        empty_dir = self.tmp_root / 'empty-task'
        empty_dir.mkdir()
        empty_res = self.gw.init_task_repo(empty_dir)
        self.assertTrue(empty_res['base_commit'])
        self.assertEqual(_git(empty_dir, 'rev-parse', 'HEAD').stdout.strip(), empty_res['base_commit'])

    def test_single_repo_preserves_user_changes_and_forbids_harvest_on_main(self):
        (self.repo_root / 'user_wip.txt').write_text('pre-existing user draft\n', encoding='utf-8')
        (self.repo_root / 'packages' / 'app' / 'README.md').write_text('single repo update\n', encoding='utf-8')

        # Main repo must never be harvested as a slice worktree.
        with self.assertRaises(self.gw.WorktreeError):
            self.gw.harvest_worktree_deliverables(self.repo_root)
        self.assertEqual((self.repo_root / 'user_wip.txt').read_text(encoding='utf-8'), 'pre-existing user draft\n')
        self.assertEqual(
            (self.repo_root / 'packages' / 'app' / 'README.md').read_text(encoding='utf-8'),
            'single repo update\n',
        )

    def test_harvest_worktree_deliverables_commits_dirty_and_untracked_filters_noise_and_checks_scope(self):
        wt_dir = self.tmp_root / 'wt-harvest'
        prep = self.gw.prepare_sibling_worktree(self.repo_root, wt_dir, branch='feat/harvest')

        # Create modified tracked file, deleted tracked file, spaced Unicode untracked file, and noise files.
        (wt_dir / 'app.txt').write_text('updated app\n', encoding='utf-8')
        (wt_dir / 'packages' / 'app' / 'README.md').unlink()
        unicode_name = 'notes 报告.txt'
        (wt_dir / unicode_name).write_text('unicode content\n', encoding='utf-8')
        (wt_dir / '__pycache__').mkdir()
        (wt_dir / '__pycache__' / 'cache.pyc').write_bytes(b'pyc')
        (wt_dir / '.pytest_cache').mkdir()
        (wt_dir / '.pytest_cache' / 'state').write_text('cache', encoding='utf-8')
        (wt_dir / '.DS_Store').write_bytes(b'ds')

        # Out-of-scope allowed_paths rejects before staging or committing anything.
        head_before = _git(wt_dir, 'rev-parse', 'HEAD').stdout.strip()
        with self.assertRaises(self.gw.WorktreeError):
            self.gw.harvest_worktree_deliverables(
                wt_dir,
                allowed_paths=['app.txt'],
                base_commit=prep['base_commit'],
            )
        self.assertEqual(_git(wt_dir, 'rev-parse', 'HEAD').stdout.strip(), head_before)
        self.assertEqual(_git(wt_dir, 'diff', '--cached', '--name-only').stdout.strip(), '')

        res = self.gw.harvest_worktree_deliverables(
            wt_dir,
            allowed_paths=['app.txt', 'packages/app/README.md', unicode_name],
            base_commit=prep['base_commit'],
        )
        self.assertTrue(res['harvested'])
        self.assertIsNotNone(res['commit'])
        self.assertEqual(set(res['committed_files']), {'app.txt', 'packages/app/README.md', unicode_name})
        self.assertEqual(
            set(res['skipped_noise_files']),
            {'__pycache__/cache.pyc', '.pytest_cache/state', '.DS_Store'},
        )
        tree_files = [
            p for p in _git(wt_dir, 'ls-tree', '-r', '--name-only', '-z', 'HEAD').stdout.split('\0') if p
        ]
        self.assertIn('app.txt', tree_files)
        self.assertIn(unicode_name, tree_files)
        self.assertNotIn('packages/app/README.md', tree_files)
        self.assertFalse(any(p.endswith('.pyc') or p.endswith('.DS_Store') or '.pytest_cache' in p for p in tree_files))

        # Second call with only noise remaining is a clean no-op without an empty commit.
        res2 = self.gw.harvest_worktree_deliverables(wt_dir)
        self.assertFalse(res2['harvested'])
        self.assertIsNone(res2['commit'])
        self.assertEqual(_git(wt_dir, 'rev-parse', 'HEAD').stdout.strip(), res['commit'])

    def test_parallel_worktrees_serial_converge_verify_cleanup_preserving_unrelated_user_dirty(self):
        # Unrelated user dirty & untracked files in target repo must survive convergence untouched.
        (self.repo_root / 'user_notes.txt').write_text('unrelated dirty edit\n', encoding='utf-8')
        _git(self.repo_root, 'add', 'user_notes.txt')
        _git(self.repo_root, 'commit', '-m', 'add user_notes')
        (self.repo_root / 'user_notes.txt').write_text('user local wip\n', encoding='utf-8')
        (self.repo_root / 'scratch.txt').write_text('untracked user scratch\n', encoding='utf-8')

        wt_a = self.tmp_root / 'wt-a'
        wt_b = self.tmp_root / 'wt-b'
        prep_a = self.gw.prepare_sibling_worktree(self.repo_root, wt_a, branch='slice/a')
        prep_b = self.gw.prepare_sibling_worktree(self.repo_root, wt_b, branch='slice/b')

        # Worker A leaves modified + untracked uncommitted deliverables.
        (wt_a / 'app.txt').write_text('from worker a\n', encoding='utf-8')
        (wt_a / 'feature_a.py').write_text('VALUE_A = 1\n', encoding='utf-8')

        # Worker B commits one file and leaves another untracked.
        (wt_b / 'feature_b1.py').write_text('VALUE_B1 = 2\n', encoding='utf-8')
        _git(wt_b, 'add', 'feature_b1.py')
        _git(wt_b, 'commit', '-m', 'worker b commit')
        (wt_b / 'feature_b2.py').write_text('VALUE_B2 = 3\n', encoding='utf-8')

        verify_a = [
            sys.executable, '-B', '-c',
            'import pathlib; assert pathlib.Path("feature_a.py").exists(); assert "from worker a" in pathlib.Path("app.txt").read_text()',
        ]
        verify_b = [
            sys.executable, '-B', '-c',
            'import pathlib; assert pathlib.Path("feature_a.py").exists(); assert pathlib.Path("feature_b1.py").exists(); assert pathlib.Path("feature_b2.py").exists()',
        ]

        res_a = self.gw.converge_worktree_to_target(
            wt_a,
            target_dir=self.repo_root,
            base_commit=prep_a['base_commit'],
            verify_cmd=verify_a,
        )
        res_b = self.gw.converge_worktree_to_target(
            wt_b,
            target_dir=self.repo_root,
            base_commit=prep_b['base_commit'],
            verify_cmd=verify_b,
        )

        for res in (res_a, res_b):
            self.assertEqual(res['status'], 'converged')
            self.assertTrue(res['integrated'])
            self.assertTrue(res['verified'])
            self.assertTrue(res['cleaned'])
            self.assertTrue(res['branch_deleted'])
            self.assertEqual(res['disposition'], 'cleaned')
            self.assertEqual(res['retain_reasons'], [])

        self.assertEqual((self.repo_root / 'app.txt').read_text(encoding='utf-8'), 'from worker a\n')
        self.assertEqual((self.repo_root / 'feature_a.py').read_text(encoding='utf-8'), 'VALUE_A = 1\n')
        self.assertEqual((self.repo_root / 'feature_b1.py').read_text(encoding='utf-8'), 'VALUE_B1 = 2\n')
        self.assertEqual((self.repo_root / 'feature_b2.py').read_text(encoding='utf-8'), 'VALUE_B2 = 3\n')
        self.assertEqual((self.repo_root / 'user_notes.txt').read_text(encoding='utf-8'), 'user local wip\n')
        self.assertEqual((self.repo_root / 'scratch.txt').read_text(encoding='utf-8'), 'untracked user scratch\n')
        self.assertFalse(wt_a.exists())
        self.assertFalse(wt_b.exists())

        wt_list = _git(self.repo_root, 'worktree', 'list', '--porcelain').stdout
        self.assertEqual([line for line in wt_list.splitlines() if line.startswith('worktree ')], [f'worktree {self.repo_root}'])
        branches = [b.lstrip('* ').strip() for b in _git(self.repo_root, 'branch').stdout.splitlines()]
        self.assertEqual(branches, ['main'])

    def test_converge_into_non_main_integration_worktree_deletes_branch_without_force(self):
        wt_int = self.tmp_root / 'wt-int'
        self.gw.prepare_sibling_worktree(self.repo_root, wt_int, branch='integration', start_point='main')
        wt_feat = self.tmp_root / 'wt-feat'
        prep = self.gw.prepare_sibling_worktree(self.repo_root, wt_feat, branch='feat/x', start_point='integration')
        (wt_feat / 'feat_x.txt').write_text('feature x\n', encoding='utf-8')

        res = self.gw.converge_worktree_to_target(
            wt_feat,
            target_dir=wt_int,
            target_branch='integration',
            base_commit=prep['base_commit'],
            verify_cmd=[sys.executable, '-B', '-c', 'import pathlib; assert pathlib.Path("feat_x.txt").exists()'],
        )
        self.assertEqual(res['status'], 'converged')
        self.assertTrue(res['integrated'])
        self.assertTrue(res['verified'])
        self.assertTrue(res['cleaned'])
        self.assertTrue(res['branch_deleted'])
        self.assertEqual(res['disposition'], 'cleaned')
        self.assertFalse(wt_feat.exists())
        self.assertTrue((wt_int / 'feat_x.txt').exists())
        self.assertFalse((self.repo_root / 'feat_x.txt').exists())
        self.assertEqual(_git(self.repo_root, 'rev-parse', 'main').stdout.strip(), self.initial_commit)
        branches = [b.lstrip('* +').strip() for b in _git(self.repo_root, 'branch').stdout.splitlines()]
        self.assertNotIn('feat/x', branches)

    def test_converge_protects_overlapping_and_staged_target_user_changes(self):
        # Case A: Overlapping unstaged change in target_dir stops before git merge.
        wt_a = self.tmp_root / 'wt-overlap'
        prep_a = self.gw.prepare_sibling_worktree(self.repo_root, wt_a, branch='feat/overlap')
        (wt_a / 'app.txt').write_text('worker change\n', encoding='utf-8')
        (self.repo_root / 'app.txt').write_text('user local app edit\n', encoding='utf-8')

        res_overlap = self.gw.converge_worktree_to_target(
            wt_a,
            target_dir=self.repo_root,
            base_commit=prep_a['base_commit'],
        )
        self.assertEqual(res_overlap['status'], 'target_dirty_overlap')
        self.assertFalse(res_overlap['integrated'])
        self.assertFalse(res_overlap['cleaned'])
        self.assertFalse(res_overlap['merge_in_progress'])
        self.assertEqual(res_overlap['disposition'], 'retained_with_reason')
        self.assertTrue(res_overlap['escalation_required'])
        self.assertIn('app.txt', res_overlap['overlapping_target_files'])
        self.assertEqual((self.repo_root / 'app.txt').read_text(encoding='utf-8'), 'user local app edit\n')
        self.assertEqual(_git(self.repo_root, 'rev-parse', '--verify', '--quiet', 'MERGE_HEAD').returncode if False else
                         subprocess.run(['git', '-C', str(self.repo_root), 'rev-parse', '--verify', '--quiet', 'MERGE_HEAD']).returncode, 1)
        self.assertEqual(_git(self.repo_root, 'stash', 'list').stdout.strip(), '')
        self.assertTrue(wt_a.exists())

        # Case B: Pre-existing staged unrelated file in target_dir stops before git merge.
        (self.repo_root / 'app.txt').write_text('base v1\n', encoding='utf-8')
        (self.repo_root / 'staged_user.txt').write_text('staged by user\n', encoding='utf-8')
        _git(self.repo_root, 'add', 'staged_user.txt')

        res_staged = self.gw.converge_worktree_to_target(
            wt_a,
            target_dir=self.repo_root,
            base_commit=prep_a['base_commit'],
        )
        self.assertEqual(res_staged['status'], 'target_staged_changes')
        self.assertFalse(res_staged['integrated'])
        self.assertFalse(res_staged['cleaned'])
        self.assertEqual(res_staged['disposition'], 'retained_with_reason')
        self.assertTrue(res_staged['escalation_required'])
        self.assertIn('staged_user.txt', _git(self.repo_root, 'diff', '--cached', '--name-only').stdout)
        self.assertTrue(wt_a.exists())

    def test_converge_true_merge_conflict_preserves_scene_without_abort(self):
        (self.repo_root / 'user_wip.txt').write_text('keep me safe\n', encoding='utf-8')

        wt_c = self.tmp_root / 'wt-conflict'
        prep_c = self.gw.prepare_sibling_worktree(self.repo_root, wt_c, branch='feat/conflict')
        (wt_c / 'app.txt').write_text('worker conflict line\n', encoding='utf-8')
        _git(wt_c, 'add', 'app.txt')
        _git(wt_c, 'commit', '-m', 'worker conflict commit')

        (self.repo_root / 'app.txt').write_text('main conflict line\n', encoding='utf-8')
        _git(self.repo_root, 'add', 'app.txt')
        _git(self.repo_root, 'commit', '-m', 'main conflict commit')

        res = self.gw.converge_worktree_to_target(
            wt_c,
            target_dir=self.repo_root,
            base_commit=prep_c['base_commit'],
        )
        self.assertEqual(res['status'], 'merge_conflict')
        self.assertFalse(res['integrated'])
        self.assertFalse(res['verified'])
        self.assertFalse(res['cleaned'])
        self.assertTrue(res['merge_in_progress'])
        self.assertEqual(res['conflicted_files'], ['app.txt'])
        self.assertEqual(res['disposition'], 'retained_with_reason')
        self.assertTrue(res['escalation_required'])
        self.assertTrue(res['recommendation'])
        # Conflict scene must stay intact without mechanical git merge --abort.
        merge_head_check = subprocess.run(
            ['git', '-C', str(self.repo_root), 'rev-parse', '--verify', '--quiet', 'MERGE_HEAD'],
            capture_output=True,
            text=True,
        )
        self.assertEqual(merge_head_check.returncode, 0)
        self.assertIn('<<<<<<<', (self.repo_root / 'app.txt').read_text(encoding='utf-8'))
        self.assertEqual((self.repo_root / 'user_wip.txt').read_text(encoding='utf-8'), 'keep me safe\n')
        self.assertTrue(wt_c.exists())

        # Re-invoking converge while MERGE_HEAD is active returns target_merge_in_progress safely.
        wt_other = self.tmp_root / 'wt-other'
        prep_o = self.gw.prepare_sibling_worktree(self.repo_root, wt_other, branch='feat/other', start_point=self.initial_commit)
        (wt_other / 'other.txt').write_text('other\n', encoding='utf-8')
        res2 = self.gw.converge_worktree_to_target(wt_other, target_dir=self.repo_root, base_commit=prep_o['base_commit'])
        self.assertEqual(res2['status'], 'target_merge_in_progress')
        self.assertEqual(res2['disposition'], 'retained_with_reason')
        self.assertTrue(res2['merge_in_progress'])
        self.assertTrue(wt_other.exists())

    def test_converge_retains_with_reason_on_verify_failure_unverified_or_active_writer(self):
        # 1. Failing verify_cmd retains worktree/branch and does not reset target.
        wt_vf = self.tmp_root / 'wt-vf'
        prep_vf = self.gw.prepare_sibling_worktree(self.repo_root, wt_vf, branch='feat/vf')
        (wt_vf / 'vf.txt').write_text('vf\n', encoding='utf-8')
        res_vf = self.gw.converge_worktree_to_target(
            wt_vf,
            target_dir=self.repo_root,
            base_commit=prep_vf['base_commit'],
            verify_cmd=[sys.executable, '-B', '-c', 'raise SystemExit(2)'],
        )
        self.assertEqual(res_vf['status'], 'verification_failed')
        self.assertTrue(res_vf['integrated'])
        self.assertFalse(res_vf['verified'])
        self.assertFalse(res_vf['cleaned'])
        self.assertEqual(res_vf['disposition'], 'retained_with_reason')
        self.assertTrue(res_vf['escalation_required'])
        self.assertTrue(wt_vf.exists())
        self.assertTrue((self.repo_root / 'vf.txt').exists())

        # 2. Integrated without verification retains until explicit cleanup_worktree(verified=True).
        wt_uv = self.tmp_root / 'wt-uv'
        prep_uv = self.gw.prepare_sibling_worktree(self.repo_root, wt_uv, branch='feat/uv')
        (wt_uv / 'uv.txt').write_text('uv\n', encoding='utf-8')
        res_uv = self.gw.converge_worktree_to_target(
            wt_uv,
            target_dir=self.repo_root,
            base_commit=prep_uv['base_commit'],
            verified=False,
        )
        self.assertEqual(res_uv['status'], 'integrated_unverified')
        self.assertTrue(res_uv['integrated'])
        self.assertFalse(res_uv['verified'])
        self.assertFalse(res_uv['cleaned'])
        self.assertEqual(res_uv['disposition'], 'retained_with_reason')
        self.assertTrue(wt_uv.exists())

        cleaned = self.gw.cleanup_worktree(wt_uv, target_ref='main', verified=True)
        self.assertTrue(cleaned['cleaned'])
        self.assertTrue(cleaned['branch_deleted'])
        self.assertEqual(cleaned['disposition'], 'cleaned')
        self.assertFalse(wt_uv.exists())

        # 3. Active writer blocks before harvest/merge.
        wt_aw = self.tmp_root / 'wt-aw'
        prep_aw = self.gw.prepare_sibling_worktree(self.repo_root, wt_aw, branch='feat/aw')
        (wt_aw / 'aw.txt').write_text('aw\n', encoding='utf-8')
        res_aw = self.gw.converge_worktree_to_target(
            wt_aw,
            target_dir=self.repo_root,
            base_commit=prep_aw['base_commit'],
            has_active_writer=True,
        )
        self.assertEqual(res_aw['status'], 'active_writer')
        self.assertFalse(res_aw['integrated'])
        self.assertFalse(res_aw['cleaned'])
        self.assertEqual(res_aw['disposition'], 'retained_with_reason')
        self.assertTrue(wt_aw.exists())
        self.assertFalse((self.repo_root / 'aw.txt').exists())

        # 4. cleanup_on_success=False retains cleanly after integration + verification.
        res_keep = self.gw.converge_worktree_to_target(
            wt_aw,
            target_dir=self.repo_root,
            base_commit=prep_aw['base_commit'],
            verified=True,
            cleanup_on_success=False,
        )
        self.assertEqual(res_keep['status'], 'converged_retained')
        self.assertTrue(res_keep['integrated'])
        self.assertTrue(res_keep['verified'])
        self.assertFalse(res_keep['cleaned'])
        self.assertEqual(res_keep['disposition'], 'retained_with_reason')
        self.assertTrue(wt_aw.exists())

    def test_cli_e2e_git_worktree_and_collab_lifecycle(self):
        gw_script = str(Path(__file__).with_name('git_worktree.py'))
        collab_script = str(Path(__file__).with_name('collab.py'))

        def run_gw(*args):
            proc = subprocess.run(
                [sys.executable, '-B', gw_script, *args],
                capture_output=True,
                text=True,
                check=True,
            )
            return json.loads(proc.stdout)

        def run_collab_sub(*args):
            proc = subprocess.run(
                [sys.executable, '-B', collab_script, '--db', str(db_path), *args],
                capture_output=True,
                text=True,
                check=True,
            )
            return json.loads(proc.stdout)

        task_dir = self.tmp_root / 'cli-e2e-repo'
        task_dir.mkdir()
        (task_dir / 'base.txt').write_text('v1\n', encoding='utf-8')
        init_out = run_gw('init-repo', '--dir', str(task_dir), '--branch', 'main')

        wt1 = self.tmp_root / 'cli-wt-1'
        wt2 = self.tmp_root / 'cli-wt-2'
        p1 = run_gw('prepare', '--repo', str(task_dir), '--worktree', str(wt1), '--branch', 'slice/one')
        p2 = run_gw('prepare', '--repo', str(task_dir), '--worktree', str(wt2), '--branch', 'slice/two')

        db_path = self.tmp_root / 'cli-e2e.sqlite3'

        # Stubbed launch writes uncommitted files into each worktree without completing immediately,
        # allowing verification of concurrent sibling writers + same-worktree exclusion, then finishes.
        launched_tasks = {}

        def parallel_launch(task):
            launched_tasks[task['id']] = task
            c_inst.store.update(task['id'], state='running')
            cwd_path = Path(task['cwd'])
            if cwd_path == wt1:
                (cwd_path / 'one.txt').write_text('deliverable one\n', encoding='utf-8')
            elif cwd_path == wt2:
                (cwd_path / 'two.txt').write_text('deliverable two\n', encoding='utf-8')

        c_inst = Collaboration(db_path, launch=parallel_launch)

        def invoke_collab(*argv):
            buf = io.StringIO()
            with redirect_stdout(buf), patch.object(collab, 'Collaboration', return_value=c_inst):
                rc = collab.main(['--db', str(db_path), *argv])
            return rc, json.loads(buf.getvalue())

        cli_info = run_collab_sub('connect', '--runtime', 'hermes')
        cid = cli_info['client_id']

        rc1, t1 = invoke_collab('--client', cid, 'start', '--cwd', p1['cwd'], '--prompt', 'slice 1', '--agent', 'codex', '--write')
        rc2, t2 = invoke_collab('--client', cid, 'start', '--cwd', p2['cwd'], '--prompt', 'slice 2', '--agent', 'codex', '--write')
        self.assertEqual((rc1, rc2), (0, 0))

        # Second writer on wt1 must be rejected while t1 is running.
        rc_dup, dup_err = invoke_collab('--client', cid, 'start', '--cwd', p1['cwd'], '--prompt', 'dup', '--agent', 'codex', '--write')
        self.assertEqual(rc_dup, 1)
        self.assertIn('error', dup_err)

        # Complete both worker turns.
        for t in (t1, t2):
            c_inst.store.finish_turn(t['id'], t['turn'], 'completed', 0, 'end_turn')

        # Real collab.py subprocess readback confirms both tasks completed.
        tasks_list = run_collab_sub('--client', cid, 'list')
        self.assertEqual({item['id']: item['state'] for item in tasks_list}, {t1['id']: 'completed', t2['id']: 'completed'})

        # Harvest + inspect + check-cleanup + converge via real git_worktree.py CLI subprocesses.
        h1 = run_gw('harvest', '--worktree', str(wt1), '--allowed-path', 'one.txt')
        self.assertTrue(h1['harvested'])
        c1 = run_gw(
            'converge',
            '--worktree', str(wt1),
            '--target-dir', str(task_dir),
            '--base-commit', p1['base_commit'],
            '--verify-cmd', f'{sys.executable} -B -c "import pathlib; assert pathlib.Path(\'one.txt\').exists()"',
        )
        c2 = run_gw(
            'converge',
            '--worktree', str(wt2),
            '--target-dir', str(task_dir),
            '--base-commit', p2['base_commit'],
            '--keep-worktree',
        )
        self.assertEqual(c1['status'], 'converged')
        self.assertEqual(c1['disposition'], 'cleaned')
        self.assertFalse(wt1.exists())

        self.assertEqual(c2['status'], 'integrated_unverified')
        self.assertEqual(c2['disposition'], 'retained_with_reason')
        chk2 = run_gw('check-cleanup', '--worktree', str(wt2), '--target-ref', 'main', '--unverified')
        self.assertEqual(chk2['disposition'], 'retained_with_reason')
        cln2 = run_gw('cleanup', '--worktree', str(wt2), '--target-ref', 'main')
        self.assertTrue(cln2['cleaned'])
        self.assertTrue(cln2['branch_deleted'])
        self.assertEqual(cln2['disposition'], 'cleaned')
        self.assertFalse(wt2.exists())
        self.assertEqual((task_dir / 'one.txt').read_text(encoding='utf-8'), 'deliverable one\n')
        self.assertEqual((task_dir / 'two.txt').read_text(encoding='utf-8'), 'deliverable two\n')
        self.assertEqual(init_out['branch'], 'main')


if __name__ == '__main__':
    unittest.main()
