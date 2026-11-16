"""Behavioral checks for the common text/native tool execution boundary."""
from types import SimpleNamespace
import unittest

from .execution_guard import ExecutionGuard
from .tool_turn import execute_turn
from .tools import ToolResult
from .toolwire import ToolCall


class ToolTurnTests(unittest.TestCase):
    def turn(self, results, *, native=False, max_result=4000, offload=False, calls=None):
        self.invoked, self.events, self.steps = [], [], []
        pending = iter(results)
        def invoke(name, args, **kwargs):
            self.invoked.append((name, args, kwargs))
            result = next(pending)
            result.meta.setdefault('action_id', 'action-' + str(len(self.invoked)))
            return result
        offloader = SimpleNamespace(policy=SimpleNamespace(should_offload=lambda n, t: offload),
                                    wrap_result_text=lambda n, i, t: 'artifact:' + i,
                                    last_path='local/artifact.txt')
        return execute_turn(calls or [ToolCall(str(i), 'read_file', {'path': 'a'})
                                     for i in range(len(results))], native=native, wire='openai',
            steps=self.steps, usage_delta={'prompt_tokens': 12}, max_result=max_result,
            invoke=invoke, emit=lambda **e: self.events.append(e), offloader=offloader,
            guard=ExecutionGuard(), supervisor_row=lambda n, a, r: {'tool': n, 'ok': r.ok})

    def test_text_and_native_share_results_and_usage(self):
        for native in (False, True):
            turn = self.turn([ToolResult(True, 'file text')], native=native)
            self.assertEqual(turn.answered[0][1:], ('file text', True))
            self.assertEqual(self.steps[0].meta['usage_delta'], {'prompt_tokens': 12})
            self.assertEqual(self.steps[0].meta.get('native_call', False), native)
            self.assertEqual(self.invoked[0][2], {'tool_call_id': '0'} if native else {})

    def test_unknown_outcome_stops_both_protocols_before_next_action(self):
        for native in (False, True):
            turn = self.turn([ToolResult(False, error='interrupted', meta={'execution_state': 'unknown'}),
                              ToolResult(True, 'must not execute')], native=native)
            self.assertEqual(turn.stopped, 'unknown_outcome')
            self.assertEqual(len(self.invoked), 1)

    def test_repeated_success_stops_remaining_calls(self):
        turn = self.turn([ToolResult(True, 'same') for _ in range(6)])
        self.assertEqual(turn.stopped, 'stalled')
        self.assertEqual(len(self.invoked), 5)

    def test_repeated_errors_stop_earlier(self):
        turn = self.turn([ToolResult(False, error='same') for _ in range(4)], native=True)
        self.assertEqual(turn.stopped, 'stalled')
        self.assertEqual(len(self.invoked), 3)

    def test_denial_keeps_audit_and_supervisor_evidence(self):
        turn = self.turn([ToolResult(False, error='denied', meta={'authorization': 'deny'})])
        self.assertEqual(self.steps[0].decision, 'deny')
        self.assertEqual(self.events[0]['authorization'], 'deny')
        self.assertEqual(turn.supervisor_rows, [{'tool': 'read_file', 'ok': False}])

    def test_long_output_keeps_full_evidence_outside_prompt_slice(self):
        self.turn([ToolResult(True, 'abcdefgh')], max_result=3)
        self.assertEqual(self.steps[0].result, 'abc')
        self.assertEqual(self.steps[0].meta['result_full'], 'abcdefgh')

    def test_offload_preserves_handle_and_checkpoint_for_native(self):
        self.turn([ToolResult(True, 'large output', meta={'checkpoint': '0123456789'})],
                  native=True, offload=True)
        self.assertEqual(self.steps[0].result, 'artifact:action-1')
        self.assertEqual(self.steps[0].note, 'checkpoint 01234567')
        self.assertEqual([e['type'] for e in self.events], ['tool_result_offloaded', 'tool_call'])
        self.assertEqual(self.events[-1]['call_id'], '0')

    def test_exception_does_not_dispatch_next_call(self):
        with self.assertRaises(StopIteration):
            self.turn([], calls=[ToolCall('1', 'read_file'), ToolCall('2', 'read_file')])
        self.assertEqual(len(self.invoked), 1)


if __name__ == '__main__':
    unittest.main()
