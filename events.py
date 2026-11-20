"""Public ACP event normalization; no raw tool bodies or private reasoning."""
import re

QUIET_REVIEW_SECONDS = 600
ROUTINE_DIAGNOSTIC_REASONS = frozenset({
    'record_observed',
    'submission_attempted',
    'acceptance_observed',
    'result_observed',
})
SUPPRESSED_SETUP_KINDS = frozenset({
    'started',
    'version',
    'model',
    'permissions',
    'skill_staged',
    'skill_cleanup',
})
TERMINAL_EVENT_KINDS = frozenset({
    'completed',
    'waiting_reply',
    'cancelled',
    'unknown',
    'closed',
})

def safe_tool_name(value):
    # Titles may contain command arguments or paths; retain identifiers only.
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.:-]{0,79}', value) else ''


def safe_tool_id(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.:-]{0,159}', value) else ''


def safe_tool_kind(value):
    return value if isinstance(value, str) and value in {
        'read', 'edit', 'delete', 'move', 'search', 'execute', 'think', 'fetch', 'switch_mode', 'other'} else ''

class IncompleteQuestion(str):
    """Bounded public prefix whose full body was not retained."""
    identity_complete = False


class EventDecoder:
    """Only public assistant messages and minimal tool status reach the event log."""
    def __init__(self, shared_request_id=None):
        self.text = ''
        self.pending = ''
        self.pending_overflow = False
        self.stop_reason = None
        self.failed = False
        self.prompt_id = None
        self.read_callbacks = set()
        self.shared_request_id = shared_request_id
        self.shared_result_seen = False
        self.prompt_started = False
        self.error_category = None
        self.error_source = None
        self.tool_failed = False
        self.tool_seen = False
        self.final_answer = ''
        self.tool_details = {}
        self.tool_summaries = {}
        self.diagnostic = None

    def _shared_message(self, frame):
        # Facade failures can precede native turn creation; only this terminal is observed.
        if frame.get('requestId') != self.shared_request_id or self.shared_result_seen:
            self.failed = True
            return None
        if frame.get('type') == 'callcli.shared.result':
            self.shared_result_seen = True
            pair = (frame.get('status'), frame.get('stopReason'))
            if pair in {('completed', 'end_turn'), ('cancelled', 'cancelled')}:
                self.stop_reason = frame['stopReason']
            else:
                self.failed = True
                if frame.get('status') == 'failed':
                    stage, reason = frame.get('stage'), frame.get('reasonCode')
                    if isinstance(stage, str) and isinstance(reason, str) and (stage, reason) in {
                                           ('input_validate', 'input_validate_failed'),
                                           ('runtime_load', 'runtime_load_failed'), ('auth_load', 'auth_load_failed'),
                                           ('runtime_create', 'runtime_create_failed'), ('prompt_submit', 'prompt_submit_failed'),
                                           ('prompt_events', 'prompt_events_failed'), ('prompt_result', 'prompt_result_failed'),
                                           ('prompt_result', 'turn_failed'), ('prompt_result', 'invalid_result')}:
                        self.diagnostic = (stage, reason)
                    category = frame.get('errorCategory')
                    source = frame.get('errorSource', 'turn_result' if category == 'auth_required' else None)
                    allowed = {'auth_required': {'turn_result', 'session_watch'},
                               'timeout': {'turn_result_code'},
                               'connection_lost': {'turn_result_detail'}}
                    if isinstance(category, str) and isinstance(source, str) and source in allowed.get(category, ()):
                        self.error_category = category
                        self.error_source = source
            return None
        if frame.get('type') != 'callcli.shared.event':
            self.failed = True
            return None
        event = frame.get('event', {})
        if event.get('type') == 'prompt_started':
            self.prompt_started = True
            return None
        if (event.get('type') == 'text_delta' and event.get('stream') == 'output'
                and event.get('tag') == 'agent_message_chunk' and isinstance(event.get('text'), str)):
            update = {'sessionUpdate': 'agent_message_chunk',
                      'content': {'type': 'text', 'text': event['text']}}
        elif event.get('type') == 'tool_call':
            update = {'sessionUpdate': 'tool_call_update',
                      'toolCallId': event.get('toolCallId', ''), 'status': event.get('status', ''),
                      'title': event.get('title', ''), 'kind': event.get('kind', '')}
        else:
            return None
        return {'params': {'update': update}}

    def feed(self, event, *, text_boundary=True):
        if self.shared_request_id is not None:
            message = self._shared_message(event)
            if message is None:
                return []
        else:
            message = event.get('message', event)
        self._legacy_rpc(message)
        update = message.get('params', {}).get('update', {})
        kind = update.get('sessionUpdate')
        if kind in {'tool_call','tool_call_update'}:
            # Text blocks often omit a trailing newline. A tool boundary ends
            # the preceding public block; publish it before the tool runs.
            out = self.finish() if text_boundary else []
            self.tool_seen = True
            self.final_answer = ''
            self.tool_failed |= update.get('status') == 'failed'
            tool_id = safe_tool_id(update.get('toolCallId', ''))
            details = self.tool_details.setdefault(tool_id, {}) if tool_id else {}
            name = safe_tool_name(update.get('title', ''))
            category = safe_tool_kind(update.get('kind'))
            if name:
                details['name'] = name
            if category:
                details['kind'] = category
            status = update.get('status') if update.get('status') in {'pending', 'in_progress', 'completed', 'failed'} else ''
            summary = ' '.join(filter(None, [tool_id, details.get('name'), details.get('kind'), status]))
            if not summary or (tool_id and self.tool_summaries.get(tool_id) == summary):
                return out
            if tool_id:
                self.tool_summaries[tool_id] = summary
            return out + [('tool', summary)]
        if kind == 'user_message_chunk':
            return self.finish() if text_boundary else []
        if kind != 'agent_message_chunk':
            return []
        content = update.get('content', {})
        chunk = content.get('text','') if isinstance(content,dict) else ''
        self.text = (self.text + chunk)[-64000:]
        out = []
        parts = chunk.split('\n')
        for index, value in enumerate(parts):
            ended = index < len(parts)-1
            remaining = 64000-len(self.pending)
            self.pending += value[:remaining]
            self.pending_overflow |= len(value) > remaining
            if ended:
                out.extend(self.finish())
        return out

    def _legacy_rpc(self, message):
        rpc_id, method = message.get('id'), message.get('method')
        if method == 'session/prompt':
            self.prompt_id = rpc_id
        if method and rpc_id is not None:
            self.read_callbacks.discard(rpc_id)
            if method == 'fs/read_text_file':
                self.read_callbacks.add(rpc_id)
        if message.get('error'):
            # A failed read callback may be recovered by the agent. Only ignore
            # a correlated read response distinct from the active prompt ID;
            # unclassified errors and bidirectional ID collisions fail closed.
            recovered_read = (self.prompt_id is not None and rpc_id != self.prompt_id
                              and rpc_id in self.read_callbacks)
            if not recovered_read:
                self.failed = True
        if 'error' in message or 'result' in message:
            self.read_callbacks.discard(rpc_id)
        result = message.get('result', {})
        if isinstance(result,dict) and 'stopReason' in result:
            self.stop_reason = result['stopReason']

    def line(self, line):
        line = line.strip()
        for prefix,kind in [('CALLCLI_STAGE ','stage'),('CALLCLI_QUESTION ','question'),('CALLCLI_SKILL ','skill')]:
            if line.startswith(prefix):
                text = line[len(prefix):]
                # Keep question identity intact until Store hashes it before public truncation.
                if kind == 'question' and self.pending_overflow:
                    return [(kind, IncompleteQuestion(text[:8000]))]
                return [(kind, text if kind == 'question' else text[:8000])]
        if line:
            self.final_answer = (self.final_answer + line + '\n')[-64000:]
            return [('answer',line[:8000])]
        return []

    def finish(self):
        out = self.line(self.pending)
        self.pending = ''
        self.pending_overflow = False
        return out


def is_routine_diagnostic(extra=None):
    """Return True only for routine lifecycle diagnostics without attention_required."""
    meta = extra if isinstance(extra, dict) else {}
    if meta.get('attention_required') is True:
        return False
    return meta.get('reason_code') in ROUTINE_DIAGNOSTIC_REASONS


def classify_event_delivery(kind, text='', *, extra=None, last_stage=None, tool_notices=0):
    """Decide whether a persisted event should emit live stdout or stay SQLite-only."""
    meta = extra if isinstance(extra, dict) else {}
    next_last_stage = last_stage
    next_tool_notices = tool_notices

    if kind in TERMINAL_EVENT_KINDS:
        return dict(deliver=True, tier='terminal', reason='terminal_settlement',
                    next_last_stage=next_last_stage, next_tool_notices=next_tool_notices)
    if meta.get('attention_required') is True:
        return dict(deliver=True, tier='attention', reason='attention_required',
                    next_last_stage=next_last_stage, next_tool_notices=next_tool_notices)
    if kind in {'question', 'permission'}:
        return dict(deliver=True, tier='attention', reason=kind,
                    next_last_stage=next_last_stage, next_tool_notices=next_tool_notices)
    if kind in SUPPRESSED_SETUP_KINDS:
        return dict(deliver=False, tier='store_only', reason='routine_setup',
                    next_last_stage=next_last_stage, next_tool_notices=next_tool_notices)
    if kind == 'diagnostic':
        if is_routine_diagnostic(meta):
            return dict(deliver=False, tier='store_only', reason='routine_diagnostic',
                        next_last_stage=next_last_stage, next_tool_notices=next_tool_notices)
        return dict(deliver=True, tier='attention', reason='non_routine_diagnostic',
                    next_last_stage=next_last_stage, next_tool_notices=next_tool_notices)
    if kind == 'skill':
        return dict(deliver=False, tier='store_only', reason='routine_skill',
                    next_last_stage=next_last_stage, next_tool_notices=next_tool_notices)
    if kind == 'answer':
        return dict(deliver=False, tier='store_only', reason='intermediate_answer',
                    next_last_stage=next_last_stage, next_tool_notices=next_tool_notices)
    if kind == 'stage':
        if meta.get('quiet_notice') is True:
            return dict(deliver=True, tier='attention', reason='review_notice',
                        next_last_stage=next_last_stage, next_tool_notices=next_tool_notices)
        if last_stage is not None and text == last_stage:
            return dict(deliver=False, tier='store_only', reason='repeated_stage',
                        next_last_stage=next_last_stage, next_tool_notices=next_tool_notices)
        return dict(deliver=True, tier='substantive', reason='stage_change',
                    next_last_stage=text, next_tool_notices=next_tool_notices)
    if kind == 'tool':
        next_tool_notices = tool_notices + 1
        if isinstance(text, str) and text.endswith(' failed'):
            return dict(deliver=True, tier='attention', reason='tool_failed',
                        next_last_stage=next_last_stage, next_tool_notices=next_tool_notices)
        if next_tool_notices > 12:
            return dict(deliver=False, tier='store_only', reason='tool_cap',
                        next_last_stage=next_last_stage, next_tool_notices=next_tool_notices)
        return dict(deliver=True, tier='tool', reason='tool_status',
                    next_last_stage=next_last_stage, next_tool_notices=next_tool_notices)
    return dict(deliver=True, tier='substantive', reason='other',
                next_last_stage=next_last_stage, next_tool_notices=next_tool_notices)


class ReviewTracker:
    """Track 600s review cooldown while separating substantive progress from tool churn."""

    def __init__(self, started_at, *, interval=QUIET_REVIEW_SECONDS):
        self.started_at = started_at
        self.interval = interval
        self.last_notice = started_at
        self.last_visible = started_at
        self.last_substantive_at = started_at
        self.last_stage = None
        self.last_summary = 'No stage or tool status observed.'
        self.substantive_since_notice = False
        self.tool_events_since_substantive = 0
        self.tool_events_since_notice = 0

    def record_visible(self, now, *, summary=None):
        """Record visible non-substantive activity with an explicit monotonic timestamp."""
        self.last_visible = now
        if isinstance(summary, str) and summary.strip():
            self.last_summary = summary[:500]

    def record_event(self, kind, text, now, *, extra=None):
        """Record an emitted event, distinguishing substantive changes from tool churn."""
        meta = extra if isinstance(extra, dict) else {}
        if kind in SUPPRESSED_SETUP_KINDS:
            return
        if kind == 'diagnostic' and is_routine_diagnostic(meta):
            return
        if kind == 'stage' and meta.get('quiet_notice') is True:
            return
        self.last_visible = now
        if kind == 'stage':
            if isinstance(text, str) and text:
                self.last_summary = text[:500]
            if text != self.last_stage:
                self.last_stage = text
                self.last_substantive_at = now
                self.substantive_since_notice = True
                self.tool_events_since_substantive = 0
        elif kind in {'question', 'permission'}:
            if isinstance(text, str) and text:
                self.last_summary = text[:500]
            self.last_substantive_at = now
            self.substantive_since_notice = True
            self.tool_events_since_substantive = 0
        elif kind == 'answer':
            if isinstance(text, str) and text.strip():
                self.last_substantive_at = now
                self.substantive_since_notice = True
                self.tool_events_since_substantive = 0
        elif kind == 'tool':
            if isinstance(text, str) and text:
                self.last_summary = text[:500]
            self.tool_events_since_substantive += 1
            self.tool_events_since_notice += 1

    def is_due(self, now):
        return (now - self.last_notice) >= self.interval

    def inspect(self, now):
        elapsed = int(now - self.started_at)
        quiet_seconds = int(now - self.last_visible)
        since_substantive_seconds = int(now - self.last_substantive_at)
        is_quiet = (now - self.last_visible) >= self.interval
        tool_churn_only = (
            (not is_quiet)
            and ((now - self.last_substantive_at) >= self.interval)
            and (self.tool_events_since_substantive > 0)
        )
        substantive_progress = (
            (not is_quiet)
            and ((now - self.last_substantive_at) < self.interval)
            and self.substantive_since_notice
        )
        notice_kind = 'quiet' if is_quiet else ('tool_churn_only' if tool_churn_only else 'checkpoint')
        return dict(
            elapsed=elapsed,
            quiet_seconds=quiet_seconds,
            since_substantive_seconds=since_substantive_seconds,
            is_quiet=is_quiet,
            tool_churn_only=tool_churn_only,
            substantive_progress=substantive_progress,
            notice_kind=notice_kind,
            last_stage=self.last_stage,
        )

    def build_notice(self, now):
        """Construct the 600s review stage payload and advance the cooldown."""
        state = self.inspect(now)
        self.last_notice = now
        self.substantive_since_notice = False
        self.tool_events_since_notice = 0
        notice_kind = state['notice_kind']
        elapsed = state['elapsed']
        detail = ''
        if notice_kind == 'tool_churn_only':
            detail = (
                f"Tool activity observed without substantive stage or answer change for "
                f"{state['since_substantive_seconds']}s. "
            )
        elif not state['is_quiet'] and not state['substantive_progress'] and state['since_substantive_seconds'] >= self.interval:
            detail = (
                f"No substantive stage or answer change observed for "
                f"{state['since_substantive_seconds']}s. "
            )
        text = (
            f"Coordinator review requested ({notice_kind}); elapsed {elapsed}s. "
            + detail
            + 'Inspect this task and its events, then choose to keep waiting, explicitly intervene, or cancel. '
            + 'Activity or silence alone does not establish success or a hang. '
            + 'No cancellation or closure requested. Last visible status: '
            + self.last_summary
        )
        return dict(
            text=text,
            metadata=dict(
                notice_kind=notice_kind,
                elapsed=elapsed,
                quiet_seconds=state['quiet_seconds'],
                since_substantive_seconds=state['since_substantive_seconds'],
                last_stage=state['last_stage'],
                tool_churn_only=state['tool_churn_only'],
                substantive_progress=state['substantive_progress'],
            ),
        )
