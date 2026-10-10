"""Desktop jobs and mediated connectors. No plugin imports or arbitrary HTTP tool.

Credentials are supplied by a trusted host callback; grants are re-read at every
invocation. A write requires a concrete per-invocation confirmation, even with
an account-wide write grant. This is a permission gate, not an OS sandbox.
"""
from __future__ import annotations

import copy
import json
import re
import sqlite3
import time
import uuid
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError

from .policy import Decision, Mode, Sandbox
from .secrets import SecretScope, redact


class DesktopStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS accounts
              (id TEXT PRIMARY KEY, data TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL,
              due REAL NOT NULL, interval REAL NOT NULL, enabled INTEGER NOT NULL,
              status TEXT NOT NULL, result TEXT NOT NULL DEFAULT '');
              CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, at REAL,
              operation TEXT, data TEXT);''')
            db.execute('BEGIN IMMEDIATE')
            if 'started' not in {row[1] for row in db.execute('PRAGMA table_info(jobs)')}:
                db.execute('ALTER TABLE jobs ADD COLUMN started REAL NOT NULL DEFAULT 0')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    def account(self, name):
        with self.connect() as db:
            row = db.execute('SELECT data FROM accounts WHERE id=?', (name,)).fetchone()
        return json.loads(row[0]) if row else {}

    def set_account(self, name, data):
        # Never persist tokens/client secrets in this ordinary configuration DB.
        safe = self._account_data(data)
        safe['revision']=uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO accounts VALUES (?,?)', (name, json.dumps(safe)))

    @staticmethod
    def _account_data(data):
        return {k:data[k] for k in ('agent','read','write','client_id','tenant','scopes','updating','authenticated') if k in data}

    def finish_account(self,name,expected,data,save_credential):
        """Commit credentials under the same lock as their account generation.

        Readers see an updating account until this transaction commits. A late
        OAuth/refresh callback cannot resurrect a disconnected account.
        """
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT data FROM accounts WHERE id=?',(name,)).fetchone()
            if (json.loads(row[0]) if row else {})!=expected:
                raise PermissionError('Account changed while authorizing; sign in again')
            save_credential()
            if data is not None:
                safe=self._account_data(data); safe['revision']=uuid.uuid4().hex
                db.execute('INSERT OR REPLACE INTO accounts VALUES (?,?)',(name,json.dumps(safe)))

    def audit(self, operation, data):
        with self.connect() as db:
            db.execute('INSERT INTO audit(at,operation,data) VALUES (?,?,?)',
                       (time.time(), operation, json.dumps(redact(data), ensure_ascii=False)[:16000]))
            db.execute('DELETE FROM audit WHERE id < (SELECT MAX(id)-1000 FROM audit)')

    def add_job(self, name, prompt, workspace, model, due, interval=0):
        from .phase_models import model_pair
        pair = model_pair(model)
        if pair is None: raise ValueError('Select a configured provider/model')
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 16000:
            raise ValueError('Task must contain 1–16000 characters')
        if redact(prompt) != prompt or 'SECRET_REF' in prompt:
            raise ValueError('Scheduled prompts cannot contain secrets or session SecretRefs')
        if not Path(workspace).is_dir(): raise ValueError('Workspace does not exist')
        if not isinstance(interval, (float, int)) or interval != 0 and not 60 <= interval <= 31536000:
            raise ValueError('Repeat interval must be at least 60 seconds')
        if not isinstance(due, (float, int)) or not 0 < due < 32503680000:
            raise ValueError('Invalid due time')
        key = uuid.uuid4().hex
        data = {'name': str(redact(str(name)))[:120], 'prompt': prompt, 'workspace': str(Path(workspace).resolve()),
                'model': list(pair)}
        with self.connect() as db:
            if db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]>=100:
                raise ValueError('Maximum of 100 stored scheduled tasks reached')
            db.execute('INSERT INTO jobs(id,data,due,interval,enabled,status) VALUES (?,?,?,?,1,?)',
                       (key, json.dumps(data), due, interval, 'waiting'))
        return key

    def jobs(self):
        with self.connect() as db: rows = db.execute('SELECT * FROM jobs ORDER BY due LIMIT 100').fetchall()
        return [{**dict(r), **json.loads(r['data'])} for r in rows]

    def enable_job(self, key, enabled):
        with self.connect() as db:
            db.execute('UPDATE jobs SET enabled=? WHERE id=?', (int(bool(enabled)), key))

    def claim_due(self, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE jobs SET status='interrupted',enabled=0 WHERE status='running' AND started<?",(now-300,))
            row = db.execute("SELECT * FROM jobs WHERE enabled=1 AND due<=? AND status!='running' ORDER BY due LIMIT 1", (now,)).fetchone()
            if not row: return []
            # Persist BEFORE sending a paid request; crashes leave a visible
            # running occurrence, never silently replay it after restart.
            db.execute("UPDATE jobs SET status='running', due=?, enabled=?, started=? WHERE id=?",
                       (now+row['interval'] if row['interval'] else row['due'],
                        1 if row['interval'] else 0,now,row['id']))
        return [{**dict(row), **json.loads(row['data']), 'started':now}]

    def finish_job(self, key, status, result, *, started=None):
        with self.connect() as db:
            query='UPDATE jobs SET status=?,result=? WHERE id=?'
            params=(status, str(redact(result))[:16000], key)
            if started is not None:
                query+=' AND started=?'; params+= (started,)
            updated=db.execute(query,params).rowcount
        if not updated: return
        self.audit('schedule.'+status, {'job': key})


@dataclass(frozen=True)
class Operation:
    account: str
    method: str
    route: str
    fields: tuple[str, ...] = ()
    required: tuple[str, ...] = ()
    scope: str = ''

    @property
    def write(self): return self.method != 'GET'


OPERATIONS = {}
def _op(name, account, method, route, fields=(), required=(), scope=''):
    OPERATIONS['connector_'+account+'_'+name] = Operation(account, method, route, tuple(fields), tuple(required), scope)

_op('repos', 'github', 'GET', '/user/repos?per_page=30')
_op('issues', 'github', 'GET', '/repos/{owner}/{repo}/issues?per_page=30', ('owner','repo'), ('owner','repo'))
_op('pulls', 'github', 'GET', '/repos/{owner}/{repo}/pulls?per_page=30', ('owner','repo'), ('owner','repo'))
_op('file', 'github', 'GET', '/repos/{owner}/{repo}/contents/{path}', ('owner','repo','path','ref'), ('owner','repo','path'))
_op('issue_create', 'github', 'POST', '/repos/{owner}/{repo}/issues', ('owner','repo','title','body'), ('owner','repo','title'))
_op('issue_update', 'github', 'PATCH', '/repos/{owner}/{repo}/issues/{number}', ('owner','repo','number','title','body','state'), ('owner','repo','number'))
_op('pull_create', 'github', 'POST', '/repos/{owner}/{repo}/pulls', ('owner','repo','title','body','head','base'), ('owner','repo','title','head','base'))
_op('pull_update', 'github', 'PATCH', '/repos/{owner}/{repo}/pulls/{number}', ('owner','repo','number','title','body','state','base'), ('owner','repo','number'))
_op('file_write', 'github', 'PUT', '/repos/{owner}/{repo}/contents/{path}', ('owner','repo','path','content','message','sha','branch'), ('owner','repo','path','content','message'))
_op('mail', 'microsoft', 'GET', '/me/messages?$top=20&$select=id,subject,from,receivedDateTime,bodyPreview', scope='Mail.Read')
_op('mail_send', 'microsoft', 'POST', '/me/sendMail', ('to','subject','body'), ('to','subject','body'), 'Mail.Send')
_op('mail_draft_create', 'microsoft', 'POST', '/me/messages', ('to','subject','body'), ('to','subject','body'), 'Mail.ReadWrite')
_op('mail_draft_update', 'microsoft', 'PATCH', '/me/messages/{id}', ('id','subject','body'), ('id',), 'Mail.ReadWrite')
_op('events', 'microsoft', 'GET', '/me/events?$top=20', scope='Calendars.Read')
_op('event_create', 'microsoft', 'POST', '/me/events', ('subject','body','start','end','timezone'), ('subject','start','end','timezone'), 'Calendars.ReadWrite')
_op('event_update', 'microsoft', 'PATCH', '/me/events/{id}', ('id','subject','body','start','end','timezone'), ('id',), 'Calendars.ReadWrite')
_op('files', 'microsoft', 'GET', '/me/drive/root/children?$top=30', scope='Files.Read')
_op('file_write', 'microsoft', 'PUT', '/me/drive/root:/{path}:/content', ('path','content'), ('path','content'), 'Files.ReadWrite')
_op('file_rename', 'microsoft', 'PATCH', '/me/drive/items/{id}', ('id','name'), ('id','name'), 'Files.ReadWrite')
_op('sites', 'microsoft', 'GET', '/sites?search={search}', ('search',), ('search',), 'Sites.Read.All')
_op('site_files', 'microsoft', 'GET', '/sites/{id}/drive/root/children?$top=30', ('id',), ('id',), 'Sites.Read.All')
_op('site_file_write', 'microsoft', 'PUT', '/sites/{id}/drive/root:/{path}:/content', ('id','path','content'), ('id','path','content'), 'Sites.ReadWrite.All')
_op('teams', 'microsoft', 'GET', '/me/joinedTeams', scope='Team.ReadBasic.All')
_op('channels', 'microsoft', 'GET', '/teams/{team}/channels', ('team',), ('team',), 'Channel.ReadBasic.All')
_op('channel_messages', 'microsoft', 'GET', '/teams/{team}/channels/{channel}/messages?$top=20', ('team','channel'), ('team','channel'), 'ChannelMessage.Read.All')
_op('channel_send', 'microsoft', 'POST', '/teams/{team}/channels/{channel}/messages', ('team','channel','body'), ('team','channel','body'), 'ChannelMessage.Send')
_op('channel_update', 'microsoft', 'PATCH', '/teams/{team}/channels/{channel}/messages/{id}', ('team','channel','id','body'), ('team','channel','id','body'), 'ChannelMessage.ReadWrite')
_op('excel_range', 'microsoft', 'GET', "/me/drive/items/{id}/workbook/worksheets/{sheet}/range(address='{address}')", ('id','sheet','address'), ('id','sheet','address'), 'Files.ReadWrite')
_op('excel_range_update', 'microsoft', 'PATCH', "/me/drive/items/{id}/workbook/worksheets/{sheet}/range(address='{address}')", ('id','sheet','address','values'), ('id','sheet','address','values'), 'Files.ReadWrite')


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


def request_json(url, method, headers=None, body=None, *, form=False, opener=None, cancel_event=None):
    # Only callers in this module construct URLs. No URL is accepted by a tool.
    data = body if isinstance(body, bytes) else ((urlencode(body).encode() if form else json.dumps(body).encode()) if body is not None else None)
    req = Request(url, data=data, method=method, headers=headers or {})
    deadline=threading.Event()
    class Cancel:
        def is_set(self): return deadline.is_set() or cancel_event is not None and cancel_event.is_set()
    timer=threading.Timer(20,deadline.set); timer.daemon=True; timer.start()
    try:
        if Cancel().is_set(): raise RuntimeError('Connector request cancelled')
        response_context=(opener(req,timeout=20,cancel_event=Cancel()) if opener else build_opener(_NoRedirect).open(req,timeout=20))
        with response_context as response:
            raw = response.read(2*1024*1024+1)
            if len(raw) > 2*1024*1024: raise ValueError('Connector response exceeds 2 MiB')
            return json.loads(raw) if raw else {'accepted': True}
    except HTTPError as exc:
        # Never reflect response bodies, headers or token endpoint descriptions.
        if form:
            try: error = json.loads(exc.read(8192)).get('error')
            except Exception: error = None
            if error in {'authorization_pending','slow_down','expired_token','authorization_declined'}:
                return {'error': error}
        raise RuntimeError(f'Connector HTTP {exc.code}') from None
    except Exception as exc:
        if isinstance(exc, (ValueError, RuntimeError)): raise
        raise RuntimeError('Connector network request failed') from None
    finally: timer.cancel()


class ConnectorService:
    def __init__(self, store, credential, *, transport=request_json, save_credential=None,response_opener=None):
        self.store, self.credential, self.transport = store, credential, transport
        self.save_credential = save_credential
        self.response_opener=response_opener

    def _request(self,*args,cancel_event=None,**kwargs):
        if self.transport is request_json and self.response_opener is not None:
            kwargs.update(opener=self.response_opener,cancel_event=cancel_event)
        return self.transport(*args,**kwargs)

    def schemas(self):
        result = []
        for name, op in OPERATIONS.items():
            grant = self.store.account(op.account)
            if grant.get('updating') or grant.get('authenticated') is False or grant.get('agent') is not True or grant.get('write' if op.write else 'read') is not True: continue
            if op.account == 'microsoft' and not self._scope_allowed(op.scope,grant): continue
            result.append({'type':'function', 'function':{'name':name,
                'description': ('Create/modify external content; requires user confirmation. ' if op.write else 'Read external content. ')+op.route,
                'parameters': {'type':'object','properties': {f:{'type':'string'} for f in op.fields},
                               'required':list(op.required), 'additionalProperties':False}}})
        return result

    @staticmethod
    def _scope_allowed(required, grant):
        scopes={s.rsplit('/',1)[-1] for s in grant.get('scopes',[]) if isinstance(s,str)}
        implied={'Mail.Read':'Mail.ReadWrite','Calendars.Read':'Calendars.ReadWrite',
                 'Files.Read':'Files.ReadWrite','Sites.Read.All':'Sites.ReadWrite.All'}
        return required in scopes or implied.get(required) in scopes

    def registry(self, *, approve=None, cancel_event=None, agent=True):
        from .tools import ToolRegistry, ToolSpec, ToolResult
        registry=ToolRegistry()
        for name,op in OPERATIONS.items():
            def handler(args,ctx,name=name):
                result=self.invoke(name,args,ctx.policy,approve=approve,cancel_event=cancel_event,agent=agent)
                return ToolResult(True,content=json.dumps(ctx.secret_scope.protect(result['content']),ensure_ascii=False),
                                  meta={'connector':OPERATIONS[name].account})
            registry.register(ToolSpec(name,op.route,handler,read_only=not op.write,tags=('connector',),
                schema={'type':'object','properties':{f:{'type':'string'} for f in op.fields},
                        'required':list(op.required),'additionalProperties':False}))
        return registry

    def invoke(self, name, arguments, policy, *, approve=None, cancel_event=None, agent=True):
        op = OPERATIONS.get(name)
        if op is None: raise ValueError('Unknown connector operation')
        args = copy.deepcopy(arguments)
        if not isinstance(args, dict) or set(args)-set(op.fields): raise ValueError('Unknown connector fields')
        if any(not isinstance(v,str) or len(v)>100000 for v in args.values()): raise ValueError('Invalid connector field')
        if any(not args.get(k) for k in op.required): raise ValueError('Missing required connector field')
        safe = redact(args)
        if safe != args or 'SECRET_REF' in json.dumps(args): raise PermissionError('Secret export is unavailable')
        if op.write and len(json.dumps(safe,ensure_ascii=False,indent=2))>12000:
            raise ValueError('Write payload exceeds the complete confirmation preview limit')
        grant = self.store.account(op.account)
        if grant.get('updating') or grant.get('authenticated') is False or (agent and grant.get('agent') is not True) or grant.get('write' if op.write else 'read') is not True:
            raise PermissionError('Connector capability has not been granted')
        if op.account == 'microsoft' and not self._scope_allowed(op.scope,grant):
            raise PermissionError('Sign in again with the required Microsoft scope')
        decision = policy.evaluate(name, args=args)
        if decision is Decision.DENY or op.write and (policy.mode in {Mode.READ_ONLY,Mode.PLAN,Mode.DONT_ASK} or policy.sandbox is Sandbox.READ_ONLY):
            raise PermissionError('Connector operation denied by Forge Policy')
        if (op.write or decision is Decision.ASK) and (approve is None or not approve(name, safe)):
            self.store.audit(name, {'status':'denied'})
            raise PermissionError('User confirmation required')
        # Confirmation can be open while another session disconnects/revokes.
        if self.store.account(op.account) != grant: raise PermissionError('Connector grant changed; retry')
        if cancel_event is not None and cancel_event.is_set(): raise PermissionError('Request cancelled')
        route = op.route
        if 'address' in args and not re.fullmatch(r'\$?[A-Za-z]{1,3}\$?[1-9][0-9]{0,6}(?::\$?[A-Za-z]{1,3}\$?[1-9][0-9]{0,6})?',args['address']):
            raise ValueError('Excel range must be an A1 address')
        if 'address' in args:
            cells=[]
            for cell in args['address'].split(':'):
                letters,row=re.fullmatch(r'\$?([A-Za-z]+)\$?(\d+)',cell).groups()
                col=0
                for letter in letters.upper(): col=col*26+ord(letter)-64
                cells.append((col,int(row)))
            first,last=cells[0],cells[-1]
            if any(c>16384 or r>1048576 for c,r in cells) or not 0<=last[0]-first[0]<100 or not 0<=last[1]-first[1]<100:
                raise ValueError('Excel range is limited to 100×100 cells')
        route_fields = re.findall(r'\{(\w+)\}', route)
        for field in route_fields:
            value = args[field]
            if field in {'owner','repo','number'} and not re.fullmatch(r'[A-Za-z0-9_.-]+', value):
                raise ValueError('Invalid repository identifier')
            if field != 'search' and any(part in {'','..','.'} for part in value.replace('\\','/').split('/')):
                raise ValueError('Invalid resource path')
            route = route.replace('{'+field+'}', quote(value, safe='/' if field=='path' else ''))
        body = {k:v for k,v in args.items() if k not in route_fields}
        if op.account == 'github':
            if name.endswith('_file') and args.get('ref'): route += '?'+urlencode({'ref':args['ref']})
            if name.endswith('_file_write'):
                import base64
                body['content'] = base64.b64encode(args['content'].encode()).decode()
            if 'state' in body and body['state'] not in {'open','closed'}: raise ValueError('Invalid issue state')
            host = 'https://api.github.com'
            headers = {'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2026-03-10', 'User-Agent':'Forge-Connector'}
        else:
            host = 'https://graph.microsoft.com/v1.0'
            headers = {}
            if name in {'connector_microsoft_mail_send','connector_microsoft_mail_draft_create'}:
                recipients = [v.strip() for v in args['to'].split(',') if v.strip()]
                if not recipients or len(recipients)>20 or any(not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+',v) for v in recipients):
                    raise ValueError('Invalid mail recipients')
                body = {'message':{'subject':args['subject'], 'body':{'contentType':'Text','content':args['body']},
                                   'toRecipients':[{'emailAddress':{'address':v}} for v in recipients]}}
                if name.endswith('_draft_create'): body=body['message']
            elif name.endswith('_mail_draft_update'):
                if 'body' in body: body['body']={'contentType':'Text','content':body['body']}
            elif '_event_' in name:
                if 'body' in body: body['body'] = {'contentType':'Text','content':body['body']}
                zone = body.pop('timezone', None)
                for key in ('start','end'):
                    if key in body:
                        if not zone: raise ValueError('Event time requires timezone')
                        body[key] = {'dateTime':body[key], 'timeZone':zone}
            elif name.endswith(('_channel_send','_channel_update')):
                body = {'body':{'contentType':'text','content':args['body']}}
            elif name.endswith('_excel_range_update'):
                values=json.loads(args['values'])
                if not isinstance(values,list) or not values or len(values)>100 or any(not isinstance(row,list) or len(row)>100 for row in values):
                    raise ValueError('Excel values must be a matrix of at most 100×100 cells')
                if not values[0] or any(len(row)!=len(values[0]) for row in values) or any(not isinstance(cell,(str,int,float,bool,type(None))) for row in values for cell in row):
                    raise ValueError('Excel values must be a rectangular scalar matrix')
                import math
                if any(isinstance(cell,float) and not math.isfinite(cell) for row in values for cell in row):
                    raise ValueError('Excel numbers must be finite')
                if len(values)!=last[1]-first[1]+1 or len(values[0])!=last[0]-first[0]+1:
                    raise ValueError('Excel matrix dimensions must match the selected range')
                if redact(values)!=values or any(isinstance(cell,str) and cell.startswith(('=','+','@')) for row in values for cell in row):
                    raise PermissionError('Secret values and formulas are unavailable in this operation')
                body={'values':values}
            elif name.endswith('_file_write'):
                body = args['content'].encode('utf-8')
        # Small trusted boundary: credentials enter HTTP headers only here.
        token = self._token(op.account,cancel_event=cancel_event)
        if cancel_event is not None and cancel_event.is_set(): raise PermissionError('Request cancelled')
        if self.store.account(op.account)!=grant: raise PermissionError('Connector grant changed; retry')
        scope = SecretScope()
        scope.reference(token)
        headers.update({'Authorization':'Bearer '+token, 'Content-Type':'text/plain; charset=utf-8' if isinstance(body,bytes) else 'application/json'})
        self.store.audit(name, {'status':'started','target':route})
        try:
            result = self._request(host+route, op.method, headers, body if op.write else None,cancel_event=cancel_event)
            if name=='connector_github_file' and isinstance(result,dict) and result.get('encoding')=='base64':
                import base64
                decoded=base64.b64decode(result.get('content',''),validate=False)
                if len(decoded)>512*1024: raise ValueError('Repository file is too large')
                result={'path':result.get('path'),'sha':result.get('sha'),
                        'content':scope.protect_text(decoded.decode('utf-8-sig'))}
            result = scope.protect(redact(result))
            self.store.audit(name, {'status':'completed'})
            return {'ok':True,'content':result}
        except Exception:
            self.store.audit(name, {'status':'failed'})
            raise RuntimeError('Connector request failed; check account permissions and network') from None
        finally: scope.close()

    def _token(self, account, *, cancel_event=None):
        value = self.credential(account)
        if not value: raise PermissionError('Connector is not signed in')
        if account == 'github': return value
        tokens = json.loads(value)
        registration=SecretScope()
        for field in ('access_token','refresh_token'):
            if tokens.get(field): registration.reference(tokens[field])
        registration.close()
        if tokens.get('expires_at',0) <= time.time()+60:
            conf = self.store.account(account)
            fresh = self._request(self._oauth_url(conf,'token'), 'POST',
                {'Content-Type':'application/x-www-form-urlencoded'},
                {'client_id':conf['client_id'],'grant_type':'refresh_token','refresh_token':tokens['refresh_token']}, form=True,cancel_event=cancel_event)
            if 'access_token' not in fresh: raise PermissionError('Microsoft sign-in expired')
            tokens.update(fresh); tokens['expires_at'] = time.time()+float(fresh['expires_in'])
            if self.save_credential is not None:
                self.store.finish_account(account,conf,None,lambda:self.save_credential(account,json.dumps(tokens)))
        return tokens['access_token']

    @staticmethod
    def _oauth_url(conf, endpoint):
        tenant = conf.get('tenant') or 'common'
        if not re.fullmatch(r'[A-Za-z0-9-]+',tenant): raise ValueError('Invalid tenant')
        if not re.fullmatch(r'[A-Za-z0-9-]{1,100}',conf.get('client_id','')): raise ValueError('Set your Entra application Client ID')
        return f'https://login.microsoftonline.com/{tenant}/oauth2/v2.0/{endpoint}'

    def device_login(self, conf, notify, cancel_event):
        scopes = sorted({'offline_access','User.Read', *conf.get('scopes',[])})
        allowed = {op.scope for op in OPERATIONS.values() if op.scope}
        if set(scopes)-allowed-{'offline_access','User.Read'}: raise ValueError('Unknown Microsoft scope')
        self._oauth_url(conf,'devicecode')  # validate before changing existing account
        self.store.set_account('microsoft',dict(conf,updating=True,authenticated=False))
        expected=self.store.account('microsoft')
        code = self._request(self._oauth_url(conf,'devicecode'), 'POST',
                              {'Content-Type':'application/x-www-form-urlencoded'},
                              {'client_id':conf['client_id'],'scope':' '.join(scopes)}, form=True,cancel_event=cancel_event)
        notify(code['user_code'], 'https://microsoft.com/devicelogin')
        interval = max(5, int(code.get('interval',5)))
        expires = time.monotonic()+min(1200,int(code.get('expires_in',900)))
        while time.monotonic()<expires and not cancel_event.wait(interval):
            tokens = self._request(self._oauth_url(conf,'token'), 'POST',
                 {'Content-Type':'application/x-www-form-urlencoded'},
                 {'client_id':conf['client_id'],'grant_type':'urn:ietf:params:oauth:grant-type:device_code',
                  'device_code':code['device_code']}, form=True,cancel_event=cancel_event)
            if tokens.get('error')=='authorization_pending': continue
            if tokens.get('error')=='slow_down': interval += 5; continue
            if 'access_token' not in tokens: raise PermissionError('Microsoft authorization declined or expired')
            if cancel_event.is_set(): raise PermissionError('Sign-in cancelled')
            scope = SecretScope()
            for key in ('access_token','refresh_token'):
                if tokens.get(key): scope.reference(tokens[key])
            scope.close()
            tokens['expires_at'] = time.time()+float(tokens['expires_in'])
            granted={s.rsplit('/',1)[-1] for s in tokens.get('scope','').split()}
            conf = dict(conf, scopes=sorted(granted & allowed),authenticated=True)
            def save_tokens():
                if cancel_event.is_set(): raise PermissionError('Sign-in cancelled')
                self.save_credential('microsoft',json.dumps(tokens))
            self.store.finish_account('microsoft',expected,conf,save_tokens)
            return
        raise PermissionError('Microsoft sign-in cancelled or expired')
