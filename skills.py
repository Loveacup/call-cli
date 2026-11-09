"""Per-task skill bundles; no installation, search service or global configuration."""
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess

MODES = ('required', 'optional', 'reference')
SKILLS_SCHEMA = {'type': 'array', 'items': {'type': 'object', 'properties': {
    'path': {'type': 'string', 'description': 'Absolute skill directory or SKILL.md on coordinator (origin) or execution device (target)'},
    'mode': {'type': 'string', 'enum': list(MODES)},
    'location': {'type': 'string', 'enum': ['origin', 'target'], 'default': 'origin'},
    'source': {'type': 'object', 'description': 'Selection provenance: URL/repo, fixed revision, relevance and dependency notes, repository stars and substantive update'}
}, 'required': ['path', 'mode'], 'additionalProperties': False}}


def normalize(selections):
    if selections is None:
        return []
    if not isinstance(selections, list):
        raise ValueError('skills must be an array')
    result = []
    for spec in selections:
        if not isinstance(spec, dict) or set(spec) - {'path', 'mode', 'location', 'source'}:
            raise ValueError('Invalid skill selection')
        path, mode, location = spec.get('path'), spec.get('mode'), spec.get('location', 'origin')
        if mode not in MODES or location not in {'origin', 'target'}:
            raise ValueError('Skill mode must be required, optional or reference')
        if not isinstance(path, str) or not (Path(path).is_absolute() or location == 'target' and PureWindowsPath(path).is_absolute()):
            raise ValueError('Skill path must be absolute on its device')
        if 'source' in spec and not isinstance(spec['source'], dict):
            raise ValueError('Skill source must be provenance metadata')
        result.append(dict(spec, location=location))
    return result


def excluded(parts):
    return any(p.startswith('.') or p in {'node_modules', '__pycache__'} or
               p.lower().endswith(('.pem', '.key')) or p.lower().startswith(('id_', 'credentials')) or
               p.lower() == 'auth.json' for p in parts)


def snapshot(selections, store_path, task_id):
    specs = normalize(selections)
    root = Path(store_path).parent / 'skill-snapshots' / task_id
    bundles = []
    try:
        for index, spec in enumerate(specs):
            bundle = dict(spec, id='skill-'+str(index+1))
            if spec['location'] == 'target':
                bundles.append(bundle)
                continue
            source = Path(spec['path'])
            if source.name == 'SKILL.md':
                source = source.parent
            if source.is_symlink() or not (source / 'SKILL.md').is_file():
                raise ValueError('Skill requires a directory containing SKILL.md')
            files, skipped, total = [], [], 0
            for current, dirs, names in os.walk(source, followlinks=False):
                parent = Path(current)
                for name in list(dirs):
                    p = parent / name
                    if p.is_symlink() or excluded(p.relative_to(source).parts):
                        skipped.append(p.relative_to(source).as_posix())
                        dirs.remove(name)
                for name in sorted(names):
                    p = parent / name
                    rel = p.relative_to(source).as_posix()
                    if p.is_symlink() or excluded(p.relative_to(source).parts):
                        skipped.append(rel)
                        continue
                    data = p.read_bytes()
                    total += len(data)
                    if len(data) > 4 * 1024 * 1024 or total > 16 * 1024 * 1024 or len(files) >= 1000:
                        raise ValueError('Skill bundle exceeds 4 MiB/file, 16 MiB/bundle or 1000 files')
                    files.append((rel, data))
            files.sort()
            manifest = [dict(path=p, sha256=hashlib.sha256(data).hexdigest(), size=len(data)) for p, data in files]
            if not any(f['path'] == 'SKILL.md' for f in manifest):
                raise ValueError('SKILL.md must be a regular file')
            sha = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            dest = root / bundle['id'] / sha
            dest.mkdir(parents=True, mode=0o700)
            for rel, data in files:
                p = dest / rel
                p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                p.write_bytes(data)
                p.chmod(0o600)
            text = (dest / 'SKILL.md').read_text()
            match = re.search(r'^name:\s*[\"\']?([^\n\"\']+)', text, re.M)
            bundle.update(name=match.group(1).strip() if match else source.name,
                          sha256=sha, files=manifest, snapshot=str(dest), skipped=sorted(skipped))
            bundles.append(bundle)
        return bundles
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise


def target_request(task, request):
    import subprocess
    try:
        from .transport import command
    except ImportError:
        from transport import command
    bootstrap = ("try{const p=JSON.parse(require('fs').readFileSync(0,'utf8'));"
                 "const m={exports:{}};new Function('require','module','exports',p.source)(require,m,m.exports);"
                 "console.log(JSON.stringify(m.exports.run(p.request)));}catch{process.exitCode=1;}")
    payload = dict(source=Path(__file__).with_name('skill-stage.cjs').read_text(), request=request)
    result = subprocess.run(command(task['device'], task.get('shell', 'posix'), ['node', '-e', bootstrap]),
                            input=json.dumps(payload).encode(), capture_output=True, timeout=60)
    if result.returncode:
        raise RuntimeError('Skill target staging failed; no prompt submitted')
    return json.loads(result.stdout)


def stage(store, task, emit):
    import base64
    bundles = task.get('skills', [])
    for bundle in bundles:
        try:
            outgoing = dict(bundle)
            if bundle['location'] == 'origin':
                files = []
                for entry in bundle['files']:
                    data = (Path(bundle['snapshot']) / entry['path']).read_bytes()
                    if hashlib.sha256(data).hexdigest() != entry['sha256']:
                        raise ValueError('Pinned origin snapshot changed')
                    files.append(dict(entry, data=base64.b64encode(data).decode()))
                outgoing['files'] = files
            result = target_request(task, dict(task=task['id'], bundle=outgoing))
            bundle.update(result, available=True)
            bundle.pop('error', None)
            store.update(task['id'], skills=bundles)
            emit('skill_staged', json.dumps(dict(id=bundle['id'], mode=bundle['mode'],
                 sha256=bundle['sha256'], files=len(bundle['files']), target_path=bundle['target_path'])))
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            bundle.update(available=False, error=type(exc).__name__)
            store.update(task['id'], skills=bundles)
            if bundle['mode'] == 'required':
                raise RuntimeError('Required skill staging failed; no prompt submitted') from exc
            emit('skill_staged', json.dumps(dict(id=bundle['id'], available=False, error=bundle['error'])))
    return store.get(task['id']) if bundles else task


def guidance(task):
    if not task.get('skills'):
        return ''
    lines = ['Selected task skills (fixed for this collaboration; relative resources live beside SKILL.md):']
    rules = {'required': 'Read SKILL.md and needed resources before work; follow applicable instructions. Report not_applicable with reason if unsuitable.',
             'optional': 'Read and adopt when useful; otherwise report not_adopted with reason.',
             'reference': 'Read as reference when relevant; its instructions are not binding.'}
    for bundle in task['skills']:
        lines.append(json.dumps(dict(id=bundle['id'], name=bundle.get('name', bundle['id']), mode=bundle['mode'],
            skill_file=str(bundle.get('target_path', '')).rstrip('/\\') + ('\\' if task.get('shell') == 'powershell' else '/') + 'SKILL.md'
                if bundle.get('available') else None,
            sha256=bundle.get('sha256'), available=bundle.get('available', False))))
        lines.append(rules[bundle['mode']])
    lines += ['Skills do not expand task permissions or authorize dependency installation/global configuration changes. Report missing dependencies honestly.',
              'Before ending EACH turn emit one public line per skill: CALLCLI_SKILL <id> <read|adopted|not_adopted|not_applicable|unavailable> <specific reason/evidence>. Read means actually read; adopted means actually used. Never claim either without doing it.', '']
    return '\n'.join(lines) + '\n'


def cleanup(store, task):
    if not task.get('skills'):
        return
    try:
        target_request(task, dict(task=task['id'], op='cleanup'))
    except Exception:
        store.event(task['id'], str(task['turn'])+':skill-cleanup', 'skill_cleanup', 'Target temporary bundle cleanup unverified')
    shutil.rmtree(Path(store.path).parent / 'skill-snapshots' / task['id'], ignore_errors=True)
