"""AGY-only approval, prompt guidance and business-outcome policy.

The coordinator, transport and task lifecycle remain shared with other agents.
"""
import json
try:
    from .acpx_backend import AcpxBackend
except ImportError:
    from acpx_backend import AcpxBackend


AGY_FATAL_LINE = '\u26a0 Agent execution terminated due to error.'


def permission_mode(task):
    # Missing persisted fields are legacy tasks, not permission to widen access.
    value = task.get('agy_approval')
    if value is not None:
        if value not in {'approve-all', 'approve-reads'}:
            raise ValueError('agy_approval must be approve-all or approve-reads')
        return value
    return 'approve-all' if task['write'] else 'approve-reads'


def prompt_guidance(task):
    mode = permission_mode(task)
    return ('Prefer native read/search tools for reading and searching. '
            'Tool approval policy: '+mode+'. '
            + ('Shell execution is approved within the stated task scope. '
               if mode == 'approve-all' else
               'Read/search requests are approved; shell/write requests are denied non-interactively. ')
            + 'This approval policy is not a filesystem sandbox and does not expand task authorization.\n\n')


def fatal_error(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return bool(lines) and lines[-1] == AGY_FATAL_LINE


def outcome_note(decoder, exit_code, state):
    reason = None
    if fatal_error(decoder.text):
        reason = 'AGY reported a fatal execution error (final line "'+AGY_FATAL_LINE+'")'
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
        approval = 'approve-all' if approval is None else approval
        if approval not in {'approve-all', 'approve-reads'}:
            raise ValueError('agy_approval must be approve-all or approve-reads')
        return dict(agy_approval=approval, **{k:v for k,v in dict(model=model, effort=effort).items() if v is not None})

    def agent_args(self, task):
        return ['--agent', 'agy acp']

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
