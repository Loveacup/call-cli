"""Credential-free recovery coordinates and honest host responsibility hints."""
def public_close_receipt(receipt):
    return {key:receipt[key] for key in
            ('state','phase','stage','reason_code','external_close_attempted','resource_state',
             'business_state','risk','arbitraryDetachedDescendantsProven','operation') if key in receipt}


def _is_settled_missing_final(task):
    if task.get('state') != 'unknown' or task.get('runner_pid'):
        return False
    ts = task.get('turn_settlement')
    if not isinstance(ts, dict) or ts.get('protocol_settled') is not True:
        return False
    req = task.get('backend_request_id')
    if ts.get('turn') != task.get('turn') or not req or ts.get('request_id') != req:
        return False
    if task.get('launch_id') and ts.get('launch_id') != task.get('launch_id'):
        return False
    return True


def recovery_card(task, db_path=None, seq=0, host=None):
    surface = task.get('initiating_surface', host or 'unknown')
    exact_reconcile = (task['state'] == 'unknown' and task['device'] == 'local'
                       and bool(task.get('acpx_record_id')) and task.get('agy_swarm') != 'native')
    settled_missing_final = exact_reconcile and _is_settled_missing_final(task)
    if task['state'] == 'unknown':
        if not exact_reconcile:
            next_action = 'retain HOLD; exact-record recovery unavailable here; resolve identity/device via authorized operator'
        elif settled_missing_final:
            next_action = ('protocol settled (end_turn) without final answer (business outcome incomplete); '
                           'inspect artifacts read-only, then run release_only(task_id, expected_turn) '
                           'for single exact-record close and no-session readback to release write lock without abandon=true; no replay')
        elif host in {'hermes-native','standalone','omp-extension'}:
            action = 'reconcile' if host == 'hermes-native' else 'close (probe-only)'
            next_action = action + ' exact record without replay'
        else:
            next_action = 'retain HOLD pending exact-record probe-only reconciliation through an authorized available entry point; no replay'
        if task.get('agy_swarm') == 'native':
            action = 'reconcile' if host=='hermes-native' else 'close (receipt-only)'
            next_action = ('retain native worker HOLD; '+action+' validates an existing owner cleanup receipt only; '
                'no new owner, load or repeated close; missing receipt requires authorized native evidence, not ACPX')
    else:
        next_action = 'inspect exact task before explicit control'
    released = task.get('resource_state') == 'released'
    if released:
        next_action = 'Resource ownership released; business outcome unchanged; do not replay the fenced task'
        exact_reconcile = False
    elif task['state'] == 'unknown' and not settled_missing_final:
        next_action += ('; current authorized callers can use release_only(task_id, expected_turn) for strict close and readback where supported; '
                        'if unavailable or still HOLD, explicitly accept risk with release_only(task_id, expected_turn, abandon=true); '
                        'native/detached work may still be active; no stop or success claim')
    return dict(task_id=task['id'], device=task['device'],
        session=task['session'], acpx_record_id=task.get('acpx_record_id'), turn=task['turn'],
        control_epoch=task.get('control_epoch', 1), controller=task.get('controller', task['origin']),
        seq=seq, questions=list(task.get('questions', [])),
        commands={key:value.get('status') for key,value in task.get('commands', {}).items()},
        reservation=dict(write=task['write'],cwd=task['cwd'],state=task.get('resource_state',task['state'])),
        responsibility=dict(coverage='uncovered', host=surface, pre_exit_hook=False,
            notification_return='configured' if surface == 'hermes-native' else 'unverified',
            exit_interception='uncovered', successor_takeover='uncovered',
            handoff_verified=False, note='No verified exit interception or successor handoff; current controller must retain this card'),
        capabilities=dict(exact_record_reconcile=exact_reconcile, release_only=task['state']=='unknown' and not released), next_action=next_action)
