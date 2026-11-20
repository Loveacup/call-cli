"""AGY-only approval, prompt guidance and business-outcome policy.

The coordinator, transport and task lifecycle remain shared with other agents.
"""
import json
import re
try:
    from .acpx_backend import AcpxBackend
except ImportError:
    from acpx_backend import AcpxBackend


AGY_FATAL_LINE = '\u26a0 Agent execution terminated due to error.'
AGY_INVALID_OUTPUT_LINE = '\u26a0 Model produced invalid output.'
AGY_INTERRUPTED_LINE = 'The stream was interrupted. Please continue the task you were working on.'
_PROVIDER_ERROR_LINE_RE = re.compile(r'^(?:\u26a0\s*)?API error\b[^\r\n]*$', re.IGNORECASE)
BOARD_HINT_RE = re.compile(
    r'Run /swarm board(?: \(default shortcut [^()\r\n]+\))?'
    r'(?: to open the live board)?\.?'
)
AGY_APPROVALS = {'skip-permissions', 'approve-all', 'approve-reads'}


def is_skip_permissions(task):
    return task.get('execution_policy') == 'native-skip' or task.get('agy_approval') == 'skip-permissions'


def permission_mode(task):
    # Missing persisted fields are legacy tasks, not permission to widen access.
    value = task.get('agy_approval')
    if value is not None:
        if value not in AGY_APPROVALS:
            raise ValueError('agy_approval must be skip-permissions, approve-all, or approve-reads')
        return 'approve-all' if value == 'skip-permissions' else value
    if task.get('execution_policy') == 'native-skip':
        return 'approve-all'
    return 'approve-all' if task['write'] else 'approve-reads'


def prompt_guidance(task):
    mode = permission_mode(task)
    if is_skip_permissions(task):
        return ('Prefer native read/search tools for reading and searching. '
                'Tool approval policy: skip-permissions (native CLI permission, sandbox, and directory prompts skipped; parent fallback approve-all). '
                'Shell execution is approved within the stated task scope. '
                'This policy does not expand business task authorization, task identity, or writer lock.\n\n')
    return ('Prefer native read/search tools for reading and searching. '
            'Tool approval policy: '+mode+'. '
            + ('Shell execution is approved within the stated task scope. '
               if mode == 'approve-all' else
               'Read/search requests are approved; shell/write requests are denied non-interactively. ')
            + 'This approval policy is not a filesystem sandbox and does not expand task authorization.\n\n')


def fatal_error(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return bool(lines) and lines[-1] == AGY_FATAL_LINE


def is_board_navigation_only(text):
    lines = [line.strip() for line in (text or '').splitlines() if line.strip()]
    return bool(lines) and all(BOARD_HINT_RE.fullmatch(line) for line in lines)


def is_standalone_incomplete_diagnostic(text):
    lines = [line.strip() for line in (text or '').splitlines() if line.strip()]
    non_nav = [line for line in lines if not BOARD_HINT_RE.fullmatch(line)]
    if not non_nav:
        return None
    has_interrupted = False
    has_provider_error = False
    has_invalid_output = False
    for line in non_nav:
        if line == AGY_INTERRUPTED_LINE:
            has_interrupted = True
        elif _PROVIDER_ERROR_LINE_RE.fullmatch(line):
            has_provider_error = True
        elif line == AGY_INVALID_OUTPUT_LINE:
            has_invalid_output = True
        else:
            return None
    if has_interrupted:
        return 'interrupted'
    if has_provider_error:
        return 'provider_error'
    if has_invalid_output:
        return 'invalid_output'
    return None


def outcome_note(decoder, exit_code, state):
    reason = None
    standalone_diag = (
        is_standalone_incomplete_diagnostic(decoder.final_answer)
        if state == 'completed' and decoder.stop_reason == 'end_turn'
        else None
    )
    if fatal_error(decoder.text):
        reason = 'AGY reported a fatal execution error (final line "'+AGY_FATAL_LINE+'")'
    elif decoder.final_answer.strip() == AGY_INVALID_OUTPUT_LINE or standalone_diag == 'invalid_output':
        # Observed error-only response, not arbitrary prose mentioning an error.
        return ('AGY reported "'+AGY_INVALID_OUTPUT_LINE+'"; no usable final answer '
                'although the protocol ended '+str(decoder.stop_reason)+' with exit '+str(exit_code)
                +'; turn not completed, outcome unknown, no automatic replay.')
    elif standalone_diag == 'interrupted':
        return ('AGY reported stream interruption ("'+AGY_INTERRUPTED_LINE+'"); no usable final answer '
                'although the protocol ended '+str(decoder.stop_reason)+' with exit '+str(exit_code)
                +'; turn not completed, outcome unknown, no automatic replay.')
    elif standalone_diag == 'provider_error':
        return ('AGY reported a provider API error diagnostic without a usable final answer '
                'although the protocol ended '+str(decoder.stop_reason)+' with exit '+str(exit_code)
                +'; turn not completed, outcome unknown, no automatic replay.')
    elif (exit_code == 0 and not decoder.failed and decoder.stop_reason == 'end_turn'
          and state == 'completed' and decoder.tool_failed and not decoder.final_answer.strip()):
        reason = 'AGY tool failed without a final answer'
    if reason:
        return (reason+' although the protocol ended '+str(decoder.stop_reason)+' with exit '
                +str(exit_code)+'; turn not completed, outcome unknown, no automatic replay. '
                'Tool failure cause unknown; permission decisions are not observable in the public Shared Runtime events.')
    return None


class AgyBackend(AcpxBackend):
    def validate_selection(self, model, effort):
        if model is None:
            raise ValueError('AGY requires an explicit discovered model selected by the initiating CLI')
        super().validate_selection(model, None)
        super().validate_selection(effort, None)

    def prepare_start(self, model, effort, approval):
        self.validate_selection(model, effort)
        approval = 'skip-permissions' if approval is None else approval
        if approval not in AGY_APPROVALS:
            raise ValueError('agy_approval must be skip-permissions, approve-all, or approve-reads')
        fields = dict(agy_approval=approval, **{k:v for k,v in dict(model=model, effort=effort).items() if v is not None})
        if approval == 'skip-permissions':
            fields['execution_policy'] = 'native-skip'
        return fields

    def agent_argv(self, task):
        if is_skip_permissions(task):
            return ['agy', 'acp', '--dangerously-skip-permissions']
        return ['agy', 'acp']

    def agent_args(self, task):
        return ['--agent', ' '.join(self.agent_argv(task))]

    def permission_mode(self, task):
        return permission_mode(task)

    def prompt_guidance(self, task):
        return prompt_guidance(task)

    def pre_prompt(self, store, task, emit):
        diagnostic = dict(approval=self.permission_mode(task), permission_decisions='unobservable',
                          reason='Public Shared Runtime events do not expose permission decisions')
        store.update(task['id'], permission_diagnostics=diagnostic)
        emit('permissions', json.dumps(diagnostic))

    def outcome_note(self, decoder, exit_code, state):
        return outcome_note(decoder, exit_code, state)

    def run(self, store, task, emit):
        if task.get('backend') == 'oar':
            try:
                from .backends import backend_for
            except ImportError:
                from backends import backend_for
            return backend_for(task).run(store, task, emit)
        if task.get('agy_swarm') != 'native':
            return super().run(store, task, emit)
        try:
            from .agy_native import run_native
        except ImportError:
            from agy_native import run_native
        return run_native(store, task, emit, self.prompt_guidance(task))

    def control(self, task, action, timeout=20):
        if task.get('backend') == 'oar':
            try:
                from .backends import backend_for
            except ImportError:
                from backends import backend_for
            return backend_for(task).control(task, action, timeout)
        if task.get('agy_swarm') == 'native':
            raise ValueError('Native ACP control belongs to the claimed single owner; no ACPX control permitted')
        return super().control(task, action, timeout)
