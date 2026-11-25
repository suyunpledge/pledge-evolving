"""Background, read-only scheduled jobs; independent of UI page lifetime."""
from pathlib import Path
import os
import threading
import time

from config_model import load_user_layer
from revon.secrets import SecretScope, redact
from revon.policy import Policy, Mode, Sandbox
from phase_client import PhaseClient
import secret_store


class SchedulerService:
    def __init__(self, host):
        self.host, self.stop = host, threading.Event()
        self.lock = threading.Lock()
        self.active_job = None
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.run, name='revon-Scheduler', daemon=True)
        self.thread.start()

    def cancel_job(self, key=None):
        with self.lock:
            if self.active_job and (key is None or key == self.active_job[0]):
                self.active_job[1].set()

    def close(self):
        self.stop.set()
        self.cancel_job()
        if self.thread:
            self.thread.join(timeout=2)

    def run(self):
        store = self.host.pages.store
        while not self.stop.wait(2):
            try:
                jobs = store.claim_due()
                if not jobs:
                    continue
                job = jobs[0]
                cancel = threading.Event()
                with self.lock:
                    self.active_job = (job['id'], cancel)
                    if self.stop.is_set():
                        cancel.set()
                timer = threading.Timer(180, cancel.set)
                timer.daemon = True
                timer.start()
                scope = SecretScope()
                try:
                    try:
                        status, result = self.execute(job, load_user_layer(self.host.home), scope, cancel)
                    except Exception as exc:
                        status, result = 'failed', str(redact(str(exc)))
                    if cancel.is_set() or self.stop.is_set():
                        status = 'cancelled'
                    store.finish_job(job['id'], status, result, run_id=job['run_id'])
                finally:
                    scope.close()
                    timer.cancel()
                    with self.lock:
                        self.active_job = None
                    if self.host.page == 'schedules':
                        self.host.pages.publish('schedules')
            except Exception as exc:
                self.host.emit('error', message=str(redact(str(exc))))

    def execute(self, job, rows, scope, cancel):
        from revon.config import Config
        from revon.loop import build_agent, LoopLimits
        def check():
            if self.stop.is_set() or cancel.is_set():
                raise RuntimeError('Scheduled task cancelled')
        check()
        workspace = Path(job['workspace'])
        if str(workspace.resolve()) != job['workspace'] or not workspace.is_dir():
            raise PermissionError('Scheduled workspace changed or was removed')
        client = PhaseClient(rows, job['model'], {**os.environ, **secret_store.env_for()}, scope, cancel)
        agent = build_agent(home=self.host.home, workspace=workspace, config=Config(),
            router=client.router, policy=Policy(mode=Mode.READ_ONLY, sandbox=Sandbox.READ_ONLY, workspace=workspace, non_interactive=True),
            limits=LoopLimits(max_steps=6, spawn_budget=0), mount_contrib=False,
            expose=('read_file', 'list_dir', 'grep', 'edit_config'),
            session_path=self.host.home / 'sessions' / ('scheduled-' + job['id'] + '-' + str(time.time_ns()) + '.jsonl'))
        complete = client.router.complete
        def bounded(messages, **options):
            check()
            options['max_tokens'] = min(int(options.get('max_tokens') or 2048), 2048)
            return complete(messages, **options)
        client.router.complete = bounded
        agent.cancellation_event = cancel
        try:
            report = agent.run(job['prompt'])
            return ('done' if report.stopped == 'final' else 'failed'), redact(report.text)
        finally:
            agent.secret_scope.close()
