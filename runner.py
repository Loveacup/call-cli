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


def run_task(db_path, task_id):
    store = Store(db_path)
    task = store.get(task_id)
    turn = task['turn']
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
                              event_id=token,seq=seq,**extra), ensure_ascii=False),flush=True)
    def emit(kind,text,**extra):
        nonlocal counter, answer_tail
        counter += 1
        token = str(turn)+':'+str(counter)
        if kind == 'answer':
            answer_tail = (answer_tail + text + '\n')[-1000:]
        seq = store.event(task_id,token,kind,text,turn=turn,**extra)
        if seq:
            publish(kind, text, token, seq, turn=turn, **extra)
    # Claim exactly the queued turn; duplicate launches never replay provider work.
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current = json.loads(db.execute('SELECT data FROM tasks WHERE id=?',(task_id,)).fetchone()[0])
        if current['state'] != 'queued' or current['turn'] != turn:
            raise ValueError('Turn already claimed or not queued')
        current.update(state='running',runner_pid=os.getpid())
        store._save(db,current)
    task = store.get(task_id)
    emit('started',task['device']+'/'+task['agent'])
    try:
        result = backend_for(task['agent']).run(store, task, emit)
        settled = store.finish_turn(task_id, turn, result.state, result.exit_code, result.stop_reason,
                                    note=result.note, answer_tail=answer_tail)
        if settled:
            publish(**settled)
        return 0 if result.state in {'completed', 'waiting_reply', 'cancelled'} else 1
    except Exception as exc:
        # Never infer remote worker death from an SSH transport failure.
        settled = store.finish_turn(task_id,turn,'unknown',None,None,
                                    text=type(exc).__name__+': '+str(exc)[:500], answer_tail=answer_tail)
        if settled:
            publish(**settled)
        return 1


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--db',required=True)
    parser.add_argument('--task',required=True)
    opts=parser.parse_args()
    raise SystemExit(run_task(opts.db,opts.task))
