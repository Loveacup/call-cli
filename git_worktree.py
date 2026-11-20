"""Thin pure-stdlib helper for Hermes-managed Git/worktree isolation and convergence.

Provides scoped task-repo initialization, outside-repo sibling worktree
preparation, four-state delivery inspection, uncommitted deliverable harvesting,
safe serial convergence (protecting user pre-existing changes and preserving
true merge-conflict scenes without forced abort/reset/stash/clean), and
post-verification non-forced cleanup without any background daemon or SQLite.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import subprocess
import sys


class WorktreeError(RuntimeError):
    """Fail-closed error raised when a worktree precondition or Git command fails."""


_NOISE_DIRS = frozenset({'__pycache__', '.pytest_cache'})
_NOISE_FILES = frozenset({'.DS_Store'})
_NOISE_SUFFIXES = ('.pyc', '.pyo')
_SYSTEM_ROOT_PATHS = (
    '/',
    '/tmp',
    '/private',
    '/private/tmp',
    '/var',
    '/private/var',
    '/Volumes',
    '/Users',
    '/home',
    '/etc',
    '/opt',
    '/usr',
    '/bin',
    '/sbin',
    '/System',
    '/Library',
    '/Applications',
    '/dev',
)


def _run_git(cwd, *args, check=True):
    cmd = ['git', '-C', str(cwd), *args]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise WorktreeError('git executable not found') from exc
    if check and res.returncode != 0:
        detail = res.stderr.strip() or res.stdout.strip() or f'exit code {res.returncode}'
        raise WorktreeError(f'git {" ".join(args)} failed: {detail}')
    return res


def _validate_ref_arg(value, label):
    if not isinstance(value, str) or not value or value != value.strip() or value.startswith('-'):
        raise WorktreeError(f'{label} must be a valid non-empty Git ref')
    return value


def _validate_branch_name(branch):
    _validate_ref_arg(branch, 'branch')
    if branch == 'HEAD':
        raise WorktreeError('branch cannot be HEAD')
    try:
        res = subprocess.run(
            ['git', 'check-ref-format', '--branch', branch],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise WorktreeError('git executable not found') from exc
    if res.returncode != 0:
        raise WorktreeError(f'Invalid branch name: {branch}')
    return branch


def _normalize_subdir(target_subdir, *, label='target_subdir'):
    if target_subdir is None:
        return ''
    if isinstance(target_subdir, Path):
        raw = target_subdir.as_posix()
    elif isinstance(target_subdir, str):
        raw = target_subdir
    else:
        raise WorktreeError(f'{label} must be a string or Path')
    if raw == '':
        return ''
    if not raw.strip():
        raise WorktreeError(f'{label} cannot be whitespace-only')
    if (
        raw.startswith(('/', '\\'))
        or PurePosixPath(raw).is_absolute()
        or PureWindowsPath(raw).is_absolute()
    ):
        raise WorktreeError(f'{label} must be relative: {raw}')
    raw_parts = raw.replace('\\', '/').split('/')
    if '..' in raw_parts:
        raise WorktreeError(f'{label} cannot contain parent traversal (..): {raw}')
    parts = [part for part in raw_parts if part and part != '.']
    return '/'.join(parts)


def _list_worktrees(repo_dir):
    res = _run_git(repo_dir, 'worktree', 'list', '--porcelain')
    entries = []
    current = None
    for line in res.stdout.splitlines():
        if not line:
            if current is not None:
                entries.append(current)
                current = None
            continue
        key, _, val = line.partition(' ')
        if key == 'worktree':
            if current is not None:
                entries.append(current)
            current = {
                'path': Path(val).resolve(),
                'head': None,
                'branch': None,
                'detached': False,
                'bare': False,
                'locked': False,
                'prunable': False,
            }
        elif current is not None:
            if key == 'HEAD':
                current['head'] = val
            elif key == 'branch':
                current['branch'] = val
            elif key == 'detached':
                current['detached'] = True
            elif key == 'bare':
                current['bare'] = True
            elif key == 'locked':
                current['locked'] = True
            elif key == 'prunable':
                current['prunable'] = True
    if current is not None:
        entries.append(current)
    if not entries:
        raise WorktreeError(f'No Git worktrees found for {repo_dir}')
    return entries


def _resolve_repo_root(repo_root):
    if repo_root is None or (isinstance(repo_root, str) and not repo_root.strip()):
        raise WorktreeError('repo_root must be a non-empty directory path')
    repo_path = Path(repo_root).expanduser()
    if not repo_path.is_dir():
        raise WorktreeError(f'repo_root does not exist or is not a directory: {repo_root}')
    repo_resolved = repo_path.resolve()
    top = _run_git(repo_resolved, 'rev-parse', '--show-toplevel').stdout.strip()
    return Path(top).resolve()


def _resolve_worktree_context(worktree_root):
    if worktree_root is None or (isinstance(worktree_root, str) and not worktree_root.strip()):
        raise WorktreeError('worktree_root must be a non-empty directory path')
    wt_input = Path(worktree_root).expanduser()
    if not wt_input.is_dir():
        raise WorktreeError(f'worktree_root does not exist or is not a directory: {worktree_root}')
    wt_path = wt_input.resolve()
    top = Path(_run_git(wt_path, 'rev-parse', '--show-toplevel').stdout.strip()).resolve()
    if top != wt_path:
        raise WorktreeError(f'worktree_root must be a Git worktree root, not a subdirectory: {wt_path}')
    entries = _list_worktrees(wt_path)
    match = next((entry for entry in entries if entry['path'] == wt_path), None)
    if match is None:
        raise WorktreeError(f'Directory is not a registered Git worktree: {wt_path}')
    return wt_path, entries[0]['path'], match


def _uncommitted_porcelain_lines(wt_path):
    # Preserve raw porcelain prefixes (leading spaces matter for unstaged edits).
    res = _run_git(wt_path, 'status', '--porcelain', '--untracked-files=all')
    return [line for line in res.stdout.splitlines() if line]


def _git_identity_prefix(cwd):
    prefix = []
    name_res = _run_git(cwd, 'config', '--get', 'user.name', check=False)
    if name_res.returncode != 0 or not name_res.stdout.strip():
        prefix.extend(['-c', 'user.name=Hermes Call-CLI'])
    email_res = _run_git(cwd, 'config', '--get', 'user.email', check=False)
    if email_res.returncode != 0 or not email_res.stdout.strip():
        prefix.extend(['-c', 'user.email=hermes@local'])
    return prefix


def _status_entries(cwd):
    res = _run_git(cwd, 'status', '--porcelain=v1', '-z', '--untracked-files=all')
    tokens = res.stdout.split('\0')
    entries = []
    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        if not token:
            idx += 1
            continue
        if len(token) < 4:
            raise WorktreeError(f'Unexpected git status entry format: {token!r}')
        xy = token[:2]
        path = token[3:]
        orig_path = None
        if xy[0] in ('R', 'C') or xy[1] in ('R', 'C'):
            idx += 1
            if idx < len(tokens):
                orig_path = tokens[idx]
        entries.append({'xy': xy, 'path': path, 'orig_path': orig_path})
        idx += 1
    return entries


def _is_noise_path(rel_path):
    parts = [part for part in rel_path.rstrip('/').replace('\\', '/').split('/') if part]
    if not parts:
        return False
    if any(part in _NOISE_DIRS for part in parts):
        return True
    name = parts[-1]
    return name in _NOISE_FILES or name.endswith(_NOISE_SUFFIXES)


def _normalize_allowed_paths(allowed_paths):
    if allowed_paths is None:
        return None
    if isinstance(allowed_paths, (str, bytes, Path)):
        raise WorktreeError('allowed_paths must be a sequence of relative paths')
    try:
        items = list(allowed_paths)
    except TypeError as exc:
        raise WorktreeError('allowed_paths must be a sequence of relative paths') from exc
    normalized = []
    for item in items:
        norm = _normalize_subdir(item, label='allowed_paths entry')
        if not norm:
            raise WorktreeError('allowed_paths entries cannot be empty or repository root')
        normalized.append(norm)
    return normalized


def _check_allowed_paths(paths, allowed_prefixes):
    if allowed_prefixes is None:
        return
    disallowed = []
    for raw in paths:
        norm = raw.rstrip('/')
        if not any(norm == prefix or norm.startswith(prefix + '/') for prefix in allowed_prefixes):
            disallowed.append(norm)
    if disallowed:
        raise WorktreeError(f'Deliverable paths outside allowed_paths: {", ".join(sorted(set(disallowed)))}')


def _paths_overlap(path_a, path_b):
    a = path_a.rstrip('/')
    b = path_b.rstrip('/')
    return a == b or a.startswith(b + '/') or b.startswith(a + '/')


def _has_merge_head(cwd):
    return _run_git(cwd, 'rev-parse', '--verify', '--quiet', 'MERGE_HEAD', check=False).returncode == 0


def _conflicted_files(cwd):
    res = _run_git(cwd, 'diff', '--name-only', '--diff-filter=U', '-z')
    return [p for p in res.stdout.split('\0') if p]


def _named_branch(cwd):
    sym = _run_git(cwd, 'symbolic-ref', '--quiet', '--short', 'HEAD', check=False)
    if sym.returncode != 0 or not sym.stdout.strip():
        return None
    return sym.stdout.strip()


def _find_branch_delete_cwd(entries, wt_path, wt_head, target_ref, target_sha):
    main_root = entries[0]['path']
    target_branch_ref = f'refs/heads/{target_ref}' if target_ref else None
    candidates = []
    for entry in entries:
        epath = entry['path']
        if epath == wt_path or entry.get('bare') or not entry.get('head'):
            continue
        anc = _run_git(epath, 'merge-base', '--is-ancestor', wt_head, entry['head'], check=False)
        if anc.returncode == 0:
            score = 0
            if target_branch_ref and entry.get('branch') == target_branch_ref:
                score = 3
            elif target_sha and entry['head'] == target_sha:
                score = 2
            elif epath == main_root:
                score = 1
            candidates.append((score, epath))
    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]
    return main_root


def _validate_scoped_init_dir(task_dir):
    if task_dir is None or (isinstance(task_dir, str) and not task_dir.strip()):
        raise WorktreeError('task_dir must be a non-empty directory path')
    raw_path = Path(task_dir).expanduser()
    if not raw_path.is_dir():
        raise WorktreeError(f'task_dir does not exist or is not a directory: {task_dir}')
    task_path = raw_path.resolve()

    home_path = Path.home().resolve()
    if task_path == home_path or home_path.is_relative_to(task_path):
        raise WorktreeError(f'Refusing to initialize Git repo at or above home directory: {task_path}')

    forbidden = {Path('/')}
    for sys_root in _SYSTEM_ROOT_PATHS:
        p = Path(sys_root)
        forbidden.add(p)
        if p.exists():
            forbidden.add(p.resolve())
    if task_path in forbidden:
        raise WorktreeError(f'Refusing to initialize Git repo at host/system directory: {task_path}')

    if (task_path / '.git').exists():
        raise WorktreeError(f'Directory already contains .git: {task_path}')
    inside = _run_git(task_path, 'rev-parse', '--is-inside-work-tree', check=False)
    if inside.returncode == 0:
        raise WorktreeError(f'Directory is already inside an existing Git repository: {task_path}')
    return task_path


def init_task_repo(task_dir, *, branch='main', commit_existing=True, message='chore: init task baseline'):
    """Initialize a scoped Git repository in a non-Git task directory and establish base_commit."""
    if not isinstance(commit_existing, bool):
        raise WorktreeError('commit_existing must be a boolean')
    if not isinstance(message, str) or not message.strip():
        raise WorktreeError('message must be a non-empty string')
    _validate_branch_name(branch)
    task_path = _validate_scoped_init_dir(task_dir)

    try:
        res = subprocess.run(
            ['git', 'init', '-b', branch, str(task_path)],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise WorktreeError('git executable not found') from exc
    if res.returncode != 0:
        detail = res.stderr.strip() or res.stdout.strip() or f'exit code {res.returncode}'
        raise WorktreeError(f'git init failed: {detail}')

    committed_files = []
    skipped_noise_files = []
    base_commit = None
    if commit_existing:
        entries = _status_entries(task_path)
        for entry in entries:
            if entry['xy'] == '??' and _is_noise_path(entry['path']):
                skipped_noise_files.append(entry['path'])
            else:
                committed_files.append(entry['path'])
        ident = _git_identity_prefix(task_path)
        if committed_files:
            _run_git(task_path, 'add', '-A', '--', *committed_files)
            _run_git(task_path, *ident, 'commit', '-m', message)
        else:
            _run_git(task_path, *ident, 'commit', '--allow-empty', '-m', message)
        base_commit = _run_git(task_path, 'rev-parse', 'HEAD').stdout.strip()

    return {
        'repo_root': str(task_path),
        'branch': branch,
        'base_commit': base_commit,
        'committed_files': committed_files,
        'skipped_noise_files': skipped_noise_files,
    }


def prepare_sibling_worktree(
    repo_root,
    worktree_dir,
    *,
    branch=None,
    start_point='HEAD',
    target_subdir='',
    reuse=False,
    device='local',
):
    """Create or safely reuse an outside-repo sibling Git worktree."""
    if not isinstance(reuse, bool):
        raise WorktreeError('reuse must be a boolean')
    if not isinstance(device, str) or not device.strip():
        raise WorktreeError('device must be a non-empty string')
    if worktree_dir is None or (isinstance(worktree_dir, str) and not worktree_dir.strip()):
        raise WorktreeError('worktree_dir must be a non-empty path')

    resolved_repo = _resolve_repo_root(repo_root)
    entries = _list_worktrees(resolved_repo)
    main_root = entries[0]['path']
    wt_path = Path(worktree_dir).expanduser().resolve()

    for anchor in {resolved_repo, main_root}:
        if wt_path == anchor:
            raise WorktreeError(f'worktree_dir cannot equal repo_root: {wt_path}')
        if wt_path.is_relative_to(anchor):
            raise WorktreeError(f'worktree_dir cannot be inside repo_root (writer-lock collision): {wt_path}')
        if anchor.is_relative_to(wt_path):
            raise WorktreeError(f'worktree_dir cannot be an ancestor of repo_root (writer-lock collision): {wt_path}')

    relative_subdir = _normalize_subdir(target_subdir)
    if branch is not None:
        _validate_branch_name(branch)

    if reuse:
        if not wt_path.is_dir():
            raise WorktreeError(f'Cannot reuse missing worktree_dir: {wt_path}')
        top_res = _run_git(wt_path, 'rev-parse', '--show-toplevel', check=False)
        if top_res.returncode != 0 or Path(top_res.stdout.strip()).resolve() != wt_path:
            raise WorktreeError(f'Cannot reuse non-worktree directory: {wt_path}')
        match = next((entry for entry in entries if entry['path'] == wt_path), None)
        if match is None:
            raise WorktreeError(f'Worktree does not belong to repo {resolved_repo}: {wt_path}')
        actual_branch = _named_branch(wt_path)
        if not actual_branch:
            raise WorktreeError(f'Reused worktree is not on a named branch: {wt_path}')
        if branch is not None and actual_branch != branch:
            raise WorktreeError(f'Worktree branch mismatch: expected {branch}, found {actual_branch}')
        cwd_path = (wt_path / relative_subdir).resolve() if relative_subdir else wt_path
        if not cwd_path.is_dir() or not cwd_path.is_relative_to(wt_path):
            raise WorktreeError(f'target_subdir missing or invalid in reused worktree: {relative_subdir}')
        base_commit = _run_git(wt_path, 'rev-parse', 'HEAD').stdout.strip()
        return {
            'managed_by': 'Hermes',
            'device': device,
            'repo_root': str(resolved_repo),
            'worktree_root': str(wt_path),
            'cwd': str(cwd_path),
            'relative_subdir': relative_subdir,
            'branch': actual_branch,
            'base_commit': base_commit,
            'reused': True,
        }

    if wt_path.exists():
        raise WorktreeError(f'worktree_dir already exists with reuse=False: {wt_path}')
    for entry in entries:
        if wt_path.is_relative_to(entry['path']) or entry['path'].is_relative_to(wt_path):
            raise WorktreeError(f'worktree_dir overlaps existing worktree {entry["path"]}: {wt_path}')

    _validate_ref_arg(start_point, 'start_point')
    start_commit = _run_git(resolved_repo, 'rev-parse', '--verify', f'{start_point}^{{commit}}').stdout.strip()

    actual_branch = branch if branch is not None else _validate_branch_name(f'hermes/{wt_path.name}')
    dup = _run_git(resolved_repo, 'show-ref', '--verify', '--quiet', f'refs/heads/{actual_branch}', check=False)
    if dup.returncode == 0:
        raise WorktreeError(f'Branch already exists: {actual_branch}')

    if relative_subdir:
        obj_type = _run_git(resolved_repo, 'cat-file', '-t', f'{start_commit}:{relative_subdir}', check=False)
        if obj_type.returncode != 0 or obj_type.stdout.strip() != 'tree':
            raise WorktreeError(f'target_subdir missing or not a directory in {start_point}: {relative_subdir}')

    wt_path.parent.mkdir(parents=True, exist_ok=True)
    _run_git(resolved_repo, 'worktree', 'add', '-b', actual_branch, str(wt_path), start_commit)

    cwd_path = (wt_path / relative_subdir).resolve() if relative_subdir else wt_path
    if not cwd_path.is_dir() or not cwd_path.is_relative_to(wt_path):
        raise WorktreeError(f'Created worktree missing valid target_subdir: {relative_subdir}')
    base_commit = _run_git(wt_path, 'rev-parse', 'HEAD').stdout.strip()
    return {
        'managed_by': 'Hermes',
        'device': device,
        'repo_root': str(resolved_repo),
        'worktree_root': str(wt_path),
        'cwd': str(cwd_path),
        'relative_subdir': relative_subdir,
        'branch': actual_branch,
        'base_commit': base_commit,
        'reused': False,
    }


def inspect_worktree_state(worktree_root, *, base_commit, target_ref=None, verified=False):
    """Report uncommitted files, branch commits, integration status, and verification separately."""
    if not isinstance(verified, bool):
        raise WorktreeError('verified must be a boolean')
    wt_path, main_root, _ = _resolve_worktree_context(worktree_root)
    _validate_ref_arg(base_commit, 'base_commit')
    base_sha = _run_git(wt_path, 'rev-parse', '--verify', f'{base_commit}^{{commit}}').stdout.strip()
    head_sha = _run_git(wt_path, 'rev-parse', 'HEAD').stdout.strip()
    branch = _named_branch(wt_path)

    uncommitted_files = _uncommitted_porcelain_lines(wt_path)
    rev_res = _run_git(wt_path, 'rev-list', '--reverse', f'{base_sha}..HEAD')
    branch_commits = [line for line in rev_res.stdout.splitlines() if line]

    if target_ref is None:
        integrated = None
    else:
        _validate_ref_arg(target_ref, 'target_ref')
        target_sha = _run_git(wt_path, 'rev-parse', '--verify', f'{target_ref}^{{commit}}').stdout.strip()
        anc = _run_git(wt_path, 'merge-base', '--is-ancestor', 'HEAD', target_sha, check=False)
        if anc.returncode not in (0, 1):
            detail = anc.stderr.strip() or f'exit code {anc.returncode}'
            raise WorktreeError(f'git merge-base failed: {detail}')
        integrated = (len(uncommitted_files) == 0) and (anc.returncode == 0)

    return {
        'worktree_root': str(wt_path),
        'repo_root': str(main_root),
        'branch': branch,
        'head_commit': head_sha,
        'base_commit': base_sha,
        'target_ref': target_ref,
        'uncommitted_files': uncommitted_files,
        'clean': len(uncommitted_files) == 0,
        'branch_commits': branch_commits,
        'integrated': integrated,
        'verified': verified,
    }


def harvest_worktree_deliverables(
    worktree_root,
    *,
    message='chore: harvest worktree deliverables',
    allowed_paths=None,
    base_commit=None,
):
    """Stage and commit modified/untracked slice deliverables while filtering transient noise."""
    if not isinstance(message, str) or not message.strip():
        raise WorktreeError('message must be a non-empty string')
    wt_path, main_root, _ = _resolve_worktree_context(worktree_root)
    if wt_path == main_root:
        raise WorktreeError(f'Cannot harvest main repository worktree: {wt_path}')

    branch = _named_branch(wt_path)
    if not branch:
        raise WorktreeError(f'Worktree must be on a named branch to harvest: {wt_path}')
    if _has_merge_head(wt_path) or _conflicted_files(wt_path):
        raise WorktreeError(f'Worktree has unresolved merge or conflicts: {wt_path}')

    allowed_prefixes = _normalize_allowed_paths(allowed_paths)
    if base_commit is not None:
        _validate_ref_arg(base_commit, 'base_commit')
        base_sha = _run_git(wt_path, 'rev-parse', '--verify', f'{base_commit}^{{commit}}').stdout.strip()
        if allowed_prefixes is not None:
            diff_res = _run_git(wt_path, 'diff', '--name-only', '-z', '--no-renames', f'{base_sha}..HEAD')
            committed_range = [p for p in diff_res.stdout.split('\0') if p]
            if committed_range:
                _check_allowed_paths(committed_range, allowed_prefixes)

    entries = _status_entries(wt_path)
    skipped_noise_files = []
    stage_entries = []
    for entry in entries:
        if entry['xy'] == '??' and _is_noise_path(entry['path']):
            skipped_noise_files.append(entry['path'])
        else:
            stage_entries.append(entry)

    head_sha = _run_git(wt_path, 'rev-parse', 'HEAD').stdout.strip()
    if not stage_entries:
        return {
            'worktree_root': str(wt_path),
            'repo_root': str(main_root),
            'branch': branch,
            'harvested': False,
            'commit': None,
            'head_commit': head_sha,
            'committed_files': [],
            'skipped_noise_files': skipped_noise_files,
        }

    paths_to_check = []
    paths_to_add = []
    for entry in stage_entries:
        paths_to_check.append(entry['path'])
        paths_to_add.append(entry['path'])
        if entry['orig_path']:
            paths_to_check.append(entry['orig_path'])
            paths_to_add.append(entry['orig_path'])

    _check_allowed_paths(paths_to_check, allowed_prefixes)

    _run_git(wt_path, 'add', '-A', '--', *paths_to_add)
    staged_diff = _run_git(wt_path, 'diff', '--cached', '--name-only', '-z').stdout
    if not staged_diff:
        return {
            'worktree_root': str(wt_path),
            'repo_root': str(main_root),
            'branch': branch,
            'harvested': False,
            'commit': None,
            'head_commit': head_sha,
            'committed_files': [],
            'skipped_noise_files': skipped_noise_files,
        }

    ident = _git_identity_prefix(wt_path)
    _run_git(wt_path, *ident, 'commit', '-m', message)
    new_head = _run_git(wt_path, 'rev-parse', 'HEAD').stdout.strip()
    return {
        'worktree_root': str(wt_path),
        'repo_root': str(main_root),
        'branch': branch,
        'harvested': True,
        'commit': new_head,
        'head_commit': new_head,
        'committed_files': [entry['path'] for entry in stage_entries],
        'skipped_noise_files': skipped_noise_files,
    }


def verify_cleanup_safe(worktree_root, *, target_ref, has_active_writer=False, verified=True):
    """Gate non-forced worktree cleanup: clean after integration+verification or retain with reason."""
    if not isinstance(has_active_writer, bool):
        raise WorktreeError('has_active_writer must be a boolean')
    if not isinstance(verified, bool):
        raise WorktreeError('verified must be a boolean')
    wt_path, main_root, match = _resolve_worktree_context(worktree_root)
    branch = _named_branch(wt_path)
    head_sha = _run_git(wt_path, 'rev-parse', 'HEAD').stdout.strip()

    reasons = []
    if has_active_writer:
        reasons.append('active writer owns this worktree or a subdirectory')
    if wt_path == main_root:
        reasons.append('target is the main worktree and cannot be removed')
    if match.get('locked'):
        reasons.append('worktree is locked')

    uncommitted_files = _uncommitted_porcelain_lines(wt_path)
    if uncommitted_files:
        reasons.append(f'worktree has {len(uncommitted_files)} uncommitted or untracked file(s)')

    target_sha = None
    if (
        target_ref is None
        or not isinstance(target_ref, str)
        or not target_ref.strip()
        or target_ref != target_ref.strip()
        or target_ref.startswith('-')
    ):
        reasons.append('missing or invalid target_ref')
    else:
        ref_res = _run_git(wt_path, 'rev-parse', '--verify', f'{target_ref}^{{commit}}', check=False)
        if ref_res.returncode != 0:
            reasons.append(f'invalid target_ref: {target_ref}')
        else:
            target_sha = ref_res.stdout.strip()
            anc = _run_git(wt_path, 'merge-base', '--is-ancestor', 'HEAD', target_sha, check=False)
            if anc.returncode != 0:
                reasons.append(f'worktree HEAD has commits not merged into target_ref ({target_ref})')

    if not verified:
        reasons.append('target verification not confirmed')

    safe = len(reasons) == 0
    disposition = 'ready_to_clean' if safe else 'retained_with_reason'
    command = ['git', '-C', str(main_root), 'worktree', 'remove', str(wt_path)] if safe else None
    branch_cleanup_command = None
    if safe and branch:
        entries = _list_worktrees(main_root)
        del_cwd = _find_branch_delete_cwd(entries, wt_path, head_sha, target_ref, target_sha)
        branch_cleanup_command = ['git', '-C', str(del_cwd), 'branch', '-d', branch]

    return {
        'safe': safe,
        'disposition': disposition,
        'retain_reasons': list(reasons),
        'reasons': reasons,
        'command': command,
        'branch': branch,
        'branch_cleanup_command': branch_cleanup_command,
        'repo_root': str(main_root),
        'worktree_root': str(wt_path),
        'target_ref': target_ref,
        'has_active_writer': has_active_writer,
        'verified': verified,
        'uncommitted_files': uncommitted_files,
    }


def cleanup_worktree(worktree_root, *, target_ref, has_active_writer=False, verified=True, delete_branch=True):
    """Execute non-forced worktree remove + branch -d when safe, or return retained_with_reason."""
    if not isinstance(delete_branch, bool):
        raise WorktreeError('delete_branch must be a boolean')
    check = verify_cleanup_safe(
        worktree_root,
        target_ref=target_ref,
        has_active_writer=has_active_writer,
        verified=verified,
    )
    if not check['safe']:
        return {
            'cleaned': False,
            'branch_deleted': False,
            'branch': check['branch'],
            'branch_retain_reason': None,
            'disposition': 'retained_with_reason',
            'retain_reasons': check['retain_reasons'],
            'worktree_root': check['worktree_root'],
            'repo_root': check['repo_root'],
            'target_ref': target_ref,
            'verified': verified,
        }

    rm_res = subprocess.run(check['command'], capture_output=True, text=True)
    if rm_res.returncode != 0:
        detail = rm_res.stderr.strip() or rm_res.stdout.strip() or f'exit code {rm_res.returncode}'
        return {
            'cleaned': False,
            'branch_deleted': False,
            'branch': check['branch'],
            'branch_retain_reason': None,
            'disposition': 'retained_with_reason',
            'retain_reasons': [f'worktree remove refused without --force: {detail}'],
            'worktree_root': check['worktree_root'],
            'repo_root': check['repo_root'],
            'target_ref': target_ref,
            'verified': verified,
        }

    branch_deleted = False
    branch_retain_reason = None
    if delete_branch and check['branch_cleanup_command']:
        br_res = subprocess.run(check['branch_cleanup_command'], capture_output=True, text=True)
        if br_res.returncode == 0:
            branch_deleted = True
        else:
            branch_retain_reason = br_res.stderr.strip() or br_res.stdout.strip() or f'exit code {br_res.returncode}'

    return {
        'cleaned': True,
        'branch_deleted': branch_deleted,
        'branch': check['branch'],
        'branch_retain_reason': branch_retain_reason,
        'disposition': 'cleaned',
        'retain_reasons': [],
        'worktree_root': check['worktree_root'],
        'repo_root': check['repo_root'],
        'target_ref': target_ref,
        'verified': verified,
    }


def _converge_result(
    *,
    status,
    disposition,
    retain_reasons,
    wt_path,
    main_root,
    target_path,
    wt_branch,
    target_branch,
    harvest=None,
    merge_commit=None,
    integrated=False,
    verified=False,
    cleaned=False,
    branch_deleted=False,
    branch_retain_reason=None,
    merge_in_progress=False,
    conflicted_files=None,
    overlapping_target_files=None,
    staged_files=None,
    verify_exit_code=None,
    verify_output='',
    escalation_required=False,
    recommendation=None,
):
    return {
        'status': status,
        'disposition': disposition,
        'retain_reasons': retain_reasons,
        'worktree_root': str(wt_path),
        'repo_root': str(main_root),
        'target_dir': str(target_path),
        'branch': wt_branch,
        'target_branch': target_branch,
        'harvest': harvest,
        'merge_commit': merge_commit,
        'integrated': integrated,
        'verified': verified,
        'cleaned': cleaned,
        'branch_deleted': branch_deleted,
        'branch_retain_reason': branch_retain_reason,
        'merge_in_progress': merge_in_progress,
        'conflicted_files': conflicted_files or [],
        'overlapping_target_files': overlapping_target_files or [],
        'staged_files': staged_files or [],
        'verify_exit_code': verify_exit_code,
        'verify_output': verify_output,
        'escalation_required': escalation_required,
        'recommendation': recommendation,
    }


def _run_verify_cmd(target_path, verify_cmd):
    if isinstance(verify_cmd, str):
        if not verify_cmd.strip():
            raise WorktreeError('verify_cmd cannot be empty')
        proc = subprocess.run(verify_cmd, cwd=str(target_path), shell=True, capture_output=True, text=True)
    elif isinstance(verify_cmd, (list, tuple)) and verify_cmd and all(isinstance(x, str) for x in verify_cmd):
        proc = subprocess.run(list(verify_cmd), cwd=str(target_path), capture_output=True, text=True)
    else:
        raise WorktreeError('verify_cmd must be a non-empty string or list of strings')
    output = (proc.stdout + proc.stderr).strip()[:2000]
    return proc.returncode, output


def converge_worktree_to_target(
    worktree_root,
    *,
    target_dir,
    base_commit=None,
    target_branch=None,
    harvest_message=None,
    allowed_paths=None,
    verify_cmd=None,
    verified=False,
    cleanup_on_success=True,
    delete_branch=True,
    has_active_writer=False,
):
    """Harvest slice deliverables, merge serially into target_dir protecting user edits, verify, and clean."""
    for name, val in (
        ('verified', verified),
        ('cleanup_on_success', cleanup_on_success),
        ('delete_branch', delete_branch),
        ('has_active_writer', has_active_writer),
    ):
        if not isinstance(val, bool):
            raise WorktreeError(f'{name} must be a boolean')

    wt_path, wt_main_root, _ = _resolve_worktree_context(worktree_root)
    if wt_path == wt_main_root:
        raise WorktreeError(f'worktree_root cannot be the main repository worktree: {wt_path}')
    wt_branch = _named_branch(wt_path)
    if not wt_branch:
        raise WorktreeError(f'worktree_root must be on a named branch: {wt_path}')

    target_path, target_main_root, _ = _resolve_worktree_context(target_dir)
    if target_main_root != wt_main_root:
        raise WorktreeError('target_dir and worktree_root must belong to the same Git repository')
    if target_path == wt_path:
        raise WorktreeError('target_dir cannot equal worktree_root')
    target_current_branch = _named_branch(target_path)
    if not target_current_branch:
        raise WorktreeError(f'target_dir must be on a named branch: {target_path}')
    if target_branch is not None:
        _validate_branch_name(target_branch)
        if target_current_branch != target_branch:
            raise WorktreeError(
                f'target_dir branch mismatch: expected {target_branch}, found {target_current_branch}'
            )

    allowed_prefixes = _normalize_allowed_paths(allowed_paths)
    if base_commit is not None:
        _validate_ref_arg(base_commit, 'base_commit')
        _run_git(wt_path, 'rev-parse', '--verify', f'{base_commit}^{{commit}}')

    if has_active_writer:
        return _converge_result(
            status='active_writer',
            disposition='retained_with_reason',
            retain_reasons=['active writer owns this worktree or a subdirectory'],
            wt_path=wt_path,
            main_root=wt_main_root,
            target_path=target_path,
            wt_branch=wt_branch,
            target_branch=target_current_branch,
            recommendation='Wait for the active writer to settle before converging.',
        )

    if _has_merge_head(target_path) or _conflicted_files(target_path):
        conflicts = _conflicted_files(target_path)
        return _converge_result(
            status='target_merge_in_progress',
            disposition='retained_with_reason',
            retain_reasons=['target_dir already has an unfinished merge in progress'],
            wt_path=wt_path,
            main_root=wt_main_root,
            target_path=target_path,
            wt_branch=wt_branch,
            target_branch=target_current_branch,
            merge_in_progress=True,
            conflicted_files=conflicts,
            escalation_required=True,
            recommendation=f'Resolve existing merge in {target_path} before converging {wt_branch}.',
        )

    msg = harvest_message if harvest_message is not None else f'chore: harvest {wt_branch}'
    harvest_res = harvest_worktree_deliverables(
        wt_path,
        message=msg,
        allowed_paths=allowed_paths,
        base_commit=base_commit,
    )
    wt_head = harvest_res['head_commit']
    target_head = _run_git(target_path, 'rev-parse', 'HEAD').stdout.strip()
    anc = _run_git(target_path, 'merge-base', '--is-ancestor', wt_head, target_head, check=False)
    already_integrated = anc.returncode == 0
    merge_commit = None

    if not already_integrated:
        mb_res = _run_git(target_path, 'merge-base', target_head, wt_head, check=False)
        if mb_res.returncode == 0:
            mb_sha = mb_res.stdout.strip()
            diff_res = _run_git(target_path, 'diff', '--name-only', '-z', '--no-renames', f'{mb_sha}..{wt_head}')
            incoming_files = [p for p in diff_res.stdout.split('\0') if p]
        else:
            ls_res = _run_git(target_path, 'ls-tree', '-r', '--name-only', '-z', wt_head)
            incoming_files = [p for p in ls_res.stdout.split('\0') if p]
        if incoming_files:
            _check_allowed_paths(incoming_files, allowed_prefixes)

        target_entries = _status_entries(target_path)
        staged_files = [e['path'] for e in target_entries if e['xy'][0] not in (' ', '?')]
        if staged_files:
            return _converge_result(
                status='target_staged_changes',
                disposition='retained_with_reason',
                retain_reasons=[
                    f'target_dir has pre-existing staged changes ({len(staged_files)} file(s)); preserving user index'
                ],
                wt_path=wt_path,
                main_root=wt_main_root,
                target_path=target_path,
                wt_branch=wt_branch,
                target_branch=target_current_branch,
                harvest=harvest_res,
                staged_files=staged_files,
                escalation_required=True,
                recommendation=f'Commit or unstage user changes in {target_path}, then re-run converge for {wt_branch}.',
            )

        target_dirty_paths = []
        for entry in target_entries:
            target_dirty_paths.append(entry['path'])
            if entry['orig_path']:
                target_dirty_paths.append(entry['orig_path'])
        overlaps = sorted({tp for tp in target_dirty_paths for ip in incoming_files if _paths_overlap(tp, ip)})
        if overlaps:
            return _converge_result(
                status='target_dirty_overlap',
                disposition='retained_with_reason',
                retain_reasons=[
                    f'target_dir has uncommitted/untracked changes overlapping incoming files: {", ".join(overlaps)}'
                ],
                wt_path=wt_path,
                main_root=wt_main_root,
                target_path=target_path,
                wt_branch=wt_branch,
                target_branch=target_current_branch,
                harvest=harvest_res,
                overlapping_target_files=overlaps,
                escalation_required=True,
                recommendation=f'Review overlapping local edits in {target_path} ({", ".join(overlaps)}) before merging {wt_branch}.',
            )

        ident = _git_identity_prefix(target_path)
        merge_res = _run_git(
            target_path,
            *ident,
            'merge',
            '--no-ff',
            '-m',
            f'merge({wt_branch}): converge worktree deliverables',
            wt_branch,
            check=False,
        )
        if merge_res.returncode != 0:
            # Preserve true conflict scene without running git merge --abort (dirty merge cannot guarantee rollback).
            if _has_merge_head(target_path) or _conflicted_files(target_path):
                conflicts = _conflicted_files(target_path)
                return _converge_result(
                    status='merge_conflict',
                    disposition='retained_with_reason',
                    retain_reasons=[
                        f'merge conflict on {", ".join(conflicts) or "unknown files"}; conflict scene preserved'
                    ],
                    wt_path=wt_path,
                    main_root=wt_main_root,
                    target_path=target_path,
                    wt_branch=wt_branch,
                    target_branch=target_current_branch,
                    harvest=harvest_res,
                    merge_in_progress=True,
                    conflicted_files=conflicts,
                    escalation_required=True,
                    recommendation=(
                        f'Resolve conflicts in {target_path} ({", ".join(conflicts) or "check git status"}), '
                        f'commit the merge, verify, and clean up {wt_path}.'
                    ),
                )
            detail = merge_res.stderr.strip() or merge_res.stdout.strip() or f'exit code {merge_res.returncode}'
            return _converge_result(
                status='merge_failed',
                disposition='retained_with_reason',
                retain_reasons=[f'git merge failed without MERGE_HEAD: {detail}'],
                wt_path=wt_path,
                main_root=wt_main_root,
                target_path=target_path,
                wt_branch=wt_branch,
                target_branch=target_current_branch,
                harvest=harvest_res,
                escalation_required=True,
                recommendation=f'Inspect git merge failure in {target_path}; worktree {wt_path} retained.',
            )
        merge_commit = _run_git(target_path, 'rev-parse', 'HEAD').stdout.strip()

    verify_exit_code = None
    verify_output = ''
    if verify_cmd is not None:
        verify_exit_code, verify_output = _run_verify_cmd(target_path, verify_cmd)
        final_verified = verify_exit_code == 0
        if not final_verified:
            return _converge_result(
                status='verification_failed',
                disposition='retained_with_reason',
                retain_reasons=[f'verification command failed with exit code {verify_exit_code} in target_dir'],
                wt_path=wt_path,
                main_root=wt_main_root,
                target_path=target_path,
                wt_branch=wt_branch,
                target_branch=target_current_branch,
                harvest=harvest_res,
                merge_commit=merge_commit,
                integrated=True,
                verified=False,
                verify_exit_code=verify_exit_code,
                verify_output=verify_output,
                escalation_required=True,
                recommendation=f'Fix failing verification in {target_path} or inspect {wt_path}; worktree retained.',
            )
    else:
        final_verified = verified
        if not final_verified:
            return _converge_result(
                status='integrated_unverified',
                disposition='retained_with_reason',
                retain_reasons=['integrated into target_dir but verification not confirmed'],
                wt_path=wt_path,
                main_root=wt_main_root,
                target_path=target_path,
                wt_branch=wt_branch,
                target_branch=target_current_branch,
                harvest=harvest_res,
                merge_commit=merge_commit,
                integrated=True,
                verified=False,
                recommendation=f'Verify {target_path}, then run cleanup on {wt_path}.',
            )

    if not cleanup_on_success:
        return _converge_result(
            status='converged_retained',
            disposition='retained_with_reason',
            retain_reasons=['cleanup_on_success=False requested retention'],
            wt_path=wt_path,
            main_root=wt_main_root,
            target_path=target_path,
            wt_branch=wt_branch,
            target_branch=target_current_branch,
            harvest=harvest_res,
            merge_commit=merge_commit,
            integrated=True,
            verified=True,
            verify_exit_code=verify_exit_code,
            verify_output=verify_output,
        )

    clean_res = cleanup_worktree(
        wt_path,
        target_ref=target_current_branch,
        has_active_writer=False,
        verified=True,
        delete_branch=delete_branch,
    )
    if clean_res['cleaned']:
        return _converge_result(
            status='converged',
            disposition='cleaned',
            retain_reasons=[],
            wt_path=wt_path,
            main_root=wt_main_root,
            target_path=target_path,
            wt_branch=wt_branch,
            target_branch=target_current_branch,
            harvest=harvest_res,
            merge_commit=merge_commit,
            integrated=True,
            verified=True,
            cleaned=True,
            branch_deleted=clean_res['branch_deleted'],
            branch_retain_reason=clean_res['branch_retain_reason'],
            verify_exit_code=verify_exit_code,
            verify_output=verify_output,
        )

    return _converge_result(
        status='converged_retained',
        disposition='retained_with_reason',
        retain_reasons=clean_res['retain_reasons'],
        wt_path=wt_path,
        main_root=wt_main_root,
        target_path=target_path,
        wt_branch=wt_branch,
        target_branch=target_current_branch,
        harvest=harvest_res,
        merge_commit=merge_commit,
        integrated=True,
        verified=True,
        cleaned=False,
        verify_exit_code=verify_exit_code,
        verify_output=verify_output,
        recommendation=f'Inspect remaining files/locks in {wt_path} before cleanup.',
    )


def _build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)

    init_p = sub.add_parser('init-repo', help='Initialize a scoped Git repo in a task directory')
    init_p.add_argument('--dir', required=True)
    init_p.add_argument('--branch', default='main')
    init_p.add_argument('--no-commit-existing', action='store_true')
    init_p.add_argument('--message', default='chore: init task baseline')

    prep = sub.add_parser('prepare', help='Create or reuse an outside-repo sibling worktree')
    prep.add_argument('--repo', required=True)
    prep.add_argument('--worktree', required=True)
    prep.add_argument('--branch', default=None)
    prep.add_argument('--start-point', default='HEAD')
    prep.add_argument('--subdir', default='')
    prep.add_argument('--reuse', action='store_true')
    prep.add_argument('--device', default='local')

    insp = sub.add_parser('inspect', help='Inspect four independent worktree delivery states')
    insp.add_argument('--worktree', required=True)
    insp.add_argument('--base-commit', required=True)
    insp.add_argument('--target-ref', default=None)
    insp.add_argument('--verified', action='store_true')

    harv = sub.add_parser('harvest', help='Commit modified/untracked worktree deliverables')
    harv.add_argument('--worktree', required=True)
    harv.add_argument('--message', default='chore: harvest worktree deliverables')
    harv.add_argument('--base-commit', default=None)
    harv.add_argument('--allowed-path', action='append', default=None)

    conv = sub.add_parser('converge', help='Harvest, merge into target_dir, verify, and clean up')
    conv.add_argument('--worktree', required=True)
    conv.add_argument('--target-dir', required=True)
    conv.add_argument('--base-commit', default=None)
    conv.add_argument('--target-branch', default=None)
    conv.add_argument('--harvest-message', default=None)
    conv.add_argument('--allowed-path', action='append', default=None)
    conv.add_argument('--verify-cmd', default=None)
    conv.add_argument('--verified', action='store_true')
    conv.add_argument('--keep-worktree', action='store_true')
    conv.add_argument('--keep-branch', action='store_true')
    conv.add_argument('--has-active-writer', action='store_true')

    clean = sub.add_parser('check-cleanup', help='Check non-destructive worktree cleanup safety')
    clean.add_argument('--worktree', required=True)
    clean.add_argument('--target-ref', default=None)
    clean.add_argument('--has-active-writer', action='store_true')
    clean.add_argument('--unverified', action='store_true')

    do_clean = sub.add_parser('cleanup', help='Remove verified merged worktree and branch without force')
    do_clean.add_argument('--worktree', required=True)
    do_clean.add_argument('--target-ref', default=None)
    do_clean.add_argument('--has-active-writer', action='store_true')
    do_clean.add_argument('--unverified', action='store_true')
    do_clean.add_argument('--keep-branch', action='store_true')

    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.action == 'init-repo':
            result = init_task_repo(
                args.dir,
                branch=args.branch,
                commit_existing=not args.no_commit_existing,
                message=args.message,
            )
        elif args.action == 'prepare':
            result = prepare_sibling_worktree(
                args.repo,
                args.worktree,
                branch=args.branch,
                start_point=args.start_point,
                target_subdir=args.subdir,
                reuse=args.reuse,
                device=args.device,
            )
        elif args.action == 'inspect':
            result = inspect_worktree_state(
                args.worktree,
                base_commit=args.base_commit,
                target_ref=args.target_ref,
                verified=args.verified,
            )
        elif args.action == 'harvest':
            result = harvest_worktree_deliverables(
                args.worktree,
                message=args.message,
                allowed_paths=args.allowed_path,
                base_commit=args.base_commit,
            )
        elif args.action == 'converge':
            result = converge_worktree_to_target(
                args.worktree,
                target_dir=args.target_dir,
                base_commit=args.base_commit,
                target_branch=args.target_branch,
                harvest_message=args.harvest_message,
                allowed_paths=args.allowed_path,
                verify_cmd=args.verify_cmd,
                verified=args.verified,
                cleanup_on_success=not args.keep_worktree,
                delete_branch=not args.keep_branch,
                has_active_writer=args.has_active_writer,
            )
        elif args.action == 'check-cleanup':
            result = verify_cleanup_safe(
                args.worktree,
                target_ref=args.target_ref,
                has_active_writer=args.has_active_writer,
                verified=not args.unverified,
            )
        elif args.action == 'cleanup':
            result = cleanup_worktree(
                args.worktree,
                target_ref=args.target_ref,
                has_active_writer=args.has_active_writer,
                verified=not args.unverified,
                delete_branch=not args.keep_branch,
            )
        else:
            raise WorktreeError(f'Unsupported action: {args.action}')
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except WorktreeError as exc:
        print(json.dumps({'error': type(exc).__name__, 'message': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
