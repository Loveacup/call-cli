"""Per-device version checks and serialized, compatible ACPX patch updates."""
from __future__ import annotations
import fcntl
import hashlib
import json
import re
import subprocess
from pathlib import Path
from contextlib import contextmanager, nullcontext
try:
    from .runner import command
except ImportError:
    from runner import command


DEFAULT_LOCK_ROOT = Path.home()/'.hermes/cache/call-cli-locks'

@contextmanager
def device_lock(task,exclusive=False,wait=True,root=None):
    root=Path(root) if root is not None else DEFAULT_LOCK_ROOT
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    key=task.get('device_key',task['device'])
    path=root/(hashlib.sha256(key.encode()).hexdigest()+'.lock')
    with path.open('a') as handle:
        flags=fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(handle,flags | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:yield True
        finally:fcntl.flock(handle,fcntl.LOCK_UN)

def registry_version(raw):
    value=json.loads(raw)
    if isinstance(value,list) and len(value)==1:value=value[0]
    if not isinstance(value,str) or not re.fullmatch(r'\d+\.\d+\.\d+',value):
        raise ValueError('Registry did not return one stable version')
    return value

def update_decision(installed, latest, official, busy):
    parse=lambda v:tuple(map(int,v.split('.'))) if isinstance(v,str) and re.fullmatch(r'\d+\.\d+\.\d+',v) else None
    old,new=parse(installed),parse(latest)
    if old is None or new is None:return 'review_version_change'
    if old==new:return 'current'
    if old>new:return 'ahead_no_downgrade'
    if not official:return 'review_install_source'
    if old[:2]!=new[:2]:return 'review_version_change'
    return 'deferred_busy' if busy else 'upgrade'


def ensure_current(store,task):
    def run(args,timeout=30):
        result=subprocess.run(command(task['device'],task.get('shell','posix'),args),capture_output=True,timeout=timeout)
        if result.returncode:raise RuntimeError('Version command failed with exit '+str(result.returncode))
        return result.stdout.decode('utf-8','replace').strip()
    with device_lock(task,exclusive=True,wait=False) as exclusive, (nullcontext(True) if exclusive else device_lock(task)):
        upgrading=False
        try:
            installed=run(['acpx','--version'])
            latest=registry_version(run(['npm','view','acpx','dist-tags.latest','--json']))
            outcome=update_decision(installed,latest,True,False)
            if outcome=='upgrade':
                with store.connect() as db:
                    tasks=[json.loads(r['data']) for r in db.execute('SELECT data FROM tasks')]
                busy=not exclusive or any(t['id']!=task['id'] and t.get('device_key',t['device'])==task.get('device_key',task['device']) and t['state'] in {'queued','running','cancelling','closing','unknown'} for t in tasks)
                # A positive external-process observation blocks upgrades as well.
                probe="""const cp=require('child_process');let rows;if(process.platform==='win32'){rows=cp.execFileSync('powershell',['-NoProfile','-Command',\"Get-CimInstance Win32_Process | Where-Object {$_.Name -match '^(node|acpx)(\\\\.exe)?$'} | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress\"],{encoding:'utf8'});rows=rows.trim()?JSON.parse(rows):[];if(!Array.isArray(rows))rows=[rows];}else{rows=cp.execFileSync('ps',['-axo','pid=,args='],{encoding:'utf8'}).split('\\n').map(x=>{let m=x.trim().match(/^(\\d+) (.*)$/);return m?{ProcessId:Number(m[1]),CommandLine:m[2]}:{}})}console.log(rows.some(x=>x.ProcessId!==process.pid&&/[\\\\/]acpx[\\\\/]/.test(x.CommandLine||'')&&!/node -e /.test(x.CommandLine||''))?'busy':'idle')"""
                busy=busy or run(['node','-e',probe])!='idle'
                root=run(['npm','root','-g'])
                source=run(['node','-e',"const p=require(require('path').join(process.argv[1],'acpx','package.json'));console.log(JSON.stringify({name:p.name,repository:p.repository}))",root])
                package=json.loads(source);repo=package.get('repository',{});url=repo.get('url','') if isinstance(repo,dict) else str(repo)
                official=package.get('name')=='acpx' and url in {'https://github.com/openclaw/acpx','https://github.com/openclaw/acpx.git','git+https://github.com/openclaw/acpx.git'}
                outcome=update_decision(installed,latest,official,busy)
                if outcome=='upgrade':
                    upgrading=True
                    run(['npm','install','-g','--ignore-scripts','acpx@'+latest],timeout=120)
                    installed=run(['acpx','--version'])
                    if installed!=latest:raise RuntimeError('Post-update version mismatch')
                    outcome='updated_needs_smoke'
            result=dict(installed=installed,latest=latest,decision=outcome)
        except (RuntimeError,ValueError,subprocess.TimeoutExpired,OSError) as exc:
            if upgrading:
                raise RuntimeError('ACPX upgrade did not verify; do not launch work') from exc
            result=dict(decision='check_failed_using_existing',error=type(exc).__name__)
        store.update(task['id'],version_check=result)
        return result
