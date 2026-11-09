"""Public ACP event normalization; no raw tool bodies or private reasoning."""
import re

def safe_tool_name(value):
    # Titles may contain command arguments or paths; retain identifiers only.
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.:-]{0,79}', value) else ''


def safe_tool_id(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.:-]{0,159}', value) else ''

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
        self.error_category = None
        self.error_source = None
        self.tool_failed = False
        self.final_answer = ''
        self.tool_details = {}
        self.tool_summaries = {}

    def _shared_message(self, frame):
        # The facade emits a terminal frame only after the native turn.result settles.
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
                if frame.get('status') == 'failed' and frame.get('errorCategory') == 'auth_required':
                    self.error_category = 'auth_required'
                    self.error_source = 'session_watch' if frame.get('errorSource') == 'session_watch' else 'turn_result'
            return None
        if frame.get('type') != 'callcli.shared.event':
            self.failed = True
            return None
        event = frame.get('event', {})
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

    def feed(self, event):
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
            out = self.finish()
            self.final_answer = ''
            self.tool_failed |= update.get('status') == 'failed'
            tool_id = safe_tool_id(update.get('toolCallId', ''))
            details = self.tool_details.setdefault(tool_id, {}) if tool_id else {}
            name = safe_tool_name(update.get('title', ''))
            category = update.get('kind')
            if name:
                details['name'] = name
            if category in {'read', 'edit', 'delete', 'move', 'search', 'execute', 'think', 'fetch', 'switch_mode', 'other'}:
                details['kind'] = category
            status = update.get('status') if update.get('status') in {'pending', 'in_progress', 'completed', 'failed'} else ''
            summary = ' '.join(filter(None, [tool_id, details.get('name'), details.get('kind'), status]))
            if not summary or (tool_id and self.tool_summaries.get(tool_id) == summary):
                return out
            if tool_id:
                self.tool_summaries[tool_id] = summary
            return out + [('tool', summary)]
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
