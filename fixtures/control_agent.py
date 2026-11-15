"""Isolated ACP control endpoint; no model, config, or external service access."""
import json
import sys
from pathlib import Path

trace = Path(sys.argv[1])
fail_load = sys.argv[2] == 'fail-load'
models = {'availableModels': [{'modelId': 'one', 'name': 'One'}, {'modelId': 'two', 'name': 'Two'}], 'currentModelId': 'one'}
modes = {'availableModes': [{'id': 'safe', 'name': 'Safe'}, {'id': 'agent-full-access', 'name': 'Full'}], 'currentModeId': 'safe'}
for line in sys.stdin:
    message = json.loads(line)
    method = message.get('method')
    with trace.open('a') as out:
        out.write(json.dumps({'method': method}) + '\n')
    result = {}
    error = None
    if method == 'initialize':
        result = {'protocolVersion': 1, 'agentCapabilities': {'loadSession': True}, 'authMethods': []}
    elif method == 'session/new':
        result = {'sessionId': 'control-fixture', 'models': models, 'modes': modes}
    elif method == 'session/load':
        if fail_load:
            error = {'code': -32603, 'message': 'Synthetic load refusal'}
        else:
            result = {'models': models, 'modes': modes}
    elif method == 'session/prompt':
        result = {'stopReason': 'end_turn'}
    if 'id' in message:
        response = {'jsonrpc': '2.0', 'id': message['id']}
        response['error' if error else 'result'] = error or result
        print(json.dumps(response), flush=True)
