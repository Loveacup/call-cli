"""Credential-free recovery coordinates and honest host responsibility hints."""
def public_close_receipt(receipt):
    return {key:receipt[key] for key in
            ('state','phase','stage','reason_code','external_close_attempted') if key in receipt}


def recovery_card(task, db_path=None, seq=0, host=None):
    surface = task.get('initiating_surface', host or 'unknown')
    exact_reconcile = (task['state'] == 'unknown' and task['device'] == 'local'
                       and bool(task.get('acpx_record_id')))
    if task['state'] == 'unknown':
        if not exact_reconcile:
            next_action = 'retain HOLD; exact-record recovery unavailable here; resolve identity/device via authorized operator'
        elif host in {'hermes-native','standalone','omp-extension'}:
            action = 'reconcile' if host == 'hermes-native' else 'close (probe-only)'
            next_action = action + ' exact record without replay'
        else:
            next_action = 'retain HOLD pending exact-record probe-only reconciliation through an authorized available entry point; no replay'
    else:
        next_action = 'inspect exact task before explicit control'
    return dict(task_id=task['id'], device=task['device'],
        session=task['session'], acpx_record_id=task.get('acpx_record_id'), turn=task['turn'],
        control_epoch=task.get('control_epoch', 1), controller=task.get('controller', task['origin']),
        seq=seq, questions=list(task.get('questions', [])),
        commands={key:value.get('status') for key,value in task.get('commands', {}).items()},
        reservation=dict(write=task['write'],cwd=task['cwd'],state=task['state']),
        responsibility=dict(coverage='uncovered', host=surface, pre_exit_hook=False,
            notification_return='configured' if surface == 'hermes-native' else 'unverified',
            exit_interception='uncovered', successor_takeover='uncovered',
            handoff_verified=False, note='No verified exit interception or successor handoff; current controller must retain this card'),
        capabilities=dict(exact_record_reconcile=exact_reconcile), next_action=next_action)
