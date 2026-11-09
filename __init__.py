"""One thin Hermes tool over ACPX and native background notifications."""
from __future__ import annotations
import json
import os
from pathlib import Path, PureWindowsPath
import shlex
import sys
from .bridge import Store, capture_origin
from .runner import control
from .transport import device_identity
from .backends import BACKENDS, prepare_start
from .skills import SKILLS_SCHEMA

SCHEMA={
    'name':'call_cli',
    'description':'Coordinate OMP, Codex, Pi or AGY via ACPX. Starts a native tracked background task with progress/completion returned to this exact Hermes session/topic. Use task_id for status/events/reply/intervene/cancel/close; never guess the latest session.',
    'parameters':{'type':'object','properties':{
        'action':{'type':'string','enum':['start','list','status','events','reply','intervene','cancel','close']},
        'task_id':{'type':'string'},
        'device':{'type':'string','description':'local or an existing SSH host alias from call-cli device table'},
        'remote_shell':{'type':'string','enum':['posix','powershell'],'default':'posix'},
        'agent':{'type':'string','enum':list(BACKENDS)},
        'model':{'type':'string','description':'Exact discovered ACP model ID; required for AGY; preserved on replies'},
        'effort':{'type':'string','description':'AGY advertised thought_level value; no suffix guessing'},
        'cwd':{'type':'string','description':'Absolute working directory on execution device'},
        'prompt':{'type':'string'},
        'command_id':{'type':'string','description':'Stable unique ID for an intervention; REQUIRED for intervene retries'},
        'write':{'type':'boolean','default':False,'description':'Only for already authorized file edits/tests'},
        'agy_approval':{'type':'string','enum':['approve-all','approve-reads'],'description':'AGY only: new tasks default to approve-all; approve-reads denies shell/write permission requests. Not a filesystem sandbox; independent of write reservation.'},
        'resource':{'type':'string','description':'Optional shared workspace identity across synchronized devices'},
        'skills':SKILLS_SCHEMA,
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
        if action in {'status','events','reply','intervene','cancel','close'}:
            task=store.get(args.get('task_id',''),origin)
        if action=='start':
            agent=args.get('agent');device=args.get('device','local');cwd=args.get('cwd','');prompt=args.get('prompt','')
            if not agent or not cwd or not prompt.strip():
                raise ValueError('start needs agent, absolute cwd and a nonempty prompt')
            if not (Path(cwd).is_absolute() or PureWindowsPath(cwd).is_absolute()):
                raise ValueError('cwd must be absolute on execution device')
            if device=='local':cwd=str(Path(cwd).resolve(strict=True))
            from .transport import command
            fields=prepare_start(agent,args.get('model'),args.get('effort'),args.get('agy_approval'))
            command(device,args.get('remote_shell','posix'),['acpx'])  # Validate host alias before any state change.
            timeout=args.get('timeout',900)
            if not isinstance(timeout,int) or not 30<=timeout<=7200:raise ValueError('timeout must be 30..7200')
            task=store.create(origin,device,agent,cwd,prompt,args.get('write',False) is True,args.get('resource',''),device_key=device_identity(device),skills=args.get('skills'))
            task=store.update(task['id'],shell=args.get('remote_shell','posix'),timeout=timeout,
                              needs_session=True,acpx_created=False,session_creation_attempted=False,**fields)
            return self.launch(store,task)
        if action=='intervene':
            prompt=args.get('prompt','')
            command_id=args.get('command_id','')
            if not prompt.strip():raise ValueError('intervene requires prompt')
            if not command_id:raise ValueError('intervene requires command_id')
            task,launch=store.intervene(task['id'],origin,prompt,command_id=command_id,legacy_hermes=True)
            if launch:return self.launch(store,task)
            status=task.get('intervention',{}).get('status','')
            note='Intervention pending cancellation settlement; use status or events to resume' if status=='pending' else f'Intervention status: {status or "none"}'
            return {'task':self.public(task),'note':note}
        if action in {'status','events'}:
            task,launch=store.advance_intervention(task['id'],origin)
            if launch:
                result=self.launch(store,task)
                task=store.get(task['id'],origin)
                if action=='status':return {'task':self.public(task),'native_process':result.get('native_process'),'note':result.get('note')}
            if action=='status':return {'task':self.public(task)}
            return {'task_id':task['id'],'events':store.events(task['id'],origin,args.get('after',0))}
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
                if success:
                    from .skills import cleanup
                    cleanup(store, task)
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
        task=store.claim_launch(task['id'],task['turn'])
        turn,launch_id=task['turn'],task['launch_id']
        try:
            result=self.ctx.dispatch_tool('terminal',args)
            result=json.loads(result) if isinstance(result,str) else result
            process_id=result.get('session_id','')
            if not process_id:raise RuntimeError('Native terminal did not return a tracked process')
            task=store.finish_launch(task['id'],turn,launch_id,process_id)
            return {'task':self.public(task),'native_process':result,
                    'note':'Completion wakes this original session; changed progress is batched by native heartbeat (60s).'}
        except Exception:
            store.finish_launch(task['id'],turn,launch_id)
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
