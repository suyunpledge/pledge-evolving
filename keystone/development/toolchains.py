"""Per-language toolchain detection and one-click run/build/test task planning.

The model follows VS Code's split between *detection* (Python interpreter
selection, ``JAVA_HOME``, the C/C++ compiler path, workspace-local
``node_modules/.bin``) and *tasks* (a label, a process command with explicit
arguments, a working directory and the problem matchers that read its output).
Nothing here executes anything: :mod:`keystone.development.tasks` runs a
planned task through the permission policy.

Tasks are always argument lists (``shell=False``). Paths are kept as
:class:`str` from :class:`pathlib.Path`, so spaces and CJK characters reach the
child process verbatim.
"""
import json
import os
from pathlib import Path
import re
import shutil

LANGUAGES = ('python', 'java', 'cpp', 'kotlin', 'javascript', 'typescript')
LABELS = {'python': 'Python', 'java': 'Java', 'cpp': 'C/C++', 'kotlin': 'Kotlin',
          'javascript': 'JavaScript', 'typescript': 'TypeScript'}
EXTENSIONS = {
    '.py': 'python', '.pyw': 'python',
    '.java': 'java',
    '.c': 'cpp', '.cc': 'cpp', '.cpp': 'cpp', '.cxx': 'cpp', '.c++': 'cpp',
    '.kt': 'kotlin', '.kts': 'kotlin',
    '.js': 'javascript', '.mjs': 'javascript', '.cjs': 'javascript',
    '.ts': 'typescript', '.mts': 'typescript', '.cts': 'typescript',
}
GUIDANCE = {
    'python': '未找到 Python 解释器。请安装 Python 3（python.org，或 winget install Python.Python.3.12），'
              '或在项目根目录执行 python -m venv .venv——Keystone 会优先使用项目内的 .venv / venv。',
    'java': '未找到 JDK（javac / java）。请安装 JDK 17 或更新版本（如 winget install Microsoft.OpenJDK.21），'
            '然后设置 JAVA_HOME，或把 JDK 的 bin 目录加入 PATH。Maven / Gradle 项目优先使用项目自带的 mvnw / gradlew。',
    'cpp': '未找到 C/C++ 编译器。可安装 MinGW-w64（g++）、LLVM（clang++）或 Visual Studio Build Tools（cl）；'
           '使用 MSVC 时请从 “Developer PowerShell for VS” 启动 Keystone，cl 才能拿到 INCLUDE / LIB 环境。'
           'CMake 项目还需要 cmake（winget install Kitware.CMake），推荐同时安装 Ninja。',
    'kotlin': '未找到 Kotlin 编译器 kotlinc。可安装 Kotlin 命令行编译器（winget install JetBrains.Kotlin.Compiler，'
              '或从 GitHub Releases 下载并把 bin 加入 PATH），Gradle 项目可直接使用项目自带的 gradlew。'
              '运行编译出的 jar 还需要 JDK。',
    'javascript': '未找到 Node.js。请安装 Node.js LTS（nodejs.org，或 winget install OpenJS.NodeJS.LTS），'
                  '安装后重新打开 Keystone 以刷新 PATH。',
    'typescript': '未找到 TypeScript 工具。请在项目中执行 npm install -D typescript（编译）或 npm install -D tsx（直接运行）；'
                  'Keystone 优先使用项目 node_modules/.bin 中的版本，其次是全局安装的 tsc / tsx。运行编译结果还需要 Node.js。',
}
# Variables a toolchain needs beyond the minimal PATH/SYSTEMROOT set used for
# approved host commands. Values come from this process; names are fixed here.
ENVIRONMENT = {
    'python': ('PYTHONPATH', 'VIRTUAL_ENV'),
    'java': ('JAVA_HOME', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH', 'APPDATA', 'LOCALAPPDATA',
             'GRADLE_USER_HOME', 'MAVEN_HOME', 'M2_HOME', 'PATHEXT', 'PROCESSOR_ARCHITECTURE', 'NUMBER_OF_PROCESSORS'),
    'kotlin': ('JAVA_HOME', 'KOTLIN_HOME', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH', 'APPDATA', 'LOCALAPPDATA',
               'GRADLE_USER_HOME', 'PATHEXT', 'PROCESSOR_ARCHITECTURE', 'NUMBER_OF_PROCESSORS'),
    'cpp': ('INCLUDE', 'LIB', 'LIBPATH', 'VCINSTALLDIR', 'VCToolsInstallDir', 'WindowsSdkDir',
            'WindowsSDKVersion', 'UCRTVersion', 'UniversalCRTSdkDir', 'PATHEXT', 'PROCESSOR_ARCHITECTURE',
            'NUMBER_OF_PROCESSORS', 'USERPROFILE', 'LOCALAPPDATA'),
    'javascript': ('APPDATA', 'LOCALAPPDATA', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH', 'PATHEXT',
                   'NODE_PATH', 'PROGRAMFILES', 'ProgramFiles', 'NUMBER_OF_PROCESSORS'),
}
ENVIRONMENT['typescript'] = ENVIRONMENT['javascript']
BUILD_FOLDER = '.keystone-build'
SKIPPED_DIRS = ('.git', '.venv', 'venv', 'node_modules', 'build', 'dist', BUILD_FOLDER, '__pycache__')
_UNSAFE_BATCH = re.compile(r'["%!^&|<>\r\n]')


class ToolchainMissing(LookupError):
    """A task needs a tool that is not installed; ``guidance`` says how to get it."""

    def __init__(self, language, missing, guidance=None):
        self.language, self.missing = language, tuple(missing)
        self.guidance = guidance or GUIDANCE[language]
        super().__init__(f"{LABELS[language]}: 缺少 {' / '.join(self.missing)}。{self.guidance}")


def language_of(path):
    return EXTENSIONS.get(Path(str(path)).suffix.lower())


def is_batch(program):
    return os.name == 'nt' and Path(program).suffix.lower() in {'.cmd', '.bat'}


def check_argv(argv):
    """Reject arguments ``cmd.exe`` would reinterpret when the program is a batch file.

    CreateProcess runs ``.cmd``/``.bat`` through ``cmd.exe`` and Python's
    quoting does not escape cmd metacharacters, so such arguments could
    execute something other than the approved command.
    """
    if not argv or not all(isinstance(a, str) and a and '\0' not in a for a in argv):
        raise ValueError('任务命令必须是非空参数列表')
    if is_batch(argv[0]) and any(_UNSAFE_BATCH.search(a) for a in argv[1:]):
        raise ValueError('批处理工具的参数不能包含 " % ! ^ & | < > 或换行；请重命名路径后重试。')
    return list(argv)


class Toolchains:
    """Find installed tools the way VS Code's language extensions do.

    ``which``, ``environ`` and ``is_file`` are injectable so tests can describe a
    machine without installing anything.
    """

    def __init__(self, workspace, *, which=shutil.which, environ=None, is_file=None, windows=None):
        self.workspace = Path(workspace)
        self._which = which
        self.environ = dict(os.environ if environ is None else environ)
        self._is_file = is_file or (lambda p: Path(p).is_file())
        self.windows = os.name == 'nt' if windows is None else windows
        self._cache = {}

    # -- lookup ---------------------------------------------------------
    def _exe(self, folder, name, *suffixes):
        suffixes = suffixes or (('.exe', '.cmd', '.bat', '') if self.windows else ('',))
        for suffix in suffixes:
            candidate = Path(folder) / (name + suffix)
            if self._is_file(str(candidate)):
                return str(candidate)
        return None

    def which(self, name):
        if name not in self._cache:
            self._cache[name] = self._which(name)
        return self._cache[name]

    def python(self):
        """Workspace virtual environment first, then ``VIRTUAL_ENV``, then PATH."""
        folders = ['.venv', 'venv', 'env']
        for name in folders:
            found = self._exe(self.workspace / name / ('Scripts' if self.windows else 'bin'),
                              'python', *(('.exe',) if self.windows else ('',)))
            if found:
                return [found], f'项目虚拟环境 {name}'
        venv = self.environ.get('VIRTUAL_ENV')
        if venv:
            found = self._exe(Path(venv) / ('Scripts' if self.windows else 'bin'), 'python',
                              *(('.exe',) if self.windows else ('',)))
            if found:
                return [found], '当前虚拟环境'
        candidates = [('python', 'PATH'), ('python3', 'PATH'), ('py', 'Python 启动器')]
        stub = None
        for name, origin in candidates:
            found = self.which(name)
            if not found:
                continue
            # The Microsoft Store alias opens the Store instead of running code.
            if 'windowsapps' in found.lower() and name != 'py':
                stub = stub or found
                continue
            return ([found, '-3'] if name == 'py' else [found]), origin
        return None, ''

    def java_tool(self, name):
        home = self.environ.get('JAVA_HOME')
        if home:
            found = self._exe(Path(home) / 'bin', name, *(('.exe',) if self.windows else ('',)))
            if found:
                return found
        return self.which(name)

    def wrapper(self, name):
        """Project wrappers (``gradlew``/``mvnw``) beat global installations."""
        suffixes = ('.bat', '.cmd') if self.windows else ('',)
        return self._exe(self.workspace, name, *suffixes)

    def node_bin(self, name):
        local = self._exe(self.workspace / 'node_modules' / '.bin', name,
                          *(('.cmd', '.exe') if self.windows else ('',)))
        return local or self.which(name)

    def cxx(self, c=False):
        names = ('gcc', 'clang', 'cl') if c else ('g++', 'clang++', 'cl')
        for name in names:
            found = self.which(name)
            if found:
                return name, found
        return None, None

    def cmake_generator(self):
        if self.which('ninja'):
            return ['-G', 'Ninja']
        if self.windows and self.which('mingw32-make') and not self.which('cl'):
            return ['-G', 'MinGW Makefiles']
        return []

    # -- detection ------------------------------------------------------
    def detect(self):
        """One row per language: what was found, what is missing, and how to fix it."""
        rows = []
        python, origin = self.python()
        rows.append(self._row('python', [('解释器', python[0] if python else None, origin)], need=['解释器']))
        rows.append(self._row('java', [
            ('javac', self.java_tool('javac'), 'JAVA_HOME' if self.environ.get('JAVA_HOME') else 'PATH'),
            ('java', self.java_tool('java'), ''),
            ('Maven', self.wrapper('mvnw') or self.which('mvn'), ''),
            ('Gradle', self.wrapper('gradlew') or self.which('gradle'), ''),
        ], need=['javac', 'java']))
        name, compiler = self.cxx()
        rows.append(self._row('cpp', [
            ('C++ 编译器', compiler, name or ''), ('C 编译器', self.cxx(c=True)[1], ''),
            ('CMake', self.which('cmake'), ''), ('Ninja', self.which('ninja'), ''),
        ], need=['C++ 编译器']))
        rows.append(self._row('kotlin', [
            ('kotlinc', self.which('kotlinc'), ''), ('java', self.java_tool('java'), ''),
            ('Gradle', self.wrapper('gradlew') or self.which('gradle'), ''),
        ], need=['kotlinc', 'java'], alternative='Gradle'))
        rows.append(self._row('javascript', [('node', self.which('node'), ''), ('npm', self.which('npm'), '')],
                              need=['node']))
        rows.append(self._row('typescript', [
            ('tsc', self.node_bin('tsc'), ''), ('tsx', self.node_bin('tsx'), ''), ('node', self.which('node'), ''),
        ], need=['node'], any_of=['tsc', 'tsx']))
        return rows

    def _row(self, language, tools, *, need, alternative=None, any_of=None):
        found = {label: path for label, path, _origin in tools}
        missing = [label for label in need if not found.get(label)]
        if any_of and not any(found.get(label) for label in any_of):
            missing.append(' 或 '.join(any_of))
        ready = not missing or bool(alternative and found.get(alternative))
        return {'language': language, 'label': LABELS[language], 'ready': ready,
                'tools': [{'name': label, 'path': path or '', 'origin': origin} for label, path, origin in tools],
                'missing': missing, 'guidance': '' if ready else GUIDANCE[language]}


# -- planning -----------------------------------------------------------
def _task(identifier, label, language, action, cwd, steps, matchers, *, scope='file', path='', prepare=()):
    # ``prepare`` lists output folders the runner creates after approval (compilers do not).
    return {'id': identifier, 'label': label, 'language': language, 'action': action, 'scope': scope,
            'cwd': str(cwd), 'steps': [check_argv(s) for s in steps], 'matchers': list(matchers),
            'path': str(path), 'env': list(ENVIRONMENT.get(language, ())), 'prepare': [str(p) for p in prepare]}


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None


def _package_scripts(workspace):
    data = _read_json(Path(workspace) / 'package.json')
    scripts = data.get('scripts') if isinstance(data, dict) else None
    return {k: v for k, v in scripts.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(scripts, dict) else {}


def _pytest_configured(workspace):
    workspace = Path(workspace)
    if any((workspace / name).is_file() for name in ('pytest.ini', 'conftest.py', 'tox.ini')):
        return True
    for name in ('pyproject.toml', 'setup.cfg'):
        try:
            text = (workspace / name).read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        if '[tool.pytest' in text or '[pytest]' in text or '[tool:pytest]' in text:
            return True
    return False


def _out(workspace, *parts):
    return Path(workspace, BUILD_FOLDER, *parts)


def _need(tool, language, label):
    if not tool:
        raise ToolchainMissing(language, [label])
    return tool


def project_markers(workspace):
    workspace = Path(workspace)
    return {
        'pom': (workspace / 'pom.xml').is_file(),
        'gradle': any((workspace / n).is_file() for n in ('build.gradle', 'build.gradle.kts',
                                                            'settings.gradle', 'settings.gradle.kts')),
        'cmake': (workspace / 'CMakeLists.txt').is_file(),
        'package': (workspace / 'package.json').is_file(),
        'tsconfig': (workspace / 'tsconfig.json').is_file(),
        'python': any((workspace / n).is_file() for n in ('pyproject.toml', 'setup.py', 'setup.cfg', 'requirements.txt'))
                  or (workspace / 'tests').is_dir(),
    }


def file_tasks(chains, path):
    """Run/build tasks for one source file (VS Code's "Run Python File", "C/C++: g++ build active file")."""
    path = Path(path)
    language = language_of(path)
    workspace = chains.workspace
    if language is None:
        return []
    stem = path.stem
    rel = path.name
    if language == 'python':
        python, _origin = chains.python()
        python = _need(python, language, '解释器')
        return [_task('file.run', f'运行 {rel}', language, 'run', workspace, [[*python, str(path)]], ['python'], path=path),
                _task('file.build', f'检查语法 {rel}', language, 'build', workspace,
                      [[*python, '-m', 'py_compile', str(path)]], ['python'], path=path)]
    if language == 'java':
        java = _need(chains.java_tool('java'), language, 'java')
        javac = chains.java_tool('javac')
        tasks = [_task('file.run', f'运行 {rel}', language, 'run', path.parent,
                       [[java, '-Dfile.encoding=UTF-8', '-Dstdout.encoding=UTF-8', '-Dstderr.encoding=UTF-8', str(path)]], ['javac', 'java'], path=path)]
        if javac:
            tasks.append(_task('file.build', f'编译 {rel}', language, 'build', path.parent,
                               [[javac, '-J-Dstdout.encoding=UTF-8', '-J-Dstderr.encoding=UTF-8', '-encoding', 'UTF-8',
                                 '-d', str(_out(workspace, 'java')), str(path)]],
                               ['javac'], path=path, prepare=[_out(workspace, 'java')]))
        return tasks
    if language == 'cpp':
        c = path.suffix.lower() == '.c'
        name, compiler = chains.cxx(c=c)
        compiler = _need(compiler, language, 'C 编译器' if c else 'C++ 编译器')
        exe = _out(workspace, 'cpp', stem + ('.exe' if chains.windows else ''))
        if name == 'cl':
            build = [compiler, '/nologo', '/EHsc', '/Zi', '/utf-8', str(path), '/Fe:' + str(exe),
                     '/Fo:' + str(exe.parent) + os.sep, '/Fd:' + str(exe.with_suffix('.pdb'))]
            matchers = ['msvc']
        else:
            build = [compiler, '-g', '-Wall', *(() if c else ('-std=c++17',)), str(path), '-o', str(exe)]
            if chains.windows:
                build.insert(1, '-finput-charset=UTF-8')
            matchers = ['gcc']
        return [_task('file.run', f'编译并运行 {rel}', language, 'run', path.parent, [build, [str(exe)]], matchers,
                      path=path, prepare=[exe.parent]),
                _task('file.build', f'编译 {rel}', language, 'build', path.parent, [build], matchers,
                      path=path, prepare=[exe.parent])]
    if language == 'kotlin':
        kotlinc = _need(chains.which('kotlinc'), language, 'kotlinc')
        if path.suffix.lower() == '.kts':
            return [_task('file.run', f'运行脚本 {rel}', language, 'run', path.parent,
                          [[kotlinc, '-script', str(path)]], ['kotlin'], path=path)]
        java = _need(chains.java_tool('java'), language, 'java')
        jar = _out(workspace, 'kotlin', stem + '.jar')
        build = [kotlinc, str(path), '-include-runtime', '-d', str(jar)]
        return [_task('file.run', f'编译并运行 {rel}', language, 'run', path.parent,
                      [build, [java, '-Dstdout.encoding=UTF-8', '-jar', str(jar)]], ['kotlin', 'java'], path=path,
                      prepare=[jar.parent]),
                _task('file.build', f'编译 {rel}', language, 'build', path.parent, [build], ['kotlin'], path=path,
                      prepare=[jar.parent])]
    if language == 'javascript':
        node = _need(chains.which('node'), language, 'node')
        return [_task('file.run', f'运行 {rel}', language, 'run', workspace, [[node, str(path)]], ['node'], path=path),
                _task('file.build', f'检查语法 {rel}', language, 'build', workspace,
                      [[node, '--check', str(path)]], ['node'], path=path)]
    if language == 'typescript':
        tsx, tsc, node = chains.node_bin('tsx'), chains.node_bin('tsc'), chains.which('node')
        tasks = []
        if tsx:
            tasks.append(_task('file.run', f'运行 {rel}（tsx）', language, 'run', workspace,
                               [[tsx, str(path)]], ['tsc', 'node'], path=path))
        if tsc:
            out = _out(workspace, 'ts')
            compile_step = [tsc, '--pretty', 'false', '--sourceMap', '--target', 'es2020', '--module', 'commonjs',
                            '--outDir', str(out), str(path)]
            if not tsx and node:
                js = out / (stem + '.js')
                tasks.append(_task('file.run', f'编译并运行 {rel}', language, 'run', workspace,
                                   [compile_step, [node, '--enable-source-maps', str(js)]], ['tsc', 'node'], path=path))
            tasks.append(_task('file.build', f'类型检查 {rel}', language, 'build', workspace,
                               [[tsc, '--pretty', 'false', '--noEmit', str(path)]], ['tsc'], path=path))
        if not tasks:
            raise ToolchainMissing(language, ['tsx 或 tsc'])
        if not node and not tsx:
            raise ToolchainMissing(language, ['node'])
        return tasks
    return []


def project_tasks(chains):
    """Workspace-level build/test/run tasks detected from project files (like VS Code's task providers)."""
    workspace = chains.workspace
    marks = project_markers(workspace)
    tasks, problems = [], []

    def add(builder):
        try:
            tasks.extend(builder())
        except ToolchainMissing as missing:
            problems.append(missing)

    if marks['pom']:
        def maven():
            mvn = _need(chains.wrapper('mvnw') or chains.which('mvn'), 'java', 'Maven（mvn 或 mvnw）')
            _need(chains.java_tool('java'), 'java', 'java')
            return [_task('maven.build', 'Maven 编译', 'java', 'build', workspace, [[mvn, '-B', 'compile']],
                          ['maven', 'javac'], scope='project'),
                    _task('maven.test', 'Maven 测试', 'java', 'test', workspace, [[mvn, '-B', 'test']],
                          ['maven', 'javac'], scope='project')]
        add(maven)
    if marks['gradle']:
        def gradle():
            command = _need(chains.wrapper('gradlew') or chains.which('gradle'), 'java', 'Gradle（gradle 或 gradlew）')
            _need(chains.java_tool('java'), 'java', 'java')
            language = 'kotlin' if any(Path(workspace).glob('*.kts')) or any(Path(workspace, 'src').rglob('*.kt')) else 'java'
            base = [command, '--console=plain']
            return [_task('gradle.build', 'Gradle 构建', language, 'build', workspace, [[*base, 'build']],
                          ['kotlin', 'javac'], scope='project'),
                    _task('gradle.test', 'Gradle 测试', language, 'test', workspace, [[*base, 'test']],
                          ['kotlin', 'javac'], scope='project'),
                    _task('gradle.run', 'Gradle 运行', language, 'run', workspace, [[*base, 'run']],
                          ['kotlin', 'javac', 'java'], scope='project')]
        add(gradle)
    if marks['cmake']:
        def cmake():
            command = _need(chains.which('cmake'), 'cpp', 'cmake')
            folder = str(Path(workspace, 'build'))
            configure = [command, '-S', str(workspace), '-B', folder, *chains.cmake_generator()]
            build = [command, '--build', folder]
            ctest = str(Path(command).with_name('ctest' + Path(command).suffix))
            matchers = ['msvc'] if chains.cxx()[0] == 'cl' else ['gcc', 'msvc']
            return [_task('cmake.build', 'CMake 构建', 'cpp', 'build', workspace, [configure, build],
                          matchers, scope='project'),
                    _task('cmake.test', 'CTest 测试', 'cpp', 'test', workspace,
                          [configure, build, [ctest, '--test-dir', folder, '--output-on-failure']],
                          matchers, scope='project')]
        add(cmake)
    if marks['package']:
        def npm():
            scripts = _package_scripts(workspace)
            command = _need(chains.which('npm'), 'javascript', 'npm')
            language = 'typescript' if marks['tsconfig'] else 'javascript'
            matchers = ['tsc', 'node']
            rows = []
            if 'lint' in scripts:
                rows.append(_task('npm.lint', 'npm run lint', language, 'lint', workspace,
                                  [[command, 'run', 'lint']], matchers, scope='project'))
            if 'build' in scripts:
                rows.append(_task('npm.build', 'npm run build', language, 'build', workspace,
                                  [[command, 'run', 'build']], matchers, scope='project'))
            test = scripts.get('test', '')
            if test and 'no test specified' not in test:
                rows.append(_task('npm.test', 'npm test', language, 'test', workspace, [[command, 'test']],
                                  matchers, scope='project'))
            if 'start' in scripts:
                rows.append(_task('npm.start', 'npm start', language, 'run', workspace, [[command, 'start']],
                                  matchers, scope='project'))
            return rows
        add(npm)
    if marks['tsconfig'] and not any(t['id'] == 'npm.build' for t in tasks):
        def tsc():
            command = _need(chains.node_bin('tsc'), 'typescript', 'tsc')
            return [_task('tsc.build', 'tsc -p tsconfig.json', 'typescript', 'build', workspace,
                          [[command, '-p', str(workspace), '--pretty', 'false']], ['tsc'], scope='project')]
        add(tsc)
    if marks['python']:
        def python():
            interpreter, _origin = chains.python()
            interpreter = _need(interpreter, 'python', '解释器')
            if _pytest_configured(workspace):
                test = [*interpreter, '-m', 'pytest', '-q']
            else:
                start = 'tests' if Path(workspace, 'tests').is_dir() else '.'
                test = [*interpreter, '-m', 'unittest', 'discover', '-s', start]
                if start == '.' or (Path(workspace, start) / '__init__.py').is_file():
                    test.extend(['-t', '.'])
                test.append('-v')
            skip = r'[\\/](' + '|'.join(re.escape(d) for d in SKIPPED_DIRS) + r')[\\/]'
            return [_task('python.test', 'Python 测试（pytest）' if 'pytest' in test else 'Python 测试（unittest）',
                          'python', 'test', workspace, [test], ['python', 'pytest'], scope='project'),
                    _task('python.build', 'Python 语法检查（compileall）', 'python', 'build', workspace,
                          [[*interpreter, '-m', 'compileall', '-q', '-x', skip, str(workspace)]],
                          ['python'], scope='project')]
        add(python)
    return tasks, problems


def available(chains, path=None):
    """Everything the UI offers for the active file and the project, plus why anything is missing."""
    tasks, missing = [], []
    if path and language_of(path):
        try:
            tasks.extend(file_tasks(chains, path))
        except ToolchainMissing as error:
            missing.append(error)
    project, problems = project_tasks(chains)
    tasks.extend(project)
    missing.extend(problems)
    notes = list(dict.fromkeys(str(m) for m in missing))
    return tasks, notes


def choose(tasks, action):
    """The default task for an action: file tasks first (F5), project tasks for build/test."""
    order = (('file', 'project') if action == 'run' else ('project', 'file'))
    for scope in order:
        for task in tasks:
            if task['action'] == action and task['scope'] == scope:
                return task
    return None
