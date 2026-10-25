"""Desktop-owned Chromium. All Playwright objects stay on one owner thread.

No installed-browser fallback, remote debugging port, persistent user profile,
or arbitrary JavaScript interface. Browser operations never retry automatically.
"""
from concurrent.futures import Future
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid

from .tools import ToolResult

TIMEOUT_MS = 15000
_ENV_LOCK = threading.Lock()


def browser_directory():
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent / 'browsers'
    return Path(__file__).resolve().parent.parent / 'forge-gui' / 'desktop' / 'browsers'


def verify_bundle(directory, manifest):
    """The frozen EXE pins its sidecar resources, not a writable sidecar manifest."""
    expected = json.loads(Path(manifest).read_text(encoding='utf-8'))
    if not isinstance(expected, dict) or not expected:
        raise RuntimeError('Chromium integrity manifest is missing or invalid')
    directory = Path(directory).resolve()
    roots = {Path(name).parts[0] for name in expected}
    actual = {p.relative_to(directory).as_posix() for root in roots
              for p in (directory / root).rglob('*') if p.is_file()}
    if actual != set(expected):
        raise RuntimeError('Chromium bundle file list changed; reinstall the complete package')
    for name, digest in expected.items():
        path = (directory / name).resolve()
        if not path.is_relative_to(directory) or not path.is_file():
            raise RuntimeError('Chromium resource escapes the distribution directory')
        with path.open('rb') as stream:
            computed = hashlib.file_digest(stream, 'sha256').hexdigest() if hasattr(hashlib, 'file_digest') else None
            if computed is None:
                value = hashlib.sha256()
                for block in iter(lambda: stream.read(1024*1024), b''):
                    value.update(block)
                computed = value.hexdigest()
        if computed != digest:
            raise RuntimeError('Chromium resource integrity check failed: ' + name)


def failure(code, message, state='not_started'):
    return ToolResult(False, error=message, meta={'error_code': code, 'execution_state': state})


def validate_url(value):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 32 for c in value):
        raise ValueError('URL must be a bounded HTTP(S) URL')
    if value == 'about:blank':
        return value
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Only HTTP(S) URLs without embedded credentials are supported')
    return value


class _BrowserActor:
    """Ephemeral cookie/storage partition with serialized, cancellable dispatch.

    Cancellation before dispatch is not_started. Once dispatched we wait for the
    bounded operation and report its actual result, never pretend it was undone.
    """
    def __init__(self, *, headless=False):
        self.id = uuid.uuid4().hex
        self.headless = headless
        self.revoked = threading.Event()
        self._queue = queue.Queue(maxsize=32)
        self._gate = threading.Lock()
        self._thread = None
        self._pages = {}
        self._observations = {}

    def call(self, operation, arguments=None, cancel=None):
        args = json.loads(json.dumps(arguments or {}, allow_nan=False))
        with self._gate:
            if self.revoked.is_set():
                return failure('browser_closed', 'Browser session was closed; open a new session')
            if cancel is not None and cancel.is_set():
                return failure('cancelled', 'Browser action cancelled before dispatch')
            future = Future()
            try:
                self._queue.put_nowait((future, operation, args, cancel))
            except queue.Full:
                return failure('browser_busy', 'Browser queue is full')
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name='Forge-Chromium', daemon=False)
                self._thread.start()
        return future.result()

    def close(self, *, wait=True):
        with self._gate:
            self.revoked.set()
            thread = self._thread
        if wait and thread is not None and thread is not threading.current_thread():
            thread.join(timeout=55)
        return thread is None or not thread.is_alive()

    def _run(self):
        with ExitStack() as resources:
            self._resources = resources
            self._context = None
            try:
                while not self.revoked.is_set():
                    try:
                        future, op, args, cancel = self._queue.get(timeout=.1)
                    except queue.Empty:
                        continue
                    if self.revoked.is_set() or cancel is not None and cancel.is_set():
                        result = failure('cancelled', 'Browser action cancelled before dispatch')
                    else:
                        try:
                            result = self._dispatch(op, args, cancel)
                        except Exception as exc:
                            result = failure('browser_internal_error', str(exc),
                                             'completed' if op == 'observe' else 'unknown')
                            self.revoked.set()
                    future.set_result(result)
            finally:
                # Fence producers before draining, including unexpected actor failure.
                with self._gate:
                    self.revoked.set()
                    while True:
                        try:
                            future, *_ = self._queue.get_nowait()
                            future.set_result(failure('browser_closed', 'Browser session closed before dispatch'))
                        except queue.Empty:
                            break
                self._observations.clear()
        self._context = None
        self._pages.clear()
        self._resources = None

    def _start(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError('Chromium runtime missing: install Forge with [browser] and run desktop/prepare-browser.py') from exc
        directory = browser_directory()
        if not directory.is_dir():
            raise RuntimeError('Bundled Chromium is missing; run desktop/prepare-browser.py or reinstall the complete desktop package')
        if getattr(sys, 'frozen', False):
            verify_bundle(directory, Path(sys._MEIPASS) / 'forge-browser-integrity.json')
        # The driver inherits this variable on startup; restore the caller's env.
        with _ENV_LOCK:
            old = os.environ.get('PLAYWRIGHT_BROWSERS_PATH')
            os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(directory)
            try:
                driver = sync_playwright().start()
            finally:
                if old is None:
                    os.environ.pop('PLAYWRIGHT_BROWSERS_PATH', None)
                else:
                    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = old
        self._resources.callback(driver.stop)
        if not Path(driver.chromium.executable_path).is_file():
            raise RuntimeError('Bundled Chromium revision does not match the installed Playwright version')
        browser = driver.chromium.launch(headless=self.headless, chromium_sandbox=True,
                                         executable_path=driver.chromium.executable_path, timeout=30000)
        self._resources.callback(browser.close)
        self._context = browser.new_context(accept_downloads=False, service_workers='block',
                                            no_viewport=not self.headless, permissions=[])
        self._context.set_default_timeout(TIMEOUT_MS)
        self._context.set_default_navigation_timeout(TIMEOUT_MS)
        # Do not let page links turn this host into a local-file reader.
        self._context.route('**/*', lambda route: route.continue_() if
            urlsplit(route.request.url).scheme in ('http', 'https') else route.abort())
        self._context.on('page', self._track_page)
        browser.on('disconnected', lambda _: self.revoked.set())

    def _track_page(self, page):
        if len(self._pages) >= 12:
            page.close()
            return
        key = uuid.uuid4().hex
        self._pages[key] = page
        page.on('dialog', lambda dialog: dialog.dismiss())
        page.on('download', lambda download: download.cancel())
        page.on('close', lambda _: (self._pages.pop(key, None), self._forget(key)))
        page.on('framenavigated', lambda frame: self._forget(key)
                if frame == page.main_frame else None)

    def _forget(self, page_id):
        previous = self._observations.pop(page_id, None)
        if previous:
            for handle, _ in previous['refs'].values():
                try:
                    handle.dispose()
                except Exception:
                    pass  # Navigation may already have destroyed the document.

    def _dispatch(self, op, args, cancel):
        allowed = {'open': {'url', 'page_id'}, 'observe': {'page_id'},
                   'click': {'page_id', 'observation_id', 'ref'},
                   'fill': {'page_id', 'observation_id', 'ref', 'text'}}
        if op not in allowed or set(args) - allowed[op]:
            return failure('invalid_arguments', 'Unknown browser operation or argument')
        try:
            if op == 'open':
                validate_url(args.get('url', 'about:blank'))
            if op == 'fill' and (not isinstance(args.get('text'), str) or len(args['text']) > 8000):
                raise ValueError('Input text must be at most 8000 characters')
            if self._context is None:
                if op != 'open':
                    return failure('browser_not_open', 'Open the browser before observing it')
                self._start()
            page_id = args.get('page_id')
            if page_id is None:
                if op in ('click', 'fill'):
                    raise ValueError('page_id is required')
                page_id = next(iter(self._pages), None)
            if page_id is None and op == 'open':
                self._context.new_page()
                page_id = next(iter(self._pages))
            page = self._pages.get(page_id)
            if page is None or page.is_closed():
                return failure('page_closed', 'Page no longer exists; observe the browser again')
            if op != 'open':
                validate_url(page.url)
            if self.revoked.is_set() or cancel is not None and cancel.is_set():
                return failure('cancelled', 'Browser action cancelled before dispatch')
            if op in ('click', 'fill'):
                observation = self._observations.get(page_id)
                if (not observation or observation['id'] != args.get('observation_id') or
                        time.monotonic() - observation['time'] > 60 or observation['url'] != page.url):
                    return failure('stale_observation', 'Page changed or observation expired; observe again')
                entry = observation['refs'].get(args.get('ref'))
                if entry is None or not entry[0].evaluate('(e) => e.isConnected') or self._describe(entry[0]) != entry[1]:
                    return failure('stale_observation', 'Target changed; observe again')
                element = entry[0]
                if op == 'fill' and entry[1]['type'] in ('password', 'file', 'hidden'):
                    return failure('sensitive_input', 'Enter credentials and select files manually in the browser')
        except (ValueError, TypeError, KeyError) as exc:
            return failure('invalid_arguments', str(exc))
        except Exception as exc:
            if self._context is None:
                self.revoked.set()
            return failure('browser_unavailable', str(exc))
        try:
            if op == 'open':
                page.goto(args.get('url', 'about:blank'), wait_until='domcontentloaded')
                if not self.headless:
                    page.bring_to_front()
            elif op in ('click', 'fill'):
                self._observations.pop(page_id, None)
                try:
                    if op == 'click':
                        element.click(timeout=TIMEOUT_MS)
                    else:
                        element.fill(args['text'], timeout=TIMEOUT_MS)
                finally:
                    for handle, _ in observation['refs'].values():
                        try:
                            handle.dispose()
                        except Exception:
                            pass
            if op == 'observe':
                value = self._observe(page_id, page)
            else:
                value = {'page_id': page_id, 'url': page.url, 'operation': op}
            return ToolResult(True, json.dumps(value, ensure_ascii=False), meta={
                'execution_state': 'completed', 'session_id': self.id,
                'cancellation_requested': bool(cancel is not None and cancel.is_set()),
                'evidence': 'untrusted_browser_content'})
        except Exception as exc:
            self._forget(page_id)
            return failure('browser_operation_failed', str(exc), 'completed' if op == 'observe' else 'unknown')

    @staticmethod
    def _describe(element):
        return element.evaluate('''e => ({tag:e.tagName, type:e.type || '',
            role:(e.getAttribute('role') || '').slice(0,80),
            label:(e.getAttribute('aria-label') || e.innerText || e.getAttribute('placeholder') || '').slice(0,200),
            href:(e.getAttribute('href') || '').slice(0,2048)})''')

    def _observe(self, page_id, page):
        self._forget(page_id)
        refs, elements = {}, []
        deadline = time.monotonic() + 5
        locator = page.locator('a, button, input, textarea, select, [role="button"], [contenteditable="true"]')
        for index in range(min(locator.count(), 120)):
            if time.monotonic() >= deadline:
                break
            element = locator.nth(index).element_handle(timeout=1000)
            if element is None:
                continue
            if not element.is_visible():
                element.dispose()
                continue
            ref = str(len(refs) + 1)
            data = self._describe(element)
            refs[ref] = (element, data)
            elements.append({'ref': ref, **data})
        observation_id = uuid.uuid4().hex
        self._observations[page_id] = {'id': observation_id, 'time': time.monotonic(), 'url': page.url, 'refs': refs}
        return {'page_id': page_id, 'observation_id': observation_id, 'url': page.url,
                'title': page.title()[:500],
                'text': page.locator('body').evaluate('(e) => e.innerText.slice(0,16000)', timeout=3000),
                'elements': elements, 'tabs': [{'page_id': key, 'url': p.url} for key, p in self._pages.items()]}


class BrowserSession(_BrowserActor):
    """Supervise a private helper so a hung renderer cannot hang the GUI forever."""
    deadline = 45

    def _dispatch(self, op, args, cancel):
        proc = getattr(self, '_process', None)
        if proc is None:
            try:
                command = ([sys.executable, '--browser-worker'] if getattr(sys, 'frozen', False) else
                           [sys.executable, '-u', '-m', 'forge.browser', '--browser-worker'])
                if self.headless:
                    command.append('--headless')
                environment = dict(os.environ)
                if not getattr(sys, 'frozen', False):
                    environment['PYTHONPATH'] = str(Path(__file__).resolve().parent.parent) + os.pathsep + environment.get('PYTHONPATH', '')
                proc = self._process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, encoding='utf-8', env=environment,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                    start_new_session=os.name != 'nt')
                self._resources.callback(self._stop_process)
                from .browser_process import BrowserLifetime
                self._lifetime = BrowserLifetime(proc)
            except Exception as exc:
                self.revoked.set()
                return failure('browser_unavailable', str(exc))
        if self.revoked.is_set() or cancel is not None and cancel.is_set():
            return failure('cancelled', 'Browser action cancelled before dispatch')
        if proc.poll() is not None:
            self.revoked.set()
            return failure('browser_closed', 'Browser worker exited; open a new session')
        request_id = uuid.uuid4().hex
        responses = queue.Queue(maxsize=1)
        def receive():
            try:
                responses.put(proc.stdout.readline())
            except Exception:
                responses.put('')
        try:
            proc.stdin.write(json.dumps({'id': request_id, 'operation': op, 'args': args}) + '\n')
            proc.stdin.flush()
            threading.Thread(target=receive, name='Forge-Browser-Receipt', daemon=True).start()
            packet = json.loads(responses.get(timeout=self.deadline))
            value = packet['result']
            if (packet['id'] != request_id or type(value['ok']) is not bool or
                    not isinstance(value.get('meta'), dict) or not isinstance(value.get('content'), str)
                    or not isinstance(value.get('error'), str)):
                raise ValueError('Invalid browser worker receipt')
            if packet.get('closed'):
                self.revoked.set()
            return ToolResult(value['ok'], value['content'], value['error'], {**value['meta'],
                'session_id': self.id, 'cancellation_requested': bool(cancel is not None and cancel.is_set())})
        except Exception as exc:
            self.revoked.set()
            self._stop_process(force=True)
            return failure('browser_worker_lost', 'Browser worker timed out or exited: ' + type(exc).__name__,
                           'completed' if op == 'observe' else 'unknown')

    def _stop_process(self, force=False):
        proc = getattr(self, '_process', None)
        if proc is None:
            return
        try:
            if proc.poll() is None and not force:
                try:
                    proc.stdin.write('{"shutdown":true}\n'); proc.stdin.flush()
                    proc.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            lifetime = getattr(self, '_lifetime', None)
            if lifetime is not None:
                lifetime.close()
            if proc.poll() is None:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=3)
        finally:
            for stream in (proc.stdin, proc.stdout):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass


def worker_main():
    from .browser_process import worker_streams
    incoming, outgoing = worker_streams()
    session = _BrowserActor(headless='--headless' in sys.argv)
    try:
        for line in incoming:
            request = json.loads(line)
            if request.get('shutdown'):
                break
            result = session.call(request['operation'], request['args'])
            print(json.dumps({'id': request['id'], 'result': result.as_dict(),
                              'closed': session.revoked.is_set()}, ensure_ascii=False), file=outgoing, flush=True)
    finally:
        session.close()
    return 0


class BrowserManager:
    def __init__(self):
        self._sessions = {}
        self._lock = threading.Lock()
        self._closed = False

    def session(self, workspace, mode, conversation):
        key = (os.path.normcase(str(Path(workspace).resolve())), str(mode), str(conversation))
        with self._lock:
            if self._closed:
                raise RuntimeError('Browser manager is closed')
            current = self._sessions.get(key)
            if current is None or current.revoked.is_set():
                if current is not None and not current.close(wait=False):
                    raise RuntimeError('Previous browser is still closing')
                self._sessions = {k: s for k, s in self._sessions.items()
                                  if not s.revoked.is_set() or not s.close(wait=False)}
                if len(self._sessions) >= 4:
                    raise RuntimeError('Four browser sessions are already open; close one before opening another')
                current = self._sessions[key] = BrowserSession()
            return current

    def close(self):
        with self._lock:
            self._closed = True
            sessions = list(self._sessions.values())
        for session in sessions:
            session.close(wait=False)
        return all([session.close() for session in sessions])


def smoke_report(destination):
    """Also works in the frozen GUI, without network, Tk, or a model account."""
    session = BrowserSession(headless=True)
    try:
        result = session.call('open', {'url': 'about:blank'})
        report = result.as_dict()
        report['browser_directory'] = str(browser_directory())
    finally:
        closed = session.close()
    report['closed'] = closed
    Path(destination).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if result.ok and closed else 1


if __name__ == '__main__':
    raise SystemExit(worker_main())
