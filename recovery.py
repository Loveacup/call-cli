"""Credential-free recovery coordinates and honest host responsibility hints."""
import os

_ALLOWED_AGENTS = frozenset({'omp', 'codex', 'pi', 'agy'})
_ALLOWED_BUSINESS_STATES = frozenset({
    'queued', 'running', 'waiting_reply', 'cancelling',
    'cancelled', 'completed', 'closing', 'closed', 'unknown',
})
_ALLOWED_RESOURCE_STATES = _ALLOWED_BUSINESS_STATES | frozenset({
    'released', 'superseded',
})
_ALLOWED_RECEIPT_STATES = frozenset({
    'released', 'unknown', 'not_prepared',
})
_ALLOWED_RECEIPT_OPERATIONS = frozenset({
    'release-only', 'release_only', 'explicit-abandonment', 'unlaunched-queue-retirement',
    'preprompt-failure', 'native-recovery', 'close', 'reconcile',
    'probe', 'resolve',
})
_ALLOWED_AGY_MODES = frozenset({'single', 'swarm', 'teamwork', 'dynamic'})
_ALLOWED_NATIVE_PHASES = frozenset({
    'initializing', 'foreground', 'background_pending',
    'settled', 'cancelled', 'unknown', 'dynamic_pending',
})
_ALLOWED_STOP_REASONS = frozenset({
    'end_turn', 'cancelled', 'max_tokens', 'max_turn_requests', 'refusal',
})
_ALLOWED_INTERVENTION_STATUSES = frozenset({
    'pending', 'queued', 'target_started', 'target_completed',
    'target_cancelled', 'target_unknown', 'withdrawn', 'superseded',
    'release-revoked',
})
_ALLOWED_RESPONSE_PENDING_REASONS = frozenset({
    'board_navigation_only', 'native_handoff_only',
})
_ALLOWED_TOOL_KINDS = frozenset({
    'read', 'edit', 'delete', 'move', 'search',
    'execute', 'think', 'fetch', 'switch_mode', 'other',
})
_ALLOWED_BLOCKER_REASONS = frozenset({
    'incomplete_question', 'history_ambiguous', 'unresolved_question',
})
_ALLOWED_BLOCKER_ACTIONS = frozenset({
    'observe', 'dialogue', 'cancel', 'inspect_origin', 'reconcile_hold',
})
_ALLOWED_REASON_CODES = frozenset({
    'prefix_changed', 'history_shrank', 'tool_changed', 'dialogue_missing',
    'dialogue_boundary_missing', 'dialogue_reordered', 'live_message_changed',
    'live_message_unconsumed', 'resumed_boundary_missing',
    'resumed_live_tool_conflict', 'resumed_live_tool_regressed',
    'drained_tool_status_regressed', 'drained_tool_missing',
    'drained_tool_conflict', 'inverted_tool_replay',
})
_ALLOWED_CALL_SITES = frozenset({
    'baseline', 'initial_replay', 'section', 'dialogue_replay',
    'history_sections', 'resumed_boundary', 'drain_reconcile',
    'resumed_live_tool_reconcile', 'response_floor_replay', 'load',
    'dynamic_load', 'dynamic_replay', 'dynamic_drain_reconcile',
})
_ALLOWED_SLOT_TYPES = frozenset({
    'agent_message_chunk', 'user_message_chunk', 'tool_call',
})
_ALLOWED_TOOL_STATUSES = frozenset({
    'pending', 'in_progress', 'completed', 'failed',
})
_HISTORY_DIAG_INT_KEYS = (
    'turn', 'interaction_seq', 'index', 'previous_len', 'current_len',
    'old_len', 'new_len', 'drained_before_load',
)
_HISTORY_DIAG_BOOL_KEYS = (
    'is_tail', 'background_growth', 'concat_agent_is_prefix',
    'same_tool_id_prefix', 'same_user_prefix', 'load_retried',
    'same_tool_id', 'old_has_kind', 'new_has_kind', 'same_kind',
    'same_swarm', 'text_equal', 'is_prefix', 'rstrip_is_prefix',
)
_HISTORY_DIAG_SLOT_KEYS = (
    'old_type', 'new_type', 'prev_slot_type', 'next_new_slot_type',
)
_HISTORY_DIAG_STATUS_KEYS = ('old_status', 'new_status')


def _enum_or_default(value, allowed, default, *, allow_none=False):
    if allow_none and value is None:
        return None
    if isinstance(value, str) and value in allowed:
        return value
    return default


def _safe_receipt_projection(receipt):
    if not isinstance(receipt, dict):
        return None
    ext = receipt.get('external_close_attempted')
    return {
        'state': _enum_or_default(receipt.get('state'), _ALLOWED_RECEIPT_STATES, 'unknown', allow_none=True),
        'operation': _enum_or_default(receipt.get('operation'), _ALLOWED_RECEIPT_OPERATIONS, 'unknown', allow_none=True),
        'business_state': _enum_or_default(receipt.get('business_state'), _ALLOWED_BUSINESS_STATES, 'unknown', allow_none=True),
        'external_close_attempted': ext if type(ext) is bool else None,
    }


def resource_recovery_projection(task, receipt):
    """Return the fixed 6-key closed-set resource recovery projection."""
    task = task if isinstance(task, dict) else {}
    raw_id = task.get('id')
    raw_turn = task.get('turn')
    raw_task_state = task.get('state')
    raw_resource_state = task.get('resource_state')
    safe_receipt = _safe_receipt_projection(receipt)

    if isinstance(receipt, dict) and receipt.get('business_state') is not None:
        business_state = _enum_or_default(receipt.get('business_state'), _ALLOWED_BUSINESS_STATES, 'unknown')
    else:
        business_state = _enum_or_default(raw_task_state, _ALLOWED_BUSINESS_STATES, 'unknown')

    if safe_receipt is not None and safe_receipt['state'] == 'released':
        resource_state = 'released'
    elif raw_resource_state is not None:
        resource_state = _enum_or_default(raw_resource_state, _ALLOWED_RESOURCE_STATES, 'unknown')
    else:
        resource_state = _enum_or_default(raw_task_state, _ALLOWED_RESOURCE_STATES, 'unknown')

    return {
        'task_id': raw_id if isinstance(raw_id, str) else None,
        'turn': raw_turn if (type(raw_turn) is int and raw_turn >= 1) else None,
        'business_state': business_state,
        'resource_state': resource_state,
        'write': task.get('write', False) is True,
        'receipt': safe_receipt,
    }


def _safe_history_diagnostic(raw):
    if not isinstance(raw, dict):
        return None
    hd = {}
    if 'reason_code' in raw:
        hd['reason_code'] = _enum_or_default(raw['reason_code'], _ALLOWED_REASON_CODES, 'other')
    if 'call_site' in raw:
        hd['call_site'] = _enum_or_default(raw['call_site'], _ALLOWED_CALL_SITES, 'other', allow_none=True)
    for key in _HISTORY_DIAG_INT_KEYS:
        if key in raw:
            val = raw[key]
            hd[key] = val if type(val) is int else None
    for key in _HISTORY_DIAG_BOOL_KEYS:
        if key in raw:
            val = raw[key]
            hd[key] = val if type(val) is bool else None
    for key in _HISTORY_DIAG_SLOT_KEYS:
        if key in raw:
            hd[key] = _enum_or_default(raw[key], _ALLOWED_SLOT_TYPES, 'other', allow_none=True)
    for key in _HISTORY_DIAG_STATUS_KEYS:
        if key in raw:
            hd[key] = _enum_or_default(raw[key], _ALLOWED_TOOL_STATUSES, 'other', allow_none=True)
    return hd


def diagnostic_projection(task):
    """Return strictly closed-set audited diagnostic enums, ints, and bools."""
    task = task if isinstance(task, dict) else {}
    exit_code = task.get('exit_code')
    questions = task.get('questions')
    intervention = task.get('intervention')
    iv_status = intervention.get('status') if isinstance(intervention, dict) else None

    obs = task.get('native_observation')
    if isinstance(obs, dict):
        raw_tools = obs.get('pending_tools')
        tools = [t for t in raw_tools if isinstance(t, dict)] if isinstance(raw_tools, list) else []
        tool_kinds = sorted({
            _enum_or_default(t.get('kind'), _ALLOWED_TOOL_KINDS, 'other')
            for t in tools
        })
        blockers = []
        raw_blockers = obs.get('question_blockers')
        if isinstance(raw_blockers, list):
            for entry in raw_blockers:
                if not isinstance(entry, dict):
                    continue
                seq = entry.get('interaction_seq')
                reason = entry.get('reason')
                if type(seq) is not int or seq < 0 or not (isinstance(reason, str) and reason in _ALLOWED_BLOCKER_REASONS):
                    continue
                raw_actions = entry.get('available_actions')
                actions = (
                    [a for a in raw_actions if isinstance(a, str) and a in _ALLOWED_BLOCKER_ACTIONS]
                    if isinstance(raw_actions, list) else []
                )
                blockers.append({
                    'interaction_seq': seq,
                    'reason': reason,
                    'available_actions': actions,
                    'blocks_completion': entry.get('blocks_completion') is True,
                })
        input_avail = obs.get('input_available')
        native_diag = {
            'history_degraded': obs.get('history_degraded') is True,
            'input_available': input_avail if type(input_avail) is bool else None,
            'response_pending_reason': _enum_or_default(
                obs.get('response_pending_reason'), _ALLOWED_RESPONSE_PENDING_REASONS, None, allow_none=True
            ),
            'pending_tool_count': len(tools),
            'pending_tool_kinds': tool_kinds,
            'question_blockers': blockers,
            'history_diagnostic': _safe_history_diagnostic(obs.get('history_diagnostic')),
        }
    else:
        native_diag = None

    return {
        'agent': _enum_or_default(task.get('agent'), _ALLOWED_AGENTS, 'unknown'),
        'agy_mode': _enum_or_default(task.get('agy_mode'), _ALLOWED_AGY_MODES, None, allow_none=True),
        'native_phase': _enum_or_default(task.get('native_phase'), _ALLOWED_NATIVE_PHASES, None, allow_none=True),
        'stop_reason': _enum_or_default(task.get('stop_reason'), _ALLOWED_STOP_REASONS, 'other', allow_none=True),
        'exit_code': exit_code if type(exit_code) is int else None,
        'pending_question_count': len(questions) if isinstance(questions, (list, tuple)) else 0,
        'intervention_status': 'none' if iv_status is None else _enum_or_default(iv_status, _ALLOWED_INTERVENTION_STATUSES, 'unknown'),
        'native_diagnostic': native_diag,
    }


def _runner_inactive(task):
    pid = task.get('runner_pid')
    if pid is None or (type(pid) is int and pid == 0):
        return True
    if type(pid) is not int or pid < 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (OSError, TypeError, ValueError, OverflowError):
        pass
    return False


def public_close_receipt(receipt):
    return {key:receipt[key] for key in
            ('state','phase','stage','reason_code','external_close_attempted','resource_state',
             'business_state','risk','arbitraryDetachedDescendantsProven','operation') if key in receipt}


def _is_settled_missing_final(task):
    if task.get('state') != 'unknown' or task.get('backend') == 'oar' or not _runner_inactive(task):
        return False
    ts = task.get('turn_settlement')
    req = task.get('backend_request_id')
    if ts is None:
        # Legacy turns have no settlement object; never invent their request ID.
        return (task.get('device') == 'local' and bool(task.get('acpx_record_id'))
                and task.get('agy_swarm') != 'native'
                and type(task.get('exit_code')) is int and task['exit_code'] == 0
                and task.get('stop_reason') in {'end_turn', 'cancelled'}
                and req == str(task.get('id')) + ':' + str(task.get('turn')))
    if not isinstance(ts, dict) or ts.get('protocol_settled') is not True:
        return False
    if ts.get('turn') != task.get('turn') or not req or ts.get('request_id') != req:
        return False
    if task.get('launch_id') and ts.get('launch_id') != task.get('launch_id'):
        return False
    return True


def recovery_card(task, db_path=None, seq=0, host=None):
    surface = task.get('initiating_surface', host or 'unknown')
    exact_reconcile = (task['state'] == 'unknown' and task['device'] == 'local'
                       and bool(task.get('acpx_record_id'))
                       and task.get('agy_swarm') != 'native'
                       and task.get('backend') != 'oar')
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
        if task.get('backend') == 'oar':
            action = 'reconcile' if host == 'hermes-native' else 'close (receipt-only)'
            next_action = ('retain OAR HOLD; ' + action + ' validates an existing OAR single-owner cleanup receipt only (never ACPX); '
                'no second owner or cross-backend replay; missing receipt requires explicit release_only(task_id, expected_turn, abandon=true)')
        elif task.get('agy_swarm') == 'native':
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
