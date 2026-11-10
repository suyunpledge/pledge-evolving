"""A pre-execution stop must still yield reviewable challenge evidence."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import verify_tool_challenge as challenge


class ChallengeEvidenceTests(unittest.TestCase):
    def test_planning_stop_with_no_messages_is_retained_as_failed_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            agent=SimpleNamespace(_last_messages=None,secret_scope=SimpleNamespace(close=lambda:None),
                run=lambda *args,**kw:SimpleNamespace(stopped='planning_error',text='planner rejected',
                    steps=[],planning=None,events=[]))
            with patch.object(challenge,'router_for'):
                cases=challenge.run_case(None,SimpleNamespace(rows=[]),root,root/'evidence.json',
                    agent_cls=lambda **kw:agent)
            self.assertEqual(len(cases),1)
            self.assertFalse(cases[0]['ok'])
            self.assertEqual(cases[0]['stopped'],'planning_error')
            self.assertEqual(cases[0]['context_summaries'],[])
            self.assertEqual(cases[0]['changed_files'],[])
            self.assertFalse(cases[0]['failure_reproduced_before_edit'])
            self.assertFalse(cases[0]['final']['passed'])


if __name__=='__main__':unittest.main()
