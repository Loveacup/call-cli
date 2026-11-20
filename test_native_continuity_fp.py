"""Targeted RED->GREEN verification for Slice C native continuity & command timing."""
import contextlib
import io
import json
import os
from pathlib import Path
import selectors
import sys
import tempfile
import textwrap
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import agy_native
from agy_native import (
    NativeOwner,
    ProtocolError,
    _classify_swarm_slice,
    _is_handoff_tool,
    _is_swarm_launch_ack_text,
)
from collab import Collaboration
from runner import run_task


_SLICE_C_FAKE_AGY = textwrap.dedent("""\
    #!__PYTHON__
    import json
    import os
    from pathlib import Path
    import sys
    import time

    root = Path(os.environ['SLICE_C_DIR'])
    case = os.environ['SLICE_C_CASE']
    state_file = root / 'acp_state.json'

    def load_state():
        if state_file.exists():
            return json.loads(state_file.read_text())
        return {'next_seq': 1, 'sessions': {}}

    def save_state(st):
        state_file.write_text(json.dumps(st))

    def send(payload):
        print(json.dumps({'jsonrpc': '2.0', **payload}), flush=True)

    def metadata(sid, active_model='gemini-pro-orchestrator'):
        return {
            'sessionId': sid,
            'models': {
                'currentModelId': active_model,
                'availableModels': [
                    {'modelId': 'gemini-pro-orchestrator'},
                    {'modelId': 'gemini-4-carbon-medium'},
                ],
            },
            'configOptions': [
                {
                    'id': 'model',
                    'currentValue': active_model,
                    'options': [
                        {'value': 'gemini-pro-orchestrator', 'name': 'Orchestrator'},
                        {'value': 'gemini-4-carbon', 'name': 'Carbon'},
                    ],
                },
            ],
        }

    def emit_update(sid, update):
        send({'method': 'session/update', 'params': {'sessionId': sid, 'update': update}})

    for raw in sys.stdin:
        msg = json.loads(raw)
        method = msg.get('method')
        with (root / 'requests.jsonl').open('a') as f:
            f.write(json.dumps(msg) + '\\n')

        if method == 'initialize':
            send({
                'id': msg['id'],
                'result': {
                    'protocolVersion': 1,
                    'agentCapabilities': {'loadSession': True, 'sessionCapabilities': {'close': {}, 'resume': {}}},
                },
            })
        elif method == 'session/new':
            st = load_state()
            idx = st['next_seq']
            st['next_seq'] += 1
            sid = f'00000000-0000-4000-8000-{idx:012d}'
            st['sessions'][sid] = {
                'closed': False,
                'history': [],
                'prompted': False,
                'model': 'gemini-pro-orchestrator',
                'phase': 0,
            }
            save_state(st)
            send({'id': msg['id'], 'result': metadata(sid)})
            emit_update(sid, {
                'sessionUpdate': 'available_commands_update',
                'availableCommands': [{'name': 'swarm'}],
            })
        elif method == 'session/set_config_option':
            sid = msg['params']['sessionId']
            st = load_state()
            sess = st['sessions'].get(sid)
            if not sess or sess.get('closed'):
                send({'id': msg['id'], 'error': {'code': -32602, 'message': 'Session not found'}})
            else:
                if msg['params'].get('configId') == 'model':
                    sess['model'] = msg['params']['value']
                    save_state(st)
                send({'id': msg['id'], 'result': metadata(sid, sess.get('model', 'gemini-pro-orchestrator'))})
        elif method == 'session/load':
            sid = msg['params']['sessionId']
            st = load_state()
            sess = st['sessions'].get(sid)
            if not sess or sess.get('closed'):
                send({'id': msg['id'], 'error': {'code': -32602, 'message': 'Session not found'}})
                continue
            if sess.get('prompted') and sess.get('phase', 0) == 0 and (root / 'release_wakeup').exists():
                (root / 'result.txt').write_text('ALPHA|BETA')
                sess['history'].extend([
                    {
                        'sessionUpdate': 'tool_call',
                        'toolCallId': 'wakeup-read-board',
                        'name': 'read_board',
                        'kind': 'other',
                        'status': 'completed',
                    },
                    {
                        'sessionUpdate': 'agent_message_chunk',
                        'content': {'type': 'text', 'text': 'Swarm complete: ALPHA|BETA.\\n'},
                    },
                ])
                sess['phase'] = 1
                save_state(st)
            for item in sess['history']:
                emit_update(sid, item)
            send({'id': msg['id'], 'result': metadata(sid, sess.get('model', 'gemini-pro-orchestrator'))})
        elif method == 'session/prompt':
            sid = msg['params']['sessionId']
            body = msg['params']['prompt'][0]['text']
            st = load_state()
            sess = st['sessions'][sid]
            sess['prompted'] = True
            (root / 'prompt').write_text(body)
            events = [{'sessionUpdate': 'user_message_chunk', 'content': {'type': 'text', 'text': body}}]
            if case == 'wait_then_wakeup':
                events.extend([
                    {
                        'sessionUpdate': 'tool_call',
                        'toolCallId': 'call_swarm_wait_1',
                        'name': 'swarm_wait',
                        'kind': 'other',
                        'status': 'completed',
                    },
                    {
                        'sessionUpdate': 'agent_message_chunk',
                        'content': {'type': 'text', 'text': '等待 worker-b 完成后汇总。\\n'},
                    },
                ])
            elif case == 'wait_same_turn_final':
                (root / 'result.txt').write_text('ALPHA|BETA')
                events.extend([
                    {
                        'sessionUpdate': 'tool_call',
                        'toolCallId': 'call_add_left',
                        'name': 'swarm',
                        'kind': 'other',
                        'status': 'completed',
                        'rawInput': {'action': 'add', 'name': 'left'},
                    },
                    {
                        'sessionUpdate': 'tool_call',
                        'toolCallId': 'call_swarm_wait_1',
                        'name': 'swarm_wait',
                        'kind': 'other',
                        'status': 'completed',
                    },
                    {
                        'sessionUpdate': 'agent_message_chunk',
                        'content': {
                            'type': 'text',
                            'text': 'Added parser guard and switched select order. Results: ALPHA|BETA.\\n',
                        },
                    },
                ])
            elif case == 'long_launch_ack':
                detail_items = '\\n'.join(
                    f'- worker-{i}: 独立排查模块 {i} 的状态机边界、读写锁行为与异常路径并提交最小补丁说明 ({i:02d})'
                    for i in range(1, 28)
                )
                long_ack = '已派发 3 个 worker 并行执行，当前分工清单如下：\\n' + detail_items + '\\n'
                assert len(long_ack) > 1300
                events.extend([
                    {
                        'sessionUpdate': 'tool_call',
                        'toolCallId': 'call_add_left',
                        'name': 'swarm',
                        'kind': 'other',
                        'status': 'completed',
                        'rawInput': {'action': 'add', 'name': 'left'},
                    },
                    {
                        'sessionUpdate': 'tool_call',
                        'toolCallId': 'call_add_right',
                        'name': 'swarm',
                        'kind': 'other',
                        'status': 'completed',
                        'rawInput': {'action': 'add', 'name': 'right'},
                    },
                    {
                        'sessionUpdate': 'agent_message_chunk',
                        'content': {'type': 'text', 'text': long_ack},
                    },
                ])
            elif case == 'long_final_after_wait':
                (root / 'result.txt').write_text('ALPHA|BETA')
                body_rows = '\\n'.join(
                    f'- 检查项 {i}: 已添加空值保护并更新了边界解析与回归断言 ({i:02d})，输出校验完全一致。'
                    for i in range(1, 32)
                )
                long_final = (
                    '## 实际结果\\n'
                    '- 已创建校验模块并补全回归断言\\n'
                    + body_rows
                    + '\\n## 验证\\n12 项测试通过\\n## 剩余问题\\n无\\n'
                )
                assert len(long_final) > 1400
                events.extend([
                    {
                        'sessionUpdate': 'tool_call',
                        'toolCallId': 'call_add_left',
                        'name': 'swarm',
                        'kind': 'other',
                        'status': 'completed',
                        'rawInput': {'action': 'add', 'name': 'left'},
                    },
                    {
                        'sessionUpdate': 'tool_call',
                        'toolCallId': 'call_swarm_wait_1',
                        'name': 'swarm_wait',
                        'kind': 'other',
                        'status': 'completed',
                    },
                    {
                        'sessionUpdate': 'agent_message_chunk',
                        'content': {'type': 'text', 'text': long_final},
                    },
                ])
            for ev in events:
                sess['history'].append(ev)
                if ev.get('sessionUpdate') != 'user_message_chunk':
                    emit_update(sid, ev)
            save_state(st)
            send({'id': msg['id'], 'result': {'stopReason': 'end_turn'}})
        elif method == 'session/cancel':
            (root / 'cancelled').touch()
        elif method == 'session/close':
            sid = msg['params']['sessionId']
            st = load_state()
            if sid in st['sessions']:
                st['sessions'][sid]['closed'] = True
                save_state(st)
            send({'id': msg['id'], 'result': {}})
""")


class NativeContinuityFPTests(unittest.TestCase):
    @contextlib.contextmanager
    def slice_c_workspace(self, case):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agy_bin = root / 'agy'
            agy_bin.write_text(_SLICE_C_FAKE_AGY.replace('__PYTHON__', sys.executable))
            agy_bin.chmod(0o700)
            with patch.dict(
                os.environ,
                PATH=str(root) + os.pathsep + os.environ['PATH'],
                SLICE_C_DIR=directory,
                SLICE_C_CASE=case,
            ), patch('updates.DEFAULT_LOCK_ROOT', root / 'locks'), \
               patch('updates.ensure_current', return_value={'decision': 'fixture'}), \
               contextlib.redirect_stdout(io.StringIO()):
                c = Collaboration(root / 'tasks.db', launch=lambda task: None)
                client = c.connect('hermes')['client_id']
                task = c.start(
                    client,
                    agent='agy',
                    model='ocs',
                    cwd=directory,
                    prompt='Coordinate workers and deliver final verified output',
                    write=True,
                )
                thread = threading.Thread(
                    target=run_task,
                    args=(c.store.path, task['id']),
                    kwargs=dict(turn=1, launch_id=task['launch_id'], epoch=1),
                )
                thread.start()
                try:
                    yield root, c, client, task, thread
                finally:
                    if thread.is_alive():
                        c.cancel(client, task['id'])
                        thread.join(5)
                    self.assertFalse(thread.is_alive())

    @contextlib.contextmanager
    def fixture_workspace(self, *, env_extra=None, model='ocs', agy_mode=None, agy_approval=None,
                          native_session_id=None, native_capabilities=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agy_bin = root / 'agy'
            agy_bin.write_text('#!' + sys.executable + '\n' + (Path(__file__).parent / 'fixtures/agy_native_agent.py').read_text())
            agy_bin.chmod(0o700)
            env = {'PATH': str(root) + os.pathsep + os.environ['PATH'], 'AGY_NATIVE_FIXTURE': directory}
            if env_extra:
                env.update(env_extra)
            with patch.dict(os.environ, env), \
                 patch('updates.DEFAULT_LOCK_ROOT', root / 'locks'), \
                 patch('updates.ensure_current', return_value={'decision': 'fixture'}), \
                 contextlib.redirect_stdout(io.StringIO()):
                c = Collaboration(root / 'tasks.db', launch=lambda task: None)
                client = c.connect('hermes')['client_id']
                start_kw = dict(agent='agy', model=model, cwd=directory, prompt='Run command timing check', write=True)
                if agy_mode is not None:
                    start_kw['agy_mode'] = agy_mode
                if agy_approval is not None:
                    start_kw['agy_approval'] = agy_approval
                task = c.start(client, **start_kw)
                if native_session_id is not None or native_capabilities is not None:
                    patch_fields = {}
                    if native_session_id is not None:
                        patch_fields['native_session_id'] = native_session_id
                    if native_capabilities is not None:
                        patch_fields['native_capabilities'] = native_capabilities
                    c.store.update(task['id'], **patch_fields)
                thread = threading.Thread(
                    target=run_task,
                    args=(c.store.path, task['id']),
                    kwargs=dict(turn=1, launch_id=task['launch_id'], epoch=1),
                )
                thread.start()
                try:
                    yield root, c, client, task, thread
                finally:
                    if thread.is_alive():
                        c.cancel(client, task['id'])
                        thread.join(5)
                    self.assertFalse(thread.is_alive())

    def _until(self, predicate, msg='Expected condition did not arrive', timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail(msg)

    def _requests(self, root):
        req_path = root / 'requests.jsonl'
        if not req_path.exists():
            return []
        return [json.loads(line) for line in req_path.read_text().splitlines() if line.strip()]

    def _make_pipe_owner(self):
        r_out, w_out = os.pipe()
        r_in, w_in = os.pipe()
        owner = object.__new__(NativeOwner)
        owner.task = dict(cwd='/tmp', agy_approval='approve-all')
        owner.session_id = 'owned'
        owner.serial = 0
        owner.buffer = b''
        owner.permission_decisions = []
        owner.review = None
        owner.review_blocked = False
        owner.available_commands = set()
        owner.commands_advertised_count = 0
        owner.permission_handler = None
        owner.workflow_identity = {}
        owner.approval_wait_seconds = 0.0
        owner.pending_drained = []
        owner.drain_workflow_calls = set()
        owner.drained_public_text = ''
        owner.drained_line_pending = ''
        owner.proc = SimpleNamespace(
            stdout=os.fdopen(r_out, 'rb', buffering=0),
            stdin=os.fdopen(w_in, 'wb', buffering=0),
        )
        owner.poller = selectors.DefaultSelector()
        owner.poller.register(owner.proc.stdout, selectors.EVENT_READ)
        reader = os.fdopen(r_in, 'rb', buffering=0)
        writer = os.fdopen(w_out, 'wb', buffering=0)

        def cleanup():
            owner.poller.close()
            for handle in (owner.proc.stdout, owner.proc.stdin, reader, writer):
                try:
                    handle.close()
                except OSError:
                    pass

        return owner, reader, writer, cleanup

    # --- Hypothesis 1: swarm_wait & >1200-char launch ack vs genuine final delivery ---

    def test_swarm_wait_unit_distinguishes_waiting_from_final_delivery(self):
        wait_tool = dict(sessionUpdate='tool_call', toolCallId='w1', title='swarm_wait', kind='other', status='completed')
        add_tool = dict(sessionUpdate='tool_call', toolCallId='a1', title='swarm', kind='other', status='completed', swarm=dict(action='add', name='left'))
        done_status = dict(sessionUpdate='tool_call', toolCallId='s1', title='swarm', kind='other', status='completed', swarm=dict(action='status'), native_work_pending=False)
        read_tool = dict(sessionUpdate='tool_call', toolCallId='r1', title='read_board', kind='other', status='completed')

        self.assertTrue(_is_handoff_tool(wait_tool, 'swarm'))
        self.assertFalse(_is_handoff_tool(dict(wait_tool, status='failed'), 'swarm'))
        self.assertFalse(_is_handoff_tool(wait_tool, 'teamwork'))

        waiting_cases = [
            ('wait_en', [wait_tool], 'Still waiting on worker-b to finish.\n', False),
            ('wait_zh', [wait_tool], '等待 worker-b 完成后回传结果。\n', False),
            ('wait_board_hint', [wait_tool], 'Run /swarm board (default shortcut ctrl+b) to open the live board.', False),
            ('wait_empty', [wait_tool], '', True),
            ('wait_after_status', [done_status, wait_tool], 'Waiting for follow-up board message.', False),
        ]
        for label, entries, text, allow_empty in waiting_cases:
            with self.subTest(kind='waiting', label=label):
                res = _classify_swarm_slice(entries, text, allow_empty_handoff_hold=allow_empty)
                self.assertTrue(res['interim'], (label, res))
                self.assertTrue(res['has_unresolved_handoff'], (label, res))

        final_controls = [
            ('direct_results', [wait_tool], 'Results: left=ALPHA, right=BETA.'),
            ('added_switched_final', [wait_tool], 'Added parser guard in agy_native.py and switched select order. Left=ALPHA, right=BETA.'),
            ('add_wait_added_switched_final', [add_tool, wait_tool], 'Added foo.py and switched bar.py; left=ALPHA, right=BETA.'),
            ('add_wait_read_structured_final', [add_tool, wait_tool, read_tool],
             '## Actual Results\n- Added foo.py\n## Verification\n10 tests passed\n## Remaining Issues\nNone.'),
            ('failed_wait_never_holds', [dict(wait_tool, status='failed')], 'Waiting failed.'),
            ('invalid_output_never_holds', [wait_tool], '\u26a0 Model produced invalid output.'),
        ]
        for label, entries, text in final_controls:
            with self.subTest(kind='final_control', label=label):
                res = _classify_swarm_slice(entries, text)
                self.assertFalse(res['interim'], (label, res))

    def test_long_launch_ack_over_1200_vs_long_final_deliverable_unit(self):
        add_tool = dict(sessionUpdate='tool_call', toolCallId='a1', title='swarm', kind='other', status='completed', swarm=dict(action='add', name='worker-a'))
        wait_tool = dict(sessionUpdate='tool_call', toolCallId='w1', title='swarm_wait', kind='other', status='completed')

        zh_details = '\n'.join(
            f'- worker-{i}: 独立排查模块 {i} 的状态机边界、读写锁行为与异常路径并提交最小补丁说明 ({i:02d})'
            for i in range(1, 28)
        )
        zh_long_ack = '已派发 3 个 worker 并行执行：\n' + zh_details
        self.assertGreater(len(zh_long_ack), 1300)

        en_details = '\n'.join(
            f'- worker-{i}: inspect module_{i}.py edge cases, ownership boundaries, and write minimal regression tests ({i:02d})'
            for i in range(1, 22)
        )
        en_long_ack = 'Dispatched workers left and right:\n' + en_details
        self.assertGreater(len(en_long_ack), 1300)

        for label, ack in (('zh_long_ack', zh_long_ack), ('en_long_ack', en_long_ack)):
            with self.subTest(kind='long_ack', label=label):
                self.assertTrue(_is_swarm_launch_ack_text(ack))
                res = _classify_swarm_slice([add_tool], ack)
                self.assertTrue(res['interim'], res)

        en_prose_items = '\n'.join(
            f'- Finding {i}: module_{i}.py validated input boundaries and updated normalization consistently across call sites.'
            for i in range(1, 20)
        )
        en_long_final_no_kw = (
            'Here is the complete technical analysis across all modules:\n'
            '- We added a null check and switched the cache key in lookup_entry() so stale rows never collide.\n'
            + en_prose_items
        )
        self.assertGreater(len(en_long_final_no_kw), 1400)

        zh_prose_items = '\n'.join(
            f'- 检查项 {i}: 已添加空值保护并更新了边界解析与回归断言 ({i:02d})，输出校验完全一致。'
            for i in range(1, 32)
        )
        zh_long_final = (
            '## 实际结果\n- 已创建 test_x.py\n- 已添加校验逻辑\n'
            + zh_prose_items
            + '\n## 验证\n12 项测试通过\n## 剩余问题\n无'
        )
        self.assertGreater(len(zh_long_final), 1400)

        en_structured_after_recap = (
            'Dispatched workers left and right; summary below:\n'
            + en_details
            + '\n## Actual Results\n- Completed all requested changes\n'
            + '## Verification\nRan 12 tests in 0.4s. 12 tests passed.\n'
            + '## Remaining Issues\nNone.'
        )
        self.assertGreater(len(en_structured_after_recap), 1400)

        for label, entries, body in (
            ('en_long_final_no_kw', [add_tool, wait_tool], en_long_final_no_kw),
            ('zh_long_final', [add_tool, wait_tool], zh_long_final),
            ('en_structured_after_recap', [add_tool, wait_tool], en_structured_after_recap),
        ):
            with self.subTest(kind='long_final_control', label=label):
                res = _classify_swarm_slice(entries, body)
                self.assertFalse(res['interim'], (label, res))

        for label, short_ack in (('zh_short', '已启动 left 处理任务。'), ('en_short', 'Added worker-a.')):
            with self.subTest(kind='short_ack_control', label=label):
                res = _classify_swarm_slice([add_tool], short_ack)
                self.assertTrue(res['interim'], (label, res))

    def test_e2e_swarm_wait_waiting_holds_then_completes_while_same_turn_final_settles(self):
        with self.slice_c_workspace('wait_then_wakeup') as (root, c, client, task, thread):
            self._until(
                lambda: c.status(client, task['id']).get('native_observation', {}).get('response_pending_reason') == 'native_handoff_only',
                'wait_then_wakeup did not hold in native_handoff_only',
            )
            time.sleep(0.15)
            mid = c.status(client, task['id'])
            self.assertEqual(mid['state'], 'running')
            self.assertTrue(thread.is_alive())
            self.assertFalse(any(r.get('method') == 'session/close' for r in self._requests(root)))

            (root / 'release_wakeup').touch()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            final = c.status(client, task['id'])
            self.assertEqual(final['state'], 'completed', final.get('settlement_note'))
            self.assertEqual((root / 'result.txt').read_text(), 'ALPHA|BETA')
            self.assertIn('Swarm complete: ALPHA|BETA.', final['native_result']['result'])
            self.assertEqual(sum(r.get('method') == 'session/close' for r in self._requests(root)), 1)
            self.assertEqual(c.close(client, task['id'])['state'], 'closed')

        with self.slice_c_workspace('wait_same_turn_final') as (root, c, client, task, thread):
            thread.join(5)
            self.assertFalse(thread.is_alive())
            final = c.status(client, task['id'])
            self.assertEqual(final['state'], 'completed', final.get('settlement_note'))
            self.assertEqual((root / 'result.txt').read_text(), 'ALPHA|BETA')
            self.assertIn('Results: ALPHA|BETA.', final['native_result']['result'])
            self.assertEqual(sum(r.get('method') == 'session/close' for r in self._requests(root)), 1)
            self.assertEqual(c.close(client, task['id'])['state'], 'closed')

    def test_e2e_long_over_1200_launch_ack_holds_while_long_final_settles(self):
        with self.slice_c_workspace('long_launch_ack') as (root, c, client, task, thread):
            self._until(
                lambda: c.status(client, task['id']).get('native_observation', {}).get('response_pending_reason') == 'native_handoff_only',
                'long_launch_ack did not hold in native_handoff_only',
            )
            time.sleep(0.15)
            mid = c.status(client, task['id'])
            self.assertEqual(mid['state'], 'running')
            self.assertFalse((root / 'result.txt').exists())
            self.assertFalse(any(r.get('method') == 'session/close' for r in self._requests(root)))

            (root / 'release_wakeup').touch()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            final = c.status(client, task['id'])
            self.assertEqual(final['state'], 'completed', final.get('settlement_note'))
            self.assertEqual((root / 'result.txt').read_text(), 'ALPHA|BETA')
            self.assertIn('Swarm complete: ALPHA|BETA.', final['native_result']['result'])
            self.assertEqual(sum(r.get('method') == 'session/close' for r in self._requests(root)), 1)
            self.assertEqual(c.close(client, task['id'])['state'], 'closed')

        with self.slice_c_workspace('long_final_after_wait') as (root, c, client, task, thread):
            thread.join(5)
            self.assertFalse(thread.is_alive())
            final = c.status(client, task['id'])
            self.assertEqual(final['state'], 'completed', final.get('settlement_note'))
            self.assertEqual((root / 'result.txt').read_text(), 'ALPHA|BETA')
            self.assertIn('12 项测试通过', final['native_result']['result'])
            self.assertEqual(sum(r.get('method') == 'session/close' for r in self._requests(root)), 1)
            self.assertEqual(c.close(client, task['id'])['state'], 'closed')

    # --- Hypothesis 2: command advertisement async read loop, timeouts, model refresh ---

    def test_pipe_ensure_command_advertised_reuses_request_loop(self):
        def emit(writer, names):
            frame = {
                'jsonrpc': '2.0',
                'method': 'session/update',
                'params': {
                    'sessionId': 'owned',
                    'update': {
                        'sessionUpdate': 'available_commands_update',
                        'availableCommands': [{'name': n} for n in names],
                    },
                },
            }
            writer.write((json.dumps(frame) + '\n').encode())
            writer.flush()

        # (a) Immediate hit
        owner, _reader, _writer, cleanup = self._make_pipe_owner()
        try:
            owner.available_commands = {'swarm'}
            t0 = time.monotonic()
            self.assertTrue(owner.ensure_command_advertised('swarm', cold_timeout=1.0, settle_timeout=0.5))
            self.assertLess(time.monotonic() - t0, 0.1)
        finally:
            cleanup()

        # (b) Unreceived timeout -> False, count == 0
        owner, _reader, _writer, cleanup = self._make_pipe_owner()
        try:
            t0 = time.monotonic()
            self.assertFalse(owner.ensure_command_advertised('swarm', cold_timeout=0.25, settle_timeout=0.1))
            elapsed = time.monotonic() - t0
            self.assertGreaterEqual(elapsed, 0.2)
            self.assertEqual(owner.commands_advertised_count, 0)
        finally:
            cleanup()

        # (c) Already advertised missing -> skips cold_timeout, bounded by settle_timeout
        owner, _reader, writer, cleanup = self._make_pipe_owner()
        try:
            emit(writer, ['other'])
            owner.drain_pending()
            self.assertEqual(owner.commands_advertised_count, 1)
            t0 = time.monotonic()
            self.assertFalse(owner.ensure_command_advertised('swarm', cold_timeout=2.0, settle_timeout=0.15))
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 0.6)
            self.assertEqual(owner.commands_advertised_count, 1)
        finally:
            cleanup()

        # (d) Delayed arrival during cold wait
        owner, _reader, writer, cleanup = self._make_pipe_owner()
        try:
            timer = threading.Timer(0.12, lambda: emit(writer, ['swarm']))
            timer.start()
            self.assertTrue(owner.ensure_command_advertised('swarm', cold_timeout=1.0, settle_timeout=0.2))
            timer.join(1)
            self.assertEqual(owner.commands_advertised_count, 1)
            self.assertIn('swarm', owner.available_commands)
        finally:
            cleanup()

        # (e) Two-batch split: first batch exits cold wait early, second batch caught in settle wait
        owner, _reader, writer, cleanup = self._make_pipe_owner()
        try:
            t1 = threading.Timer(0.05, lambda: emit(writer, ['other']))
            t2 = threading.Timer(0.15, lambda: emit(writer, ['other', 'swarm']))
            t1.start()
            t2.start()
            t0 = time.monotonic()
            self.assertTrue(owner.ensure_command_advertised('swarm', cold_timeout=2.0, settle_timeout=0.5))
            elapsed = time.monotonic() - t0
            t1.join(1)
            t2.join(1)
            self.assertLess(elapsed, 0.8)
            self.assertEqual(owner.commands_advertised_count, 2)
        finally:
            cleanup()

        # (f) Empty list increments commands_advertised_count from 0 -> 1
        owner, _reader, writer, cleanup = self._make_pipe_owner()
        try:
            emit(writer, [])
            self.assertFalse(owner.ensure_command_advertised('swarm', cold_timeout=2.0, settle_timeout=0.15))
            self.assertEqual(owner.commands_advertised_count, 1)
            self.assertEqual(owner.available_commands, set())
        finally:
            cleanup()

    def test_e2e_delayed_command_advertisement_over_1s_succeeds(self):
        cases = [
            ('swarm', dict(AGY_SWARM_THIN='normal', AGY_TEST_CMD_MODE='delay', AGY_TEST_CMD_DELAY='1.35'),
             dict(model='ocs', agy_mode=None, agy_approval=None)),
            ('teamwork', dict(AGY_TEAMWORK_THIN='interview', AGY_TEST_CMD_MODE='delay', AGY_TEST_CMD_DELAY='1.35'),
             dict(model='oct', agy_mode=None, agy_approval=None)),
            ('dynamic', dict(AGY_DYNAMIC_FIXTURE='normal', AGY_TEST_CMD_MODE='delay', AGY_TEST_CMD_DELAY='1.35'),
             dict(model='gemini-4-argon', agy_mode='dynamic', agy_approval='skip-permissions')),
        ]
        for mode_label, env_extra, opts in cases:
            with self.subTest(mode=mode_label), self.fixture_workspace(env_extra=env_extra, **opts) as (root, c, client, task, thread):
                if mode_label == 'teamwork':
                    self._until(lambda: any(e['kind'] == 'answer' for e in c.events(client, task['id'])))
                    (root / 'finish').touch()
                thread.join(6)
                self.assertFalse(thread.is_alive())
                st = c.status(client, task['id'])
                self.assertEqual(st['state'], 'completed', st.get('settlement_note'))
                reqs = self._requests(root)
                self.assertEqual(sum(r.get('method') == 'session/prompt' for r in reqs), 1)
                self.assertEqual(sum(r.get('method') == 'session/close' for r in reqs), 1)
                self.assertEqual(c.close(client, task['id'])['state'], 'closed')

    def test_e2e_model_selection_refresh_adds_or_removes_command(self):
        for mode_name in ('refresh_on_model', 'refresh_on_model_async'):
            with self.subTest(mode=mode_name), self.fixture_workspace(
                env_extra=dict(AGY_SWARM_THIN='normal', AGY_TEST_CMD_MODE=mode_name, AGY_TEST_CMD_DELAY='0.25'),
                model='ocs',
            ) as (root, c, client, task, thread):
                thread.join(5)
                self.assertFalse(thread.is_alive())
                st = c.status(client, task['id'])
                self.assertEqual(st['state'], 'completed', st.get('settlement_note'))
                reqs = self._requests(root)
                methods = [r.get('method') for r in reqs]
                self.assertIn('session/set_config_option', methods)
                self.assertIn('session/prompt', methods)
                self.assertLess(methods.index('session/set_config_option'), methods.index('session/prompt'))
                self.assertEqual(c.close(client, task['id'])['state'], 'closed')

        with self.subTest(mode='drop_on_model'), self.fixture_workspace(
            env_extra=dict(AGY_SWARM_THIN='normal', AGY_TEST_CMD_MODE='drop_on_model'),
            model='ocs',
        ) as (root, c, client, task, thread):
            thread.join(5)
            self.assertFalse(thread.is_alive())
            st = c.status(client, task['id'])
            self.assertEqual(st['state'], 'unknown')
            self.assertIn('not advertised', st.get('settlement_note', ''))
            obs = st.get('native_observation', {})
            self.assertEqual(obs.get('command_advertisement_state'), 'advertised_missing')
            self.assertGreaterEqual(obs.get('commands_advertised_count', 0), 2)
            self.assertEqual(st.get('native_cleanup', {}).get('kind'), 'fresh-no-prompt')
            reqs = self._requests(root)
            self.assertFalse(any(r.get('method') == 'session/prompt' for r in reqs))
            self.assertEqual(c.close(client, task['id'])['state'], 'unknown')

    def test_e2e_timeout_unreceived_vs_advertised_missing_and_reused_cache(self):
        # 1) Zero advertisements sent -> not_received_timeout
        with self.subTest(case='unreceived_timeout'), self.fixture_workspace(
            env_extra=dict(AGY_SWARM_THIN='no-command'),
            model='ocs',
        ) as (root, c, client, task, thread):
            thread.join(5)
            self.assertFalse(thread.is_alive())
            st = c.status(client, task['id'])
            self.assertEqual(st['state'], 'unknown')
            self.assertIn('not advertised', st.get('settlement_note', ''))
            obs = st.get('native_observation', {})
            self.assertEqual(obs.get('command_advertisement_state'), 'not_received_timeout')
            self.assertEqual(obs.get('commands_advertised_count'), 0)
            self.assertEqual(obs.get('observed_commands_count'), 0)
            self.assertEqual(st.get('native_cleanup', {}).get('kind'), 'fresh-no-prompt')
            self.assertFalse(any(r.get('method') == 'session/prompt' for r in self._requests(root)))
            self.assertEqual(c.close(client, task['id'])['state'], 'unknown')

        # 2) Advertised list missing target -> advertised_missing faster than cold timeout
        for cmd_mode, expected_observed in (('missing_other', 1), ('missing_empty', 0)):
            with self.subTest(case=cmd_mode), patch.object(agy_native, 'COMMAND_ADVERTISE_COLD_TIMEOUT', 4.0, create=True), \
                 patch.object(agy_native, 'COMMAND_ADVERTISE_SETTLE_TIMEOUT', 0.2, create=True), \
                 self.fixture_workspace(
                     env_extra=dict(AGY_SWARM_THIN='normal', AGY_TEST_CMD_MODE=cmd_mode),
                     model='ocs',
                 ) as (root, c, client, task, thread):
                t0 = time.monotonic()
                thread.join(5)
                elapsed = time.monotonic() - t0
                self.assertFalse(thread.is_alive())
                self.assertLess(elapsed, 2.0)
                st = c.status(client, task['id'])
                self.assertEqual(st['state'], 'unknown')
                self.assertIn('not advertised', st.get('settlement_note', ''))
                obs = st.get('native_observation', {})
                self.assertEqual(obs.get('command_advertisement_state'), 'advertised_missing')
                self.assertEqual(obs.get('commands_advertised_count'), 1)
                self.assertEqual(obs.get('observed_commands_count'), expected_observed)
                self.assertEqual(st.get('native_cleanup', {}).get('kind'), 'fresh-no-prompt')
                self.assertFalse(any(r.get('method') == 'session/prompt' for r in self._requests(root)))
                self.assertEqual(c.close(client, task['id'])['state'], 'unknown')

        # 3) Reused non-resumed session: cache used when no live update, overwritten by live empty update
        sid = '11111111-1111-4111-8111-111111111111'
        with self.subTest(case='reused_cache_hit'), patch.object(agy_native, 'COMMAND_ADVERTISE_COLD_TIMEOUT', 4.0, create=True), \
             self.fixture_workspace(
                 env_extra=dict(AGY_SWARM_THIN='no-command'),
                 model='ocs',
                 native_session_id=sid,
                 native_capabilities=dict(session_id=sid, advertised_commands=['swarm']),
             ) as (root, c, client, task, thread):
            t0 = time.monotonic()
            thread.join(5)
            elapsed = time.monotonic() - t0
            self.assertFalse(thread.is_alive())
            self.assertLess(elapsed, 2.0)
            st = c.status(client, task['id'])
            self.assertEqual(st['state'], 'completed', st.get('settlement_note'))
            self.assertEqual(sum(r.get('method') == 'session/prompt' for r in self._requests(root)), 1)

        with self.subTest(case='reused_cache_overwritten_by_empty_live_update'), \
             patch.object(agy_native, 'COMMAND_ADVERTISE_SETTLE_TIMEOUT', 0.2, create=True), \
             self.fixture_workspace(
                 env_extra=dict(AGY_SWARM_THIN='normal', AGY_TEST_CMD_MODE='load_empty'),
                 model='ocs',
                 native_session_id=sid,
                 native_capabilities=dict(session_id=sid, advertised_commands=['swarm']),
             ) as (root, c, client, task, thread):
            thread.join(5)
            self.assertFalse(thread.is_alive())
            st = c.status(client, task['id'])
            self.assertEqual(st['state'], 'unknown')
            self.assertIn('not advertised', st.get('settlement_note', ''))
            obs = st.get('native_observation', {})
            self.assertEqual(obs.get('command_advertisement_state'), 'advertised_missing')
            self.assertGreaterEqual(obs.get('commands_advertised_count', 0), 1)
            self.assertEqual(obs.get('observed_commands_count'), 0)
            self.assertFalse(any(r.get('method') == 'session/prompt' for r in self._requests(root)))


if __name__ == '__main__':
    unittest.main()
