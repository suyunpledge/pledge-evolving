import tempfile
import time
import unittest
from pathlib import Path

from forge.desktop_services import DesktopStore, ConnectorService, OPERATIONS
from forge.policy import Policy, Mode, Sandbox


class DesktopServicesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = DesktopStore(Path(self.tmp.name) / 'desktop.sqlite3')

    def test_schedule_claim_is_atomic_across_instances(self):
        job = self.store.add_job('task', 'explain README', self.tmp.name,
                                 ['p', 'm'], time.time()-1, 60)
        other = DesktopStore(self.store.path)
        self.assertEqual(self.store.claim_due()[0]['id'], job)
        self.assertEqual(other.claim_due(), [])
        self.store.finish_job(job, 'done', 'result')
        self.assertEqual(other.jobs()[0]['status'], 'done')

    def test_write_needs_grant_policy_and_exact_confirmation(self):
        calls, prompts = [], []
        self.store.set_account('github', {'agent': True, 'read': True, 'write': False})
        svc = ConnectorService(self.store, lambda _: 'ghp_fake_test',
                               transport=lambda *a, **kw: calls.append(a) or {'id': 1})
        policy = Policy(mode=Mode.DEFAULT)
        args = {'owner': 'a', 'repo': 'b', 'title': 'hello', 'body': 'text'}
        with self.assertRaises(PermissionError):
            svc.invoke('connector_github_issue_create', args, policy, approve=lambda *a: True)
        self.store.set_account('github', {'agent': True, 'read': True, 'write': True})
        with self.assertRaises(PermissionError):
            svc.invoke('connector_github_issue_create', args, policy, approve=lambda *a: False)
        self.assertEqual(calls, [])
        svc.invoke('connector_github_issue_create', args, policy,
                   approve=lambda *a: prompts.append(a) or True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(prompts), 1)
        self.assertNotIn('ghp_fake_test', str(prompts))

    def test_revoked_runtime_and_path_injection(self):
        self.store.set_account('github', {'agent': True, 'read': True})
        svc = ConnectorService(self.store, lambda _: 'token', transport=lambda *a, **kw: {})
        self.assertTrue(svc.schemas())
        self.store.set_account('github', {'agent': False, 'read': True})
        with self.assertRaises(PermissionError):
            svc.invoke('connector_github_repos', {}, Policy(mode=Mode.DEFAULT))
        self.store.set_account('github', {'agent': True, 'read': True})
        with self.assertRaises(ValueError):
            svc.invoke('connector_github_issues', {'owner': '..', 'repo': 'x'}, Policy(mode=Mode.DEFAULT))

    def test_policy_deny_wins_over_write_confirmation(self):
        self.store.set_account('github', {'agent': True, 'read': True, 'write': True})
        svc = ConnectorService(self.store, lambda _: 'token', transport=lambda *a, **kw: self.fail())
        with self.assertRaises(PermissionError):
            svc.invoke('connector_github_issue_create', {'owner': 'a', 'repo': 'b', 'title': 'x'},
                       Policy(mode=Mode.DEFAULT, deny=('connector_*',)), approve=lambda *a: True)

    def test_operation_catalog_has_no_arbitrary_endpoint(self):
        for op in OPERATIONS.values():
            self.assertNotIn('url', op.fields)

    def test_registered_connector_uses_policy_and_redacts_encoded_file(self):
        from forge.tools import ToolContext
        import base64
        key='sk-connector-synthetic-test-0987654321'
        self.store.set_account('github',{'agent':True,'read':True})
        service=ConnectorService(self.store,lambda _:'ghp_synthetic_0987654321',
            transport=lambda *a,**kw:{'encoding':'base64','content':base64.b64encode(('API_KEY='+key).encode()).decode()})
        registry=service.registry()
        result=registry.invoke('connector_github_file',{'owner':'a','repo':'b','path':'.env'},
                              ToolContext(Policy(mode=Mode.DEFAULT),Path(self.tmp.name)))
        self.assertTrue(result.ok,result.error)
        self.assertNotIn(key,result.content)
        self.assertNotIn('base64',result.content)

    def test_write_denied_in_read_only_even_with_confirmation(self):
        self.store.set_account('microsoft',{'agent':True,'write':True,'scopes':['Mail.Send']})
        svc=ConnectorService(self.store,lambda _:'unused',transport=lambda *a,**kw:self.fail())
        with self.assertRaises(PermissionError):
            svc.invoke('connector_microsoft_mail_send',{'to':'a@example.com','subject':'x','body':'x'},
                       Policy(mode=Mode.READ_ONLY),approve=lambda *a:True)
        with self.assertRaises(PermissionError):
            svc.invoke('connector_microsoft_mail_send',{'to':'a@example.com','subject':'x','body':'x'},
                       Policy(mode=Mode.BYPASS,sandbox=Sandbox.READ_ONLY),approve=lambda *a:True)

    def test_grant_revoked_while_confirmation_is_open(self):
        self.store.set_account('github',{'agent':True,'write':True})
        svc=ConnectorService(self.store,lambda _:'unused',transport=lambda *a,**kw:self.fail())
        def approve(*args):
            self.store.set_account('github',{'agent':False}); return True
        with self.assertRaises(PermissionError):
            svc.invoke('connector_github_issue_create',{'owner':'a','repo':'b','title':'x'},Policy(mode=Mode.DEFAULT),approve=approve)

    def test_job_prompt_does_not_persist_a_secret(self):
        with self.assertRaises(ValueError):
            self.store.add_job('x','API_KEY=sk-synthetic-never-persist-123456789',self.tmp.name,['p','m'],time.time())

    def test_late_credential_callback_cannot_resurrect_revoked_account(self):
        self.store.set_account('microsoft',{'agent':True,'updating':True})
        expected=self.store.account('microsoft')
        self.store.set_account('microsoft',{})
        saved=[]
        with self.assertRaises(PermissionError):
            self.store.finish_account('microsoft',expected,{'agent':True},lambda:saved.append('credential'))
        self.assertEqual(saved,[])
        self.assertNotIn('agent',self.store.account('microsoft'))

    def test_crashed_occurrence_is_visible_and_never_replayed_automatically(self):
        key=self.store.add_job('x','task',self.tmp.name,['p','m'],100,60)
        claimed=self.store.claim_due(now=101)[0]
        self.assertEqual(self.store.claim_due(now=500),[])
        self.assertEqual(self.store.jobs()[0]['status'],'interrupted')
        self.assertFalse(self.store.jobs()[0]['enabled'])
        self.store.enable_job(key,True)
        next_run=self.store.claim_due(now=501)[0]
        self.store.finish_job(key,'done','stale',started=claimed['started'])
        self.assertEqual(self.store.jobs()[0]['status'],'running')
        self.store.finish_job(key,'done','current',started=next_run['started'])
        self.assertEqual(self.store.jobs()[0]['result'],'current')

    def test_device_flow_handles_slow_down_and_only_commits_matching_generation(self):
        calls,saved,notices=[],[],[]
        class Cancel:
            def __init__(self): self.waits=[]
            def wait(self,seconds): self.waits.append(seconds); return False
            def is_set(self): return False
        cancel=Cancel()
        responses=[{'user_code':'TEST-CODE','device_code':'opaque','expires_in':600,'interval':5},
                   {'error':'slow_down'},{'access_token':'synthetic-access-token-1234','refresh_token':'synthetic-refresh-token-1234',
                                          'expires_in':3600,'scope':'User.Read offline_access Mail.Read'}]
        def transport(*args,**kwargs): calls.append(args); return responses.pop(0)
        service=ConnectorService(self.store,lambda _:None,transport=transport,
                                  save_credential=lambda name,value:saved.append((name,value)))
        service.device_login({'client_id':'test-client','tenant':'common','scopes':['Mail.Read'],'read':True},
                             lambda *args:notices.append(args),cancel)
        self.assertEqual(cancel.waits,[5,10])
        self.assertEqual(len(saved),1)
        self.assertEqual(self.store.account('microsoft')['scopes'],['Mail.Read'])
        self.assertTrue(self.store.account('microsoft')['authenticated'])
        self.assertEqual(notices,[('TEST-CODE','https://microsoft.com/devicelogin')])

    def test_each_write_preview_is_complete_and_not_truncated(self):
        self.store.set_account('github',{'agent':True,'write':True})
        service=ConnectorService(self.store,lambda _:'unused',transport=lambda *a,**kw:self.fail())
        with self.assertRaises(ValueError):
            service.invoke('connector_github_issue_create',{'owner':'a','repo':'b','title':'x','body':'x'*13000},
                           Policy(mode=Mode.DEFAULT),approve=lambda *a:self.fail())

    def test_every_declared_write_constructs_a_fixed_host_request_after_confirmation(self):
        import json
        common={'owner':'owner','repo':'repo','number':'1','title':'title','body':'ordinary text',
                'head':'feature','base':'main','path':'notes/file.txt','content':'ordinary content',
                'message':'update file','sha':'abc','branch':'main','to':'recipient@example.com',
                'subject':'subject','id':'item','name':'renamed.txt','team':'team','channel':'channel',
                'start':'2026-10-10T10:00:00','end':'2026-10-10T11:00:00','timezone':'UTC',
                'sheet':'Sheet1','address':'A1','values':'[[42]]','state':'closed'}
        scopes=sorted({op.scope for op in OPERATIONS.values() if op.scope})
        for account in ('github','microsoft'):
            self.store.set_account(account,{'agent':True,'read':True,'write':True,'scopes':scopes})
        sent=[]; confirmed=[]
        token='synthetic-connector-credential-123456'
        service=ConnectorService(self.store,lambda name:token if name=='github' else json.dumps({'access_token':token,'expires_at':time.time()+3600}),
            transport=lambda *args,**kw:sent.append(args) or {'accepted':True})
        for name,op in OPERATIONS.items():
            if not op.write: continue
            with self.subTest(operation=name):
                args={field:common[field] for field in op.fields}
                output=service.invoke(name,args,Policy(mode=Mode.DEFAULT),
                    approve=lambda n,a:confirmed.append((n,a)) or True)
                url,method,headers,body=sent[-1]
                self.assertEqual(confirmed[-1],(name,args)); self.assertEqual(method,op.method)
                self.assertTrue(url.startswith('https://api.github.com/' if op.account=='github' else 'https://graph.microsoft.com/v1.0/'))
                self.assertEqual(headers['Authorization'],'Bearer '+token)
                self.assertNotIn(token,str(body)); self.assertNotIn(token,str(output))
                if name.endswith('_excel_range_update'): self.assertEqual(body,{'values':[[42]]})
                if name.endswith('_mail_send'): self.assertEqual(body['message']['body']['contentType'],'Text')

    def test_transport_bounds_responses_and_does_not_echo_http_errors(self):
        import io
        import threading
        from urllib.error import HTTPError
        from forge.desktop_services import request_json
        class Large(io.BytesIO):
            def read(self,limit=-1): return b'x'*(2*1024*1024+1)
        with self.assertRaisesRegex(ValueError,'2 MiB'):
            request_json('https://api.github.com/user/repos','GET',opener=lambda *a,**kw:Large())
        def redirected(*a,**kw):
            raise HTTPError('https://api.github.com/',302,'private upstream detail',{},io.BytesIO(b'secret error body'))
        with self.assertRaisesRegex(RuntimeError,'^Connector HTTP 302$'):
            request_json('https://api.github.com/user/repos','GET',opener=redirected)
        cancelled=threading.Event(); cancelled.set()
        with self.assertRaisesRegex(RuntimeError,'cancelled'):
            request_json('https://api.github.com/user/repos','GET',cancel_event=cancelled,opener=lambda *a,**kw:self.fail('cancelled request sent'))


if __name__ == '__main__':
    unittest.main()
