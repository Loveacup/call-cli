"""One thin Hermes tool over ACPX and native background notifications."""
from __future__ import annotations
import json
import os
from pathlib import Path, PureWindowsPath
import shlex
import sys
from .bridge import Store, capture_origin
from .runner import control, device_identity

SCHEMA={
    'name':'call_cli',
    'description':'Coordinate OMP, Codex or Pi via ACPX. Starts a native tracked background task with progress/completion returned to this exact Hermes session/topic. Use task_id for status/events/reply/cancel/close; never guess the latest session.',
    'parameters':{'type':'object','properties':{
        'action':{'type':'string','enum':['start','list','status','events','reply','cancel','close']},
        'task_id':{'type':'string'},
        'device':{'type':'string','description':'local or an existing SSH host alias from call-cli device table'},
        'remote_shell':{'type':'string','enum':['posix','powershell'],'default':'posix'},
        'agent':{'type':'string','enum':['omp','codex','pi']},
        'cwd':{'type':'string','description':'Absolute working directory on execution device'},
        'prompt':{'type':'string'},
        'write':{'type':'boolean','default':False,'description':'Only for already authorized file edits/tests'},
        'resource':{'type':'string','description':'Optional shared workspace identity across synchronized devices'},
        'question_id':{'type':'string','description':'Exact pending question event ID for a reply'},
        'after':{'type':'integer','default':0},
        'timeout':{'type':'integer','minimum':30,'maximum':7200,'default':900}
    },'required':['action'],'additionalProperties':False}
}


class Coordinator:
    def __init__(self,ctx,home,get_env):
        self.ctx=ctx
        self.home=Path(home)
        self.get_env=get_env

    def handle(self,args):
        if self.ctx.get_config('enabled',False) is not True:
            raise PermissionError('call-cli bridge is disabled')
        origin=capture_origin(self.get_env,self.ctx.profile_name)
        store=Store(self.home/'call-cli'/'tasks.sqlite3')
        action=args['action']
        if action=='list':return {'tasks':[self.public(t) for t in store.list(origin)]}
        if action=='start':
            agent=args.get('agent');device=args.get('device','local');cwd=args.get('cwd','');prompt=args.get('prompt','')
            if agent not in {'omp','codex','pi'} or not cwd or not prompt.strip():
                raise ValueError('start needs agent, absolute cwd and a nonempty prompt')
            if not (Path(cwd).is_absolute() or PureWindowsPath(cwd).is_absolute()):
                raise ValueError('cwd must be absolute on execution device')
            if device=='local':cwd=str(Path(cwd).resolve(strict=True))
            from .runner import command
            command(device,args.get('remote_shell','posix'),['acpx'])  # Validate host alias before any state change.
            timeout=args.get('timeout',900)
            if not isinstance(timeout,int) or not 30<=timeout<=7200:raise ValueError('timeout must be 30..7200')
            task=store.create(origin,device,agent,cwd,prompt,args.get('write',False) is True,args.get('resource',''),device_key=device_identity(device))
            task=store.update(task['id'],shell=args.get('remote_shell','posix'),timeout=timeout)
            return self.launch(store,task)
        task=store.get(args.get('task_id',''),origin)
        if action=='status':return {'task':self.public(task)}
        if action=='events':return {'task_id':task['id'],'events':store.events(task['id'],origin,args.get('after',0))}
        if action=='reply':
            prompt=args.get('prompt','')
            if not prompt.strip():raise ValueError('reply requires prompt')
            task=store.reply(task['id'],origin,args.get('question_id',''),prompt)
            if task['state']=='waiting_reply':
                return {'task':self.public(task),'note':'Answer recorded; other pending questions remain'}
            return self.launch(store,task)
        if action=='cancel':
            task=store.request_cancel(task['id'],origin)
            return {'task':self.public(task),'note':'Cancellation requested; use status to confirm settlement'}
        if action=='close':
            task=store.begin_close(task['id'],origin)
            success=False
            try:
                from .updates import device_lock
                with device_lock(task):
                    result=control(task,'close')
                    if result.returncode:raise RuntimeError('ACPX close failed; task preserved')
                    result=control(task,'status')
                    out=result.stdout.decode('utf-8','replace')
                    if result.returncode or not ('no-session' in out or 'not running' in out):
                        raise RuntimeError('Closed session state not confirmed')
                success=True
            finally:
                store.finish_close(task['id'],task['turn'],success)
            return {'task':self.public(store.get(task['id'],origin))}
        raise ValueError('Unknown action')

    @staticmethod
    def public(task):
        return {k:v for k,v in task.items() if k!='prompt'}

    def launch(self,store,task):
        # The native terminal captures immutable parent_session_id and chat/topic here.
        # It queues progress/completion into the host's normal serialized session admission.
        args={'command':shlex.join([sys.executable,str(Path(__file__).with_name('runner.py')),
                                  '--db',str(store.path),'--task',task['id']]),
              'workdir':str(Path(__file__).parent),'background':True,'notify':True,
              'heartbeat':60,'timeout':task['timeout']+180}
        try:
            result=self.ctx.dispatch_tool('terminal',args)
            result=json.loads(result) if isinstance(result,str) else result
            process_id=result.get('session_id','')
            if not process_id:raise RuntimeError('Native terminal did not return a tracked process')
            task=store.update(task['id'],process_id=process_id)
            return {'task':self.public(task),'native_process':result,
                    'note':'Completion wakes this original session; changed progress is batched by native heartbeat (60s).'}
        except Exception:
            store.update(task['id'],state='unknown')
            raise


def register(ctx):
    def handle(args,**kwargs):
        from gateway.session_context import get_session_env
        from hermes_constants import get_hermes_home, get_default_hermes_root
        try:
            return json.dumps(Coordinator(ctx,Path(get_default_hermes_root(home=get_hermes_home()))/'plugin-data',get_session_env).handle(args),ensure_ascii=False)
        except (ValueError,PermissionError,KeyError,RuntimeError,OSError) as exc:
            return json.dumps({'error':str(exc),'error_type':type(exc).__name__})
    ctx.register_tool(name='call_cli',toolset='call_cli',schema=SCHEMA,handler=handle,
                      check_fn=lambda:ctx.get_config('enabled',False) is True,
                      description=SCHEMA['description'])
