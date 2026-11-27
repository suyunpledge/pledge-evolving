import unittest

from .turn_usage import add_report, add_request, context_limit


class TurnUsageTests(unittest.TestCase):
    def test_totals_use_observed_requests_and_latest_executor_context(self):
        state = add_request({}, {'prompt_tokens': 1000, 'completion_tokens': 50,
                                 'prompt_tokens_details': {'cached_tokens': 600}}, window=4096)
        state = add_report(state, {'requests': [
            {'usage': {'prompt_tokens': 1500, 'completion_tokens': 30, 'cached_tokens': 900},
             'plan': {'request_budget': {'context_window': 8192}}}]})
        self.assertEqual((state['input'], state['output'], state['cached'], state['cacheInput']),
                         (2500, 80, 1500, 2500))
        self.assertEqual((state['context'], state['limit']), (1500, 8192))
        state = add_request(state, None, executor=False)
        self.assertEqual((state['context'], state['requests'], state['cacheReports']), (1500, 3, 2))
        state = add_request(state, {'prompt_tokens': 0, 'input_observed': False,
                                    'completion_tokens': 0, 'output_observed': False,
                                    'cached_tokens': 0, 'cache_observed': False}, window=8192)
        self.assertNotIn('context', state)
        self.assertEqual((state['inputReports'], state['outputReports'], state['cacheReports']), (2, 2, 2))

    def test_limits_use_real_config_and_cache_can_be_unknown_or_zero(self):
        config = {'model': 'custom', 'routeCapabilities': {'contextWindow': 131072},
                  'modelCapabilities': {'custom': {'contextWindow': 65536}}}
        self.assertEqual(context_limit(config), 65536)
        self.assertIsNone(context_limit({}))
        self.assertNotIn('cached', add_request({}, {'prompt_tokens': 100}))
        self.assertNotIn('cached', add_request({}, {'prompt_tokens': 100, 'cached_tokens': 101}))
        self.assertEqual(add_request({}, {'prompt_tokens': 100, 'cached_tokens': 0})['cached'], 0)

    def test_report_replay_keeps_executor_window_after_smaller_reflection(self):
        report = {'requests': [
            {'usage': {'prompt_tokens': 150, 'completion_tokens': 50}, 'plan': {'request_budget': {'context_window': 8192}}},
            {'phase': 'reflection', 'usage': {'prompt_tokens': 70, 'completion_tokens': 10},
             'plan': {'request_budget': {'context_window': 4096}}}]}
        u = add_report({}, report)
        self.assertEqual((u['input'], u['output'], u['context'], u['limit']), (220, 60, 150, 8192))
