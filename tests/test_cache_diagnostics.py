"""Wire evidence separates unknown/zero/positive cache without leaking opaque data."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from verify_cache_diagnostics import DiagnosticRecorder
from forge.cache_metrics import cache_coverage
from forge.model import Provider


class WireDiagnosticTests(unittest.TestCase):
    def test_identical_wire_can_have_different_cache_receipts_and_replay_is_hash_only(self):
        sent=[]
        class Response(io.BytesIO):
            status=200
            headers={'x-request-id':'fixture-id'}
        def opener(request,**kw):
            sent.append(request.data)
            message={'role':'assistant','content':'OK'}
            if len(sent)==1:
                message={'role':'assistant','content':'','reasoning_content':'PRIVATE_THOUGHT_VALUE',
                    'encrypted_content':'PRIVATE_OPAQUE_VALUE','tool_calls':[{'id':'read','type':'function',
                        'function':{'name':'read_file','arguments':'{"path":"value.txt"}'}}]}
            details=None if len(sent)==1 else {'cached_tokens':0 if len(sent)==2 else 256}
            return Response(json.dumps({'choices':[{'message':message,'finish_reason':'tool_calls' if len(sent)==1 else 'stop'}],
                'system_fingerprint':'fixture-backend','usage':{'prompt_tokens':1000,'completion_tokens':10,
                'prompt_tokens_details':details}}).encode())
        recorder=DiagnosticRecorder(3)
        provider=Provider('fixture','https://ark.cn-beijing.volces.com/api/coding/v3',default_model='doubao-seed-evolving')
        messages=[{'role':'user','content':'Read value.txt.'}]
        with patch('verify_mimo_live.open_authenticated',side_effect=opener),contextlib.redirect_stdout(io.StringIO()):
            first=recorder.complete(provider,provider.default_model,messages)
            history=[*messages,first[2]['assistant_message'],{'role':'tool','tool_call_id':'read','content':'actual value'}]
            recorder.complete(provider,provider.default_model,history)
            recorder.complete(provider,provider.default_model,history)
        self.assertEqual(sent[1],sent[2])
        self.assertEqual(recorder.rows[1]['wire_body_sha256'],hashlib.sha256(sent[1]).hexdigest())
        self.assertTrue(recorder.rows[2]['exact_payload_repeat'])
        self.assertEqual(recorder.rows[2]['prefix_relation'],'unchanged')
        self.assertEqual(recorder.rows[1]['prefix_relation'],'appended')
        fields=recorder.rows[1]['message_components'][1]['fields']
        self.assertTrue(fields['reasoning_content']['matches_prior_response'])
        self.assertTrue(fields['encrypted_content']['matches_prior_response'])
        self.assertTrue(fields['reasoning_content']['matches_response_call'])
        self.assertTrue(fields['encrypted_content']['matches_response_call'])
        self.assertNotIn('PRIVATE_THOUGHT_VALUE',json.dumps(recorder.rows))
        self.assertNotIn('PRIVATE_OPAQUE_VALUE',json.dumps(recorder.rows))
        self.assertEqual(recorder.rows[1]['system_fingerprint'],'fixture-backend')
        coverage=cache_coverage(recorder.rows)
        self.assertEqual(coverage['unknown_requests'],1)
        self.assertEqual(coverage['all_observed']['input_tokens'],2000)
        self.assertEqual(coverage['all_observed']['cached_tokens'],256)


if __name__=='__main__':unittest.main()
