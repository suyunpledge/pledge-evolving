"""Probe integrity: later failures preserve evidence; credentials stay endpoint-bound."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import verify_seed_execution as probe


class SeedProbeTests(unittest.TestCase):
    def test_later_transport_failure_preserves_completed_mode(self):
        recorder=probe.SeedRecorder(8)
        with patch.object(probe,'wire_probe',side_effect=[
                [{'thinking':'off','ok':True}],TimeoutError('fixture')]):
            cases=probe.wire_cases(None,recorder)
        self.assertEqual(cases,[{'thinking':'off','ok':True},
                               {'thinking':'on','ok':False,'error_type':'TimeoutError'}])
        self.assertIsNone(recorder.thinking)

    def test_credential_selection_rejects_disabled_or_other_destinations(self):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td)
            rows=[{'id':'fixture','config':{'model':'doubao-seed-evolving',
                    'baseURL':'https://other.invalid/api/coding/v3'}}]
            (home/'secrets.json').write_text(json.dumps({'fixture':'synthetic-fixture-value'}))
            for disabled in (False,True):
                rows[0]['disabled']=disabled
                if disabled: rows[0]['config']['baseURL']='https://ark.cn-beijing.volces.com/api/coding/v3'
                (home/'forge.patch.json').write_text(json.dumps(rows))
                with self.assertRaises(ValueError):
                    probe.provider_for('doubao-seed-evolving',home)


if __name__=='__main__':
    unittest.main()
