"""Shared claimed-turn runner and public notification bridge; no daemon."""
from __future__ import annotations
import argparse
import json
import os
try:
    from .bridge import Store
except ImportError:
    from bridge import Store
try:
    from .backends import backend_for
except ImportError:
    from backends import backend_for




def control(task, action, timeout=20):
    return backend_for(task['agent']).control(task, action, timeout)


def run_task(db_path, task_id, *, turn=None, launch_id=None, epoch=None):
    store = Store(db_path)
    task = store.get(task_id)
    legacy = turn is None and launch_id is None and epoch is None
    if legacy:
        # Old direct clients may launch an unclaimed first turn only. Never read
        # the latest successor for an unbound, delayed dispatch.
        if task['turn'] != 1 or task.get('control_epoch', 1) != 1 or task.get('launch_id'):
            raise ValueError('Unbound legacy dispatch cannot claim this task; use turn/launch-id/epoch')
        task = store.claim_launch(task_id, 1, 1)
        turn, launch_id, epoch = 1, task['launch_id'], 1
    if type(turn) is not int or type(epoch) is not int or not launch_id:
        raise ValueError('Dispatch requires turn, launch-id and epoch')
    try:
        from .recovery import recovery_card
    except ImportError:
        from recovery import recovery_card
    counter = 0
    tool_notices = 0
    answer_tail = ''
    def publish(kind, text, token, seq, **extra):
        nonlocal tool_notices
        # Native heartbeat batches these lines; cursor reads retain every event.
        if kind in {'started', 'version', 'model', 'permissions'}:
            return
        if kind == 'tool':
            tool_notices += 1
            if tool_notices > 12 and not text.endswith(' failed'):
                return
        print(json.dumps(dict(task_id=task_id,event=kind,text=text[:4000],
                              event_id=token,seq=seq,**dict({'recovery':recovery_card(task,db_path,seq)},**extra)), ensure_ascii=False),flush=True)
    def emit(kind,text,**extra):
        nonlocal counter, answer_tail
        counter += 1
        token = str(turn)+':'+str(counter)
        if kind == 'answer':
            answer_tail = (answer_tail + text + '\n')[-1000:]
        seq = store.event(task_id,token,kind,text,turn=turn,**extra)
        if seq:
            snapshot = store.get(task_id)
            publish(kind, text, token, seq, turn=turn, recovery=recovery_card(snapshot,db_path,seq), **extra)
    # Claim exactly the queued turn; duplicate launches never replay provider work.
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current = json.loads(db.execute('SELECT data FROM tasks WHERE id=?',(task_id,)).fetchone()[0])
        if (current['state'] != 'queued' or current['turn'] != turn
                or current.get('launch_turn') != turn or current.get('launch_id') != launch_id
                or current.get('control_epoch', 1) != epoch):
            raise ValueError('Dispatch identity is stale or turn already claimed')
        current.update(state='running',runner_pid=os.getpid())
        store._target_commands(current, 'target_started')
        store._save(db,current)
    task = store.get(task_id)
    emit('started',task['device']+'/'+task['agent'])
    try:
        result = backend_for(task['agent']).run(store, task, emit)
        text = None
        if task['agent'] == 'agy' and result.state == 'unknown':
            text = ('AGY task '+task_id+' turn '+str(turn)+' needs initiator attention: '
                    +(result.note or 'Prompt exited '+str(result.exit_code)))
        settled = store.finish_turn(task_id, turn, result.state, result.exit_code, result.stop_reason,
                                    note=result.note, text=text, answer_tail=answer_tail)
        if settled:
            publish(**settled)
        return 0 if result.state in {'completed', 'waiting_reply', 'cancelled'} else 1
    except Exception as exc:
        # Never infer remote worker death from an SSH transport failure.
        text = type(exc).__name__+': '+str(exc)[:500]
        if task['agent'] == 'agy':
            text = 'AGY task '+task_id+' turn '+str(turn)+' exited abnormally; outcome unknown. '+text
        settled = store.finish_turn(task_id,turn,'unknown',None,None,text=text,answer_tail=answer_tail)
        if settled:
            publish(**settled)
        return 1


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--db',required=True)
    parser.add_argument('--task',required=True)
    parser.add_argument('--turn', type=int)
    parser.add_argument('--launch-id')
    parser.add_argument('--epoch', type=int)
    opts=parser.parse_args()
    raise SystemExit(run_task(opts.db,opts.task,turn=opts.turn,launch_id=opts.launch_id,epoch=opts.epoch))
