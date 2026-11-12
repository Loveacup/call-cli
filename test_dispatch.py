"""Run the exact synthetic turn captured by the test caller, not legacy argv."""
def run_dispatched(db_path, task_id):
    from bridge import Store
    from runner import run_task
    store = Store(db_path)
    task = store.get(task_id)
    if task.get('launch_turn') != task['turn']:
        task = store.claim_launch(task_id, task['turn'], task.get('control_epoch', 1))
    return run_task(db_path, task_id, turn=task['turn'], launch_id=task['launch_id'],
                    epoch=task.get('control_epoch', 1))
