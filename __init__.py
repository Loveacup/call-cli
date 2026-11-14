"""One thin Hermes tool over ACPX and native background notifications."""
from __future__ import annotations
import json
import os
from pathlib import Path, PureWindowsPath
import shlex
import sqlite3
import sys
from .bridge import capture_origin
from .native_continuity import NativeStore as Store
from .transport import device_identity
from .backends import BACKENDS, prepare_start
from .skills import SKILLS_SCHEMA
from .recovery import recovery_card

SCHEMA={
    'name':'call_cli',
    'description':'Coordinate OMP, Codex, Pi or AGY via ACPX. Starts a native tracked background task with progress/completion returned to this exact Hermes session/topic. Use task_id for status/events/reply/intervene/cancel/close; never guess the latest session.',
    'parameters':{'type':'object','properties':{
        'action':{'type':'string','enum':['start','list','status','events','reply','dialogue','permission','intervene','cancel','close','reconcile','release_only'], 'description':'release_only: explicit original-controller unknown-task resource release, not business success or continuation; requires expected_turn and a settled local exact-record runner'},
        'task_id':{'type':'string'},
        'permission_id':{'type':'string'},
        'script_sha256':{'type':'string'},
        'decision':{'type':'string','enum':['allow_once','reject_once']},
        'device':{'type':'string','description':'local or an existing SSH host alias from call-cli device table'},
        'remote_shell':{'type':'string','enum':['posix','powershell'],'default':'posix'},
        'agent':{'type':'string','enum':list(BACKENDS)},
        'model':{'type':'string','description':'AGY oc/oa/4c/4a/4h: bare=single, s=Swarm, t=Teamwork. Omitted=ocs, never task-selected. Advertised IDs supported; shorthand/mode conflicts reject'},
        'effort':{'type':'string','description':'AGY advertised thought_level only; standalone OC/OA have no effort option; no suffix guessing'},
        'cwd':{'type':'string','description':'Absolute working directory on execution device'},
        'prompt':{'type':'string'},
        'command_id':{'type':'string','description':'Stable intent ID; required for dialogue/intervene and cross-controller or new no-question reply; reuse on retry'},
        'write':{'type':'boolean','default':False,'description':'Only for already authorized file edits/tests'},
        'agy_approval':{'type':'string','enum':['approve-all','approve-reads'],'description':'AGY only: new tasks default to approve-all; approve-reads denies shell/write permission requests. Not a filesystem sandbox; independent of write reservation.'},
        'agy_swarm':{'type':'string','enum':['native','off'],'description':'AGY route override for advertised IDs; must match shorthand: bare=off, s=native'},
        'agy_mode':{'type':'string','enum':['single','swarm','teamwork','dynamic'],'description':'Explicit execution mode; native modes require their advertised command; Dynamic requires exact script approval'},
        'agy_worker_model':{'type':'string','description':'Optional native worker-model preference forwarded to AGY; no default or per-member completion gate'},
        'resource':{'type':'string','description':'Optional shared workspace identity across synchronized devices'},
        'skills':SKILLS_SCHEMA,
        'question_id':{'type':'string','description':'Exact pending question event ID for a reply'},
        'expected_turn':{'type':'integer','description':'Observed turn; required for dialogue, release_only, cross-controller continuation, new no-question reply and native close/reconcile recovery'},
        'after':{'type':'integer','default':0},
        'clear_timeout':{'type':'boolean','description':'reply/intervene only: clear an inherited execution limit on the next settled continuation; queued clearing is refused'}
    },'required':['action'],'additionalProperties':False}
}


class Coordinator:
    def __init__(self,ctx,home,get_env,*,hermes_home=None,hermes_root=None):
        self.ctx=ctx
        self.home=Path(home)
        self.get_env=get_env
        self.hermes_home=hermes_home
        self.hermes_root=hermes_root
    def handle(self, args):
        result = self._handle(args)
        if 'task' in result:
            result['recovery'] = recovery_card(result['task'], self.home/'call-cli/tasks.sqlite3', host='hermes-native')
        elif 'tasks' in result:
            result['recovery'] = [recovery_card(task, self.home/'call-cli/tasks.sqlite3', host='hermes-native')
                                  for task in result['tasks']]
        return result


    def _handle(self,args):
        if self.ctx.get_config('enabled',False) is not True:
            raise PermissionError('call-cli bridge is disabled')
        if 'timeout' in args:
            raise ValueError('call_cli does not accept timeout; omit it. Explicit execution budgets remain available through CLI/Python start.')
        if set(args) - set(SCHEMA['parameters']['properties']):
            raise ValueError('Unknown call_cli field')
        if 'clear_timeout' in args and args.get('action') not in {'reply', 'intervene'}:
            raise ValueError('clear_timeout requires reply or intervene')
        origin=capture_origin(self.get_env,self.ctx.profile_name)
        action=args['action']
        store=Store(self.home/'call-cli'/'tasks.sqlite3', self.hermes_home, self.hermes_root, action=action)
        if action=='list':return {'tasks':[self.public(t) for t in store.list(origin)]}
        if action in {'status','events','reply','dialogue','permission','intervene','cancel','close','reconcile','release_only'}:
            task=store.get(args.get('task_id',''),origin)
        if action=='start':
            agent=args.get('agent');device=args.get('device','local');cwd=args.get('cwd','');prompt=args.get('prompt','')
            if not agent or not cwd or not prompt.strip():
                raise ValueError('start needs agent, absolute cwd and a nonempty prompt')
            if not (Path(cwd).is_absolute() or PureWindowsPath(cwd).is_absolute()):
                raise ValueError('cwd must be absolute on execution device')
            if device=='local':cwd=str(Path(cwd).resolve(strict=True))
            from .transport import command
            fields=prepare_start(agent,args.get('model'),args.get('effort'),args.get('agy_approval'),
                                 agy_swarm=args.get('agy_swarm'),agy_worker_model=args.get('agy_worker_model'),agy_mode=args.get('agy_mode'))
            command(device,args.get('remote_shell','posix'),['acpx'])  # Validate host alias before any state change.
            task=store.create(origin,device,agent,cwd,prompt,args.get('write',False) is True,args.get('resource',''),device_key=device_identity(device),skills=args.get('skills'),initiating_surface='hermes-native')
            task=store.update(task['id'],shell=args.get('remote_shell','posix'),timeout=None,continuation_version=2,
                              needs_session=True,acpx_created=False,session_creation_attempted=False,**fields)
            return self.launch(store,task)
        if action=='permission':
            task=store.permission(task['id'],origin,expected_turn=args.get('expected_turn'), command_id=args.get('command_id'),
                permission_id=args.get('permission_id'),script_sha256=args.get('script_sha256'),decision=args.get('decision'))
            return {'task':self.public(task),'note':'Exact single-use permission recorded; send/return evidence remains separate'}
        if action=='dialogue':
            task=store.dialogue(task['id'],origin,args.get('prompt',''), expected_turn=args.get('expected_turn'),
                                command_id=args.get('command_id'), question_id=args.get('question_id'))
            return {'task':self.public(task),'note':'Dialogue accepted by bound owner queue; not sent or adopted until evidence returns'}
        if action=='intervene':
            prompt=args.get('prompt','')
            command_id=args.get('command_id','')
            if not prompt.strip():raise ValueError('intervene requires prompt')
            if not command_id:raise ValueError('intervene requires command_id')
            task,launch=store.intervene(task['id'],origin,prompt,command_id=command_id,legacy_hermes=True,
                                        clear_timeout=args.get('clear_timeout',False), expected_turn=args.get('expected_turn'))
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
            task=store.reply(task['id'],origin,args.get('question_id',''),prompt,
                             clear_timeout=args.get('clear_timeout',False), expected_turn=args.get('expected_turn'),
                             command_id=args.get('command_id'))
            if task['state']=='waiting_reply':
                return {'task':self.public(task),'note':'Answer recorded; other pending questions remain'}
            if task.get('launch_turn') == task['turn']:
                return {'task':self.public(task),'note':'Reply already admitted; no duplicate launch'}
            return self.launch(store,task)
        if action=='cancel':
            task=store.request_cancel(task['id'],origin)
            return {'task':self.public(task),'note':'Cancellation requested; use status to confirm settlement'}
        if action in {'close','reconcile','release_only'}:
            toolchain_bin = self.ctx.get_config('local_toolchain_bin', '')
            backend = None
            if toolchain_bin:
                from .release import NativeBackend
                backend = NativeBackend(toolchain_bin=toolchain_bin)
            from .release import close_task, recover_native
            receipt = (close_task(store,origin,task['id'],args.get('expected_turn'),backend=backend) if action == 'close'
                       else recover_native(store,origin,task['id'],args.get('expected_turn'),action,backend=backend))
            if receipt['state'] == 'released':
                from .skills import cleanup
                cleanup(store,task)
            return {'task':self.public(store.get(task['id'],origin)),'receipt':receipt}
        raise ValueError('Unknown action')

    @staticmethod
    def public(task):
        def project(value):
            if isinstance(value,dict):
                return {key:project(item) for key,item in value.items() if key not in {'prompt','reply_commands','native_snapshot'}}
            if isinstance(value,list):
                return [project(item) for item in value]
            return value
        return project(task)

    def launch(self,store,task):
        # The native terminal captures immutable parent_session_id and chat/topic here.
        # It queues progress/completion into the host's normal serialized session admission.
        toolchain_bin = self.ctx.get_config('local_toolchain_bin', '') if task['device'] == 'local' else ''
        prefix = []
        if toolchain_bin:
            from .transport import local_toolchain_env
            prefix = ['env', 'PATH=' + local_toolchain_env(toolchain_bin)['PATH']]
        task=store.claim_launch(task['id'],task['turn'],task.get('control_epoch',1))
        turn,launch_id=task['turn'],task['launch_id']
        args={'command':shlex.join(prefix+[sys.executable,str(Path(__file__).with_name('runner.py')),
                                  '--db',str(store.path),'--task',task['id'],
                                  '--turn',str(turn),'--launch-id',launch_id,
                                  '--epoch',str(task.get('control_epoch',1))]),
              'workdir':str(Path(__file__).parent),'background':True,'notify':True,
              'heartbeat':60}
        if task.get('timeout') is not None:
            args['timeout']=task['timeout']+180
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
            home=Path(get_hermes_home()).resolve()
            task_root=Path(get_default_hermes_root(home=home)).resolve()
            metadata_home=metadata_root=None
            try:
                from hermes_cli.profiles import get_active_profile_name
                if get_active_profile_name() == ctx.profile_name:
                    metadata_home=home
                    metadata_root=Path(get_default_hermes_root()).resolve()
            except (ImportError,OSError,RuntimeError,ValueError):
                pass  # Unavailable lineage identity must not disable exact-origin access.
            return json.dumps(Coordinator(ctx,task_root/'plugin-data',get_session_env,
                hermes_home=metadata_home,hermes_root=metadata_root).handle(args),ensure_ascii=False)
        except (ValueError,PermissionError,KeyError,RuntimeError,OSError,sqlite3.Error) as exc:
            return json.dumps({'error':str(exc),'error_type':type(exc).__name__})
    ctx.register_tool(name='call_cli',toolset='call_cli',schema=SCHEMA,handler=handle,
                      check_fn=lambda:ctx.get_config('enabled',False) is True,
                      description=SCHEMA['description'])
