"""Code mode run/build/test tasks for the Qt service process.

Detection and planning come from :mod:`revon.development.toolchains`;
execution goes through :func:`revon.development.tasks.execute`, which checks
the desktop permission policy for every step and asks the human through the
same approval dialog as the Code terminal. Output streams into the dock's
"输出" tab; parsed problems are published for the "问题" tab.
"""
import threading
import time

from revon.development import tasks as task_runner
from revon.development import toolchains
from revon.secrets import redact


class RunService:
    def __init__(self, host):
        self.host = host
        self.problems, self.last, self.running = [], None, ''
        self.cancel = threading.Event()
        self._detected = None
        self._tasks, self._notes, self._for = [], [], None

    def chains(self):
        return toolchains.Toolchains(self.host.workspace)

    def publish(self, *, detect=False, replan=False):
        path = self.host.pages.active_document or ''
        if replan or detect or self._for != path:
            chains = self.chains()
            if detect or self._detected is None:
                self._detected = chains.detect()
            self._tasks, self._notes = toolchains.available(chains, path or None)
            self._for = path
        self.host.emit('run_state', state=redact({
            'toolchains': self._detected,
            'tasks': [{k: t[k] for k in ('id', 'label', 'language', 'action', 'scope')} for t in self._tasks],
            'notes': self._notes, 'problems': self.problems, 'running': self.running,
            'last': self.last, 'file': path, 'language': toolchains.language_of(path) or '',
            'canRun': self.host.profile != 'sandbox' and not self.host.demo}))

    def command(self, row):
        action, value = row.get('action'), row.get('value', '')
        if action == 'refresh':
            self.publish(detect=True)
        elif action == 'cancel':
            self.cancel.set()
        elif action == 'clearProblems':
            self.problems = []
            self.publish()
        elif action in {'run', 'build', 'test', 'task'}:
            self.start(action, value)

    def start(self, action, value):
        # Plan against the current file and disk state, never a stale UI list.
        self.publish(replan=True)
        if self.host.demo:
            raise PermissionError('预览模式不执行项目代码。')
        if self.host.profile == 'sandbox':
            raise PermissionError('沙箱运行不执行项目代码。请切换到“手动批准”或“完全访问”后再运行、构建或测试。')
        if self.host.pages.unsaved:
            raise ValueError('请先保存编辑器修改，再运行任务。')
        if action == 'task':
            task = next((t for t in self._tasks if t['id'] == value), None)
        else:
            task = toolchains.choose(self._tasks, action)
        if task is None:
            names = {'run': '运行', 'build': '构建', 'test': '测试'}
            reason = '\n'.join(self._notes) or (
                f"当前文件和项目没有可{names.get(action, '执行')}的任务。打开 Python / Java / C++ / Kotlin / "
                "JavaScript / TypeScript 文件，或在项目根目录放置 pyproject.toml、pom.xml、build.gradle、"
                "CMakeLists.txt、package.json 等项目文件。")
            self.host.terminal.publish(f"\n[{names.get(action, '任务')}] {reason}\n", reveal=True)
            raise LookupError(reason)
        self.host.work(lambda: self.execute(task), cancel_event=self.cancel)

    def execute(self, task):
        self.running = task['label']
        result = None
        try:
            self.publish()
            result = task_runner.execute(
                task, workspace=self.host.workspace, policy=self.host.policy(),
                approve=self.host.approve, cancelled=self.cancel,
                on_output=lambda text: self.host.terminal.publish(str(redact(text)), reveal=True))
            self.problems = result['problems']
            self.last = {'label': task['label'], 'status': result['status'], 'exitCode': result['exit_code'],
                         'counts': result['counts'], 'elapsed': result['elapsed'], 'stamp': time.time()}
        except PermissionError as error:
            self.host.terminal.publish(f'\n[未运行] {error}\n', reveal=True)
            raise
        finally:
            self.running = ''
            self.publish(replan=bool(result and result['steps']))
        self.host.pages.refresh_documents()
