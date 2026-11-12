"""One-shot local OMP extension bridge. stdin is trusted host input, not a model API."""
import hashlib
import json
import secrets
import sqlite3
import sys
import time
from bridge import Origin
from collab import Collaboration, DEFAULT_DB, _polling, _wait_deadline


class HostBridge(Collaboration):
    def __init__(self, db=DEFAULT_DB, **kwargs):
        super().__init__(db, **kwargs)
        with self.store.connect() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS omp_hosts (capability TEXT PRIMARY KEY, binding TEXT NOT NULL)')
        self.binding = None

    @staticmethod
    def identity(host):
        if set(host) != {'session_id', 'agent_id'} or not all(isinstance(v, str) and v for v in host.values()):
            raise ValueError('Host session and agent identity required')
        return json.dumps(host, sort_keys=True)

    def bind(self, host):
        binding = self.identity(host)
        capability = secrets.token_urlsafe(32)
        with self.store.connect() as conn:
            conn.execute('INSERT INTO omp_hosts VALUES (?,?)', (hashlib.sha256(capability.encode()).hexdigest(), binding))
        return capability

    def origin(self, capability):
        with self.store.connect() as conn:
            row = conn.execute('SELECT binding FROM omp_hosts WHERE capability=?', (hashlib.sha256(capability.encode()).hexdigest(),)).fetchone()
        if not row or row[0] != self.binding:
            raise PermissionError('Host binding does not own this capability')
        host = json.loads(row[0])
        return Origin('omp-extension', host['session_id'], row[0], 'omp')

    @staticmethod
    def public(value):
        if isinstance(value, list):
            return [HostBridge.public(item) for item in value]
        if not isinstance(value, dict):
            return value
        from recovery import public_close_receipt
        result = {key:(public_close_receipt(item) if key == 'close_receipt' else HostBridge.public(item))
                  for key,item in value.items()
                  if key not in {'origin','prompt','reply_commands','controller'}}
        if 'id' in value:
            result['host_binding'] = 'omp-extension'
        if 'controller' in value:
            result['controller'] = {'host_binding':'omp-extension'}
        return result

    def handle(self, host, capability, args):
        self.binding = self.identity(host)
        self.origin(capability)
        allowed = {
            'start': {'prompt', 'cwd', 'device', 'agent', 'remote_shell', 'write', 'resource', 'model', 'effort', 'agy_approval', 'skills'},
            'list': set(), 'status': {'task_id'}, 'events': {'task_id', 'after'},
            'wait': {'task_id', 'after', 'timeout'}, 'reply': {'task_id', 'question_id', 'prompt', 'clear_timeout', 'expected_turn', 'command_id'},
            'intervene': {'task_id', 'prompt', 'command_id', 'clear_timeout', 'expected_turn'}, 'cancel': {'task_id'}, 'close': {'task_id'},
        }
        args = dict(args)
        action = args.pop('action', '')
        if action != 'wait' and 'timeout' in args:
            raise ValueError('call_cli_omp does not accept timeout; omit it. Explicit execution budgets remain available through CLI/Python start.')
        if action not in allowed or set(args) - allowed[action]:
            raise ValueError('Unsupported action or model-supplied host field')
        if action == 'intervene' and not args.get('command_id'):
            raise ValueError('Stable command_id required')
        result = getattr(self, action)(capability, **args)
        if action == 'list':
            return {'tasks': [self.public(t) for t in result]}
        if action in {'events'}:
            return {'events': self.public(result)}
        if action == 'wait':
            return self.public(result)
        return {'task': self.public(result)}


def main():
    polling_token = deadline_token = None
    args = {}
    try:
        request = json.load(sys.stdin)
        args = request.get('args', {})
        polling = request.get('action') != 'bind' and args.get('action') == 'wait'
        polling_token = _polling.set(polling)
        deadline_token = _wait_deadline.set(
            time.monotonic() + min(5, max(0, args.get('timeout', 5))) if polling else None)
        bridge = HostBridge(request.get('db') or str(DEFAULT_DB))
        if request.get('action') == 'bind':
            result = {'capability': bridge.bind(request['host'])}
        else:
            result = bridge.handle(request['host'], request['capability'], request['args'])
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        if args.get('action') == 'wait' and isinstance(exc, sqlite3.OperationalError) and 'locked' in str(exc):
            print(json.dumps(dict(task=None, events=[], after=args.get('after', 0), busy=True)))
            return 0
        # Do not expose host input, capabilities, paths or provider exception bodies.
        print(json.dumps({'error': type(exc).__name__, 'message': 'OMP bridge action failed; reconcile exact task before retrying'}))
        return 1
    finally:
        if deadline_token is not None:
            _wait_deadline.reset(deadline_token)
        if polling_token is not None:
            _polling.reset(polling_token)


if __name__ == '__main__':
    raise SystemExit(main())
