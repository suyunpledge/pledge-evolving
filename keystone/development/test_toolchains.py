"""Toolchain detection, task planning, problem matchers and policy-gated execution.

Fake machines describe installed tools; tests that need a real compiler skip
when it is absent, so the suite runs anywhere.
"""
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest

from keystone.policy import Decision
from keystone.development import problems, tasks, toolchains
from keystone.development.toolchains import Toolchains, ToolchainMissing


def fake_machine(workspace, tools=(), files=(), environ=None, windows=True):
    """``tools`` maps PATH names to fake locations; ``files`` are extra existing files."""
    tools = dict(tools)
    existing = {str(Path(f)) for f in files}
    return Toolchains(workspace, which=lambda name: tools.get(name), environ=environ or {},
                      is_file=lambda p: str(Path(p)) in existing, windows=windows)


class Policy:
    def __init__(self, decision=Decision.ALLOW, interactive=True):
        self.decision, self.interactive, self.commands = decision, interactive, []

    def evaluate(self, tool, *, args=None, touching=(), effect=None):
        self.commands.append((tool, args['command'], effect))
        return self.decision

    def resolve_ask(self, decision):
        return Decision.DENY if decision is Decision.ASK and not self.interactive else decision


class Workspace(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='keystone 工具链 ')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve() / '我的 项目'
        self.root.mkdir()

    def write(self, name, text=''):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path


class DetectionTests(Workspace):
    def test_empty_machine_reports_every_language_with_guidance(self):
        rows = {r['language']: r for r in fake_machine(self.root).detect()}
        self.assertEqual(set(rows), set(toolchains.LANGUAGES))
        for row in rows.values():
            self.assertFalse(row['ready'], row)
            self.assertTrue(row['missing'])
            self.assertIn('安装', row['guidance'])
        self.assertIn('JAVA_HOME', rows['java']['guidance'])
        self.assertIn('Developer PowerShell', rows['cpp']['guidance'])
        self.assertIn('node_modules/.bin', rows['typescript']['guidance'])

    def test_workspace_venv_beats_path_and_store_alias_is_skipped(self):
        venv = self.root / '.venv' / 'Scripts' / 'python.exe'
        machine = fake_machine(self.root, {'python': r'C:\Users\x\AppData\Local\Microsoft\WindowsApps\python.exe'},
                               files=[venv])
        self.assertEqual(machine.python(), ([str(venv)], '项目虚拟环境 .venv'))
        store = fake_machine(self.root, {'python': r'C:\Users\x\AppData\Local\Microsoft\WindowsApps\python.exe',
                                         'py': r'C:\Windows\py.exe'})
        self.assertEqual(store.python()[0], [r'C:\Windows\py.exe', '-3'])

    def test_java_home_wrappers_and_local_node_bins_are_preferred(self):
        home = Path(r'C:\Program Files\Java\jdk-21')
        files = [home / 'bin' / 'javac.exe', home / 'bin' / 'java.exe', self.root / 'gradlew.bat',
                 self.root / 'node_modules' / '.bin' / 'tsc.cmd']
        machine = fake_machine(self.root, {'javac': r'C:\other\javac.exe', 'node': r'C:\node\node.exe',
                                           'tsc': r'C:\global\tsc.cmd'},
                               files=files, environ={'JAVA_HOME': str(home)})
        self.assertEqual(machine.java_tool('javac'), str(home / 'bin' / 'javac.exe'))
        self.assertEqual(machine.wrapper('gradlew'), str(self.root / 'gradlew.bat'))
        self.assertEqual(machine.node_bin('tsc'), str(self.root / 'node_modules' / '.bin' / 'tsc.cmd'))
        rows = {r['language']: r for r in machine.detect()}
        self.assertTrue(rows['java']['ready'])
        self.assertTrue(rows['typescript']['ready'])
        self.assertTrue(rows['kotlin']['ready'], 'a Gradle wrapper can build Kotlin without kotlinc')
        self.assertFalse(rows['cpp']['ready'])

    def test_real_detection_runs_on_this_machine(self):
        rows = Toolchains(self.root).detect()
        self.assertEqual([r['language'] for r in rows], list(toolchains.LANGUAGES))


class PlanningTests(Workspace):
    machine_tools = {'g++': r'C:\mingw\bin\g++.exe', 'gcc': r'C:\mingw\bin\gcc.exe', 'cmake': r'C:\cmake\bin\cmake.exe',
                     'ninja': r'C:\ninja\ninja.exe', 'javac': r'C:\jdk\bin\javac.exe', 'java': r'C:\jdk\bin\java.exe',
                     'kotlinc': r'C:\kotlin\bin\kotlinc.bat', 'node': r'C:\node\node.exe', 'npm': r'C:\node\npm.cmd',
                     'tsc': r'C:\node\tsc.cmd', 'python': r'C:\Python312\python.exe', 'mvn': r'C:\maven\mvn.cmd'}

    def machine(self, **extra):
        return fake_machine(self.root, {**self.machine_tools, **extra})

    def test_single_file_tasks_for_all_six_languages(self):
        machine = self.machine()
        expected = {
            'main.py': ([r'C:\Python312\python.exe'], 1),
            'App.java': ([r'C:\jdk\bin\java.exe'], 1),
            'util.cpp': ([r'C:\mingw\bin\g++.exe'], 2),
            'legacy.c': ([r'C:\mingw\bin\gcc.exe'], 2),
            'Main.kt': ([r'C:\kotlin\bin\kotlinc.bat'], 2),
            'index.js': ([r'C:\node\node.exe'], 1),
            'app.ts': ([r'C:\node\tsc.cmd'], 2),
        }
        for name, (head, count) in expected.items():
            with self.subTest(name):
                path = self.write('源码 dir/' + name)
                run = toolchains.choose(toolchains.file_tasks(machine, path), 'run')
                self.assertEqual(run['steps'][0][:1], head)
                self.assertEqual(len(run['steps']), count)
                self.assertIn(str(path), run['steps'][0])
                self.assertTrue(run['matchers'])
        cpp = toolchains.choose(toolchains.file_tasks(machine, self.root / '源码 dir/util.cpp'), 'run')
        exe = Path(cpp['steps'][1][0])
        self.assertEqual(exe.parent, self.root / toolchains.BUILD_FOLDER / 'cpp')
        self.assertIn(str(exe.parent), cpp['prepare'])

    def test_msvc_and_tsx_variants(self):
        machine = fake_machine(self.root, {'cl': r'C:\VS\cl.exe', 'node': r'C:\node\node.exe'},
                               files=[self.root / 'node_modules' / '.bin' / 'tsx.cmd'])
        cpp = toolchains.choose(toolchains.file_tasks(machine, self.write('a.cpp')), 'build')
        self.assertEqual(cpp['matchers'], ['msvc'])
        self.assertIn('/utf-8', cpp['steps'][0])
        ts = toolchains.choose(toolchains.file_tasks(machine, self.write('a.ts')), 'run')
        self.assertTrue(ts['steps'][0][0].endswith('tsx.cmd'))

    def test_missing_compiler_raises_guidance(self):
        machine = fake_machine(self.root, {'node': r'C:\node\node.exe'})
        for name, language in (('a.cpp', 'cpp'), ('A.java', 'java'), ('M.kt', 'kotlin'), ('m.py', 'python')):
            with self.subTest(name), self.assertRaises(ToolchainMissing) as caught:
                toolchains.file_tasks(machine, self.write(name))
            self.assertEqual(caught.exception.language, language)
            self.assertIn(toolchains.GUIDANCE[language][:10], str(caught.exception))
        tasks_, notes = toolchains.available(machine, self.root / 'a.cpp')
        self.assertEqual(tasks_, [])
        self.assertIn('C/C++', notes[0])

    def test_project_tasks_follow_project_files(self):
        self.write('pom.xml', '<project/>')
        self.write('mvnw.cmd')
        self.write('CMakeLists.txt', 'project(x)')
        self.write('package.json', '{"scripts": {"build": "tsc -p .", "test": "node --test", "start": "node ."}}')
        self.write('tsconfig.json', '{}')
        self.write('pyproject.toml', '[tool.pytest.ini_options]\n')
        machine = fake_machine(self.root, self.machine_tools, files=[self.root / 'mvnw.cmd'])
        found, notes = toolchains.project_tasks(machine)
        ids = {t['id']: t for t in found}
        self.assertEqual(notes, [])
        self.assertEqual(ids['maven.build']['steps'], [[str(self.root / 'mvnw.cmd'), '-B', 'compile']])
        self.assertEqual(ids['cmake.build']['steps'][0][-2:], ['-G', 'Ninja'])
        self.assertTrue(ids['cmake.test']['steps'][-1][0].endswith('ctest.exe'))
        self.assertEqual(ids['npm.test']['steps'], [[r'C:\node\npm.cmd', 'test']])
        self.assertNotIn('tsc.build', ids, 'npm run build already covers the TypeScript build')
        self.assertEqual(ids['python.test']['steps'][0][1:], ['-m', 'pytest', '-q'])
        self.assertEqual(toolchains.choose(found, 'build')['scope'], 'project')

    def test_default_npm_test_stub_and_unittest_fallback(self):
        self.write('package.json', '{"scripts": {"test": "echo \\"Error: no test specified\\" && exit 1"}}')
        (self.root / 'tests').mkdir()
        found, _ = toolchains.project_tasks(self.machine())
        ids = {t['id']: t for t in found}
        self.assertNotIn('npm.test', ids)
        self.assertEqual(ids['python.test']['steps'][0][1:5], ['-m', 'unittest', 'discover', '-s'])

    def test_batch_programs_reject_cmd_metacharacters(self):
        toolchains.check_argv([r'C:\node\npm.cmd', 'run', 'build'])
        if toolchains.os.name == 'nt':
            with self.assertRaises(ValueError):
                toolchains.check_argv([r'C:\node\npm.cmd', 'run', 'x&calc'])
        toolchains.check_argv([r'C:\node\node.exe', r'C:\目录 A\a&b.js'])


class MatcherTests(unittest.TestCase):
    root = Path(tempfile.gettempdir()).resolve() / '工作 区'

    def parse(self, text, *names, cwd=None):
        return problems.parse(text, names, cwd=cwd or self.root, workspace=self.root)

    def one(self, text, *names):
        rows = self.parse(text, *names)
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def test_gcc_clang_with_cjk_paths_and_relative_files(self):
        row = self.one(f'{self.root}\\src\\主 程序.cpp:12:5: error: \'x\' was not declared in this scope', 'gcc')
        self.assertEqual((row['line'], row['column'], row['severity']), (12, 5, 'error'))
        self.assertEqual(row['relative'], 'src/主 程序.cpp')
        self.assertTrue(row['inWorkspace'])
        warning = self.one('lib/a.h:3: warning: unused', 'gcc')
        self.assertEqual((warning['path'], warning['column'], warning['severity']),
                         (str(self.root / 'lib' / 'a.h'), 1, 'warning'))
        self.assertEqual(self.parse('collect2.exe: error: ld returned 1 exit status', 'gcc'), [])

    def test_msvc(self):
        row = self.one(r'  3>C:\proj\main.cpp(7,10): error C2065: "y": undeclared identifier', 'msvc')
        self.assertEqual((row['path'], row['line'], row['column'], row['code']),
                         (str(Path(r'C:\proj\main.cpp')), 7, 10, 'C2065'))

    def test_javac_english_chinese_and_maven(self):
        self.assertEqual(self.one("Main.java:3: error: ';' expected", 'javac')['line'], 3)
        chinese = self.one(r"C:\代码\Main.java:4: 错误: 找不到符号", 'javac')
        self.assertEqual((chinese['path'], chinese['severity']), (str(Path(r'C:\代码\Main.java')), 'error'))
        maven = self.one('[ERROR] /home/u/app/src/main/java/App.java:[12,8] cannot find symbol', 'maven')
        self.assertEqual((maven['line'], maven['column']), (12, 8))

    def test_kotlin_uri_and_legacy_formats(self):
        uri = self.one('e: file:///C:/Users/%E5%8C%A1/Main.kt:3:5 Unresolved reference: foo', 'kotlin')
        self.assertEqual(uri['path'], str(Path('C:/Users/匡/Main.kt')))
        self.assertEqual((uri['line'], uri['column'], uri['message']), (3, 5, 'Unresolved reference: foo'))
        old = self.one('w: src/Main.kt: (7, 2): Variable is never used', 'kotlin')
        self.assertEqual((old['line'], old['severity']), (7, 'warning'))

    def test_tsc_plain_and_pretty(self):
        plain = self.one("src/app.ts(4,7): error TS2322: Type 'number' is not assignable to type 'string'.", 'tsc')
        self.assertEqual((plain['line'], plain['column'], plain['code']), (4, 7, 'TS2322'))
        pretty = self.one('\x1b[96msrc/app.ts\x1b[0m:\x1b[93m9\x1b[0m:\x1b[93m1\x1b[0m - \x1b[91merror\x1b[0m'
                          '\x1b[90m TS1005: \x1b[0m\';\' expected.', 'tsc')
        self.assertEqual((pretty['line'], pretty['code']), (9, 'TS1005'))

    def test_python_traceback_prefers_workspace_frame(self):
        lib = Path(sys.base_prefix) / 'Lib' / 'json' / 'decoder.py'
        trace = '\n'.join([
            'Traceback (most recent call last):',
            f'  File "{self.root}\\app\\主.py", line 8, in <module>',
            '    main()',
            f'  File "{lib}", line 355, in raw_decode',
            '    raise JSONDecodeError("Expecting value", s, err.value) from None',
            'json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)'])
        row = self.one(trace, 'python')
        self.assertEqual((row['relative'], row['line']), ('app/主.py', 8))
        self.assertTrue(row['message'].startswith('json.decoder.JSONDecodeError'))

    def test_python_syntax_error_column_and_pytest_lines(self):
        syntax = self.one('\n'.join(['  File "m.py", line 2', '    x = = 1', '        ^', 'SyntaxError: invalid syntax']),
                          'python')
        self.assertEqual((syntax['line'], syntax['column']), (2, 5))
        pytest = self.one('tests/test_a.py:14: AssertionError', 'pytest')
        self.assertEqual(pytest['line'], 14)

    def test_node_stack_and_syntax_header(self):
        stack = self.one('\n'.join([
            f'{self.root}\\index.js:3', '  throw new TypeError("bad");', '  ^', '',
            'TypeError: bad', f'    at Object.<anonymous> ({self.root}\\index.js:3:9)',
            '    at Module._compile (node:internal/modules/cjs/loader:1256:14)', '']), 'node')
        self.assertEqual((stack['relative'], stack['line'], stack['message']), ('index.js', 3, 'TypeError: bad'))
        frame = self.one('\n'.join(['ReferenceError: y is not defined',
                                    f'    at main (file:///{self.root.as_posix()}/lib/a.mjs:10:3)']), 'node')
        self.assertEqual((frame['relative'], frame['line'], frame['column']), ('lib/a.mjs', 10, 3))

    def test_java_exception_stack(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / 'src').mkdir()
            (root / 'src' / 'App.java').write_text('class App {}', encoding='utf-8')
            rows = problems.parse('Exception in thread "main" java.lang.IllegalStateException: boom\n'
                                  '\tat App.main(App.java:5)\n', ['java'], cwd=root, workspace=root)
            self.assertEqual((rows[0]['relative'], rows[0]['line']), ('src/App.java', 5))

    def test_deduplicates_and_limits(self):
        line = 'a.c:1:1: error: x'
        self.assertEqual(len(self.parse('\n'.join([line] * 3), 'gcc')), 1)
        many = '\n'.join(f'a.c:{i}:1: error: x' for i in range(1, problems.LIMIT + 50))
        self.assertEqual(len(self.parse(many, 'gcc')), problems.LIMIT)


class ExecutionTests(Workspace):
    def python_task(self, source):
        path = self.write('源码/运行 测试.py', source)
        return toolchains.choose(toolchains.file_tasks(Toolchains(self.root, which=lambda n: sys.executable
                                                                  if n == 'python' else None), path), 'run'), path

    def test_runs_with_spaces_and_cjk_and_maps_traceback(self):
        task, path = self.python_task('print("你好 world")\nraise ValueError("坏了")\n')
        policy, approvals, output = Policy(Decision.ASK), [], []
        result = tasks.execute(task, workspace=self.root, policy=policy, cancelled=threading.Event(),
                               approve=lambda title, detail, cancel: approvals.append(detail) or True,
                               on_output=output.append)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(approvals), 1)
        self.assertIn(str(path), approvals[0]['commands'][0])
        self.assertIn('你好 world', ''.join(output))
        self.assertEqual(result['problems'][0]['path'], str(path))
        self.assertEqual((result['problems'][0]['line'], result['problems'][0]['message']), (2, 'ValueError: 坏了'))
        self.assertEqual(policy.commands[0][0], 'shell_exec')

    def test_policy_deny_and_declined_approval_run_nothing(self):
        task, _ = self.python_task('open("ran.txt", "w").write("x")\n')
        for policy, approve in ((Policy(Decision.DENY), lambda *a: True),
                                (Policy(Decision.ASK, interactive=False), lambda *a: True),
                                (Policy(Decision.ASK), lambda *a: False)):
            with self.assertRaises(PermissionError):
                tasks.execute(task, workspace=self.root, policy=policy, approve=approve, cancelled=threading.Event())
        self.assertFalse((self.root / 'ran.txt').exists())

    def run_real(self, task):
        return tasks.execute(task, workspace=self.root, policy=Policy(), approve=lambda *a: True,
                             cancelled=threading.Event())

    @unittest.skipUnless(shutil.which('g++'), 'g++ not installed')
    def test_real_gcc_compile_error_maps_to_file_line(self):
        path = self.write('src/主 程序.cpp', '#include <cstdio>\nint main() {\n  return y;\n}\n')
        result = self.run_real(toolchains.choose(toolchains.file_tasks(Toolchains(self.root), path), 'run'))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(result['steps']), 1, 'run step is skipped after a failed build')
        self.assertIn((str(path), 3), [(p['path'], p['line']) for p in result['problems']])
        good = self.write('src/好.cpp', '#include <cstdio>\nint main() { std::puts("ok 好"); }\n')
        self.assertEqual(self.run_real(toolchains.choose(toolchains.file_tasks(Toolchains(self.root), good), 'run'))['status'],
                         'completed')

    @unittest.skipUnless(shutil.which('javac'), 'JDK not installed')
    def test_real_javac_error_maps_to_file_line(self):
        path = self.write('Main.java', 'public class Main {\n  public static void main(String[] a) {\n    int x = "s";\n  }\n}\n')
        build = toolchains.choose(toolchains.file_tasks(Toolchains(self.root), path), 'build')
        result = self.run_real(build)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual((result['problems'][0]['path'], result['problems'][0]['line']), (str(path), 3))

    @unittest.skipUnless(shutil.which('node'), 'Node.js not installed')
    def test_real_node_runtime_error_maps_to_file_line(self):
        path = self.write('脚本/index.js', 'const a = 1;\nnull.f();\n')
        result = self.run_real(toolchains.choose(toolchains.file_tasks(Toolchains(self.root), path), 'run'))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual((result['problems'][0]['path'], result['problems'][0]['line']), (str(path), 2))


if __name__ == '__main__':
    unittest.main()
