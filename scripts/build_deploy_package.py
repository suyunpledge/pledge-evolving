"""打 Windows 一键部署包：只含 git 已提交的运行时文件，绝不带 .venv、密钥或本机数据。

用法（仓库根目录）：
    python scripts/build_deploy_package.py [--ref HEAD] [--out dist]

产物：dist/Pledge-revon-<版本>-<提交>-windows.zip
    Pledge-revon/一键部署.cmd   双击：装 Python（缺时）→ 建 .venv\\qt 装 Qt → 建桌面快捷方式 → 启动
    Pledge-revon/部署说明.txt
    Pledge-revon/<运行时文件>

内容取自 ``git archive <ref>``（提交里的文件），.cmd/.bat/.ps1 统一 CRLF；
打包后扫描疑似密钥，命中即失败。
"""
from __future__ import annotations

import argparse
import io
import re
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFIX = 'Pledge-revon/'
# 运行时需要的顶层条目；手机版、调研报告、圆桌纪要等不进部署包。
INCLUDE = ('revon/', 'revon-gui/', 'bundles/', 'client/', 'bin/', 'templates/',
           'README.md', 'README.zh.md', 'LICENSE', 'pyproject.toml', 'run.py', 'revon_gui.py',
           'revon-budgets.json', 'setup.cmd', 'setup.sh', 'run-interactive.bat', 'run-selftest.bat',
           'run-task.bat', 'run-task-clean.bat', 'start-client.bat', 'RELEASE_v0.7.0.md')
EXCLUDE = re.compile(r'(^|/)(__pycache__|\.venv|desktop/dist|desktop/build[^/]*)(/|$)|\.pyc$')
CRLF = ('.cmd', '.bat', '.ps1')
SECRET = re.compile(rb'(?<![A-Za-z0-9])(sk-[A-Za-z0-9]{32,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36})')

LAUNCHER = '''@echo off
chcp 65001 >nul
cd /d "%~dp0"
call setup.cmd gui
if errorlevel 1 pause
'''
GUIDE = '''Pledge revon {version}（{commit}）一键部署包

1. 解压到任意目录（路径可以包含中文）。
2. 双击「一键部署.cmd」：
   - 没有 Python 3.10+ 时自动静默安装（winget，失败时从 python.org 下载）；
   - 在本目录 .venv\\qt 安装 Qt 桌面运行时（约 200 MB，官方源失败自动改用清华镜像）；
   - 在桌面创建「Pledge revon」快捷方式并启动桌面版。
   Qt 安装失败时自动退回经典桌面版或浏览器版。
3. 首次启动按引导选择模型厂商并粘贴 API 密钥。密钥只存本机密钥库，不写进配置文件。

其他入口：setup.cmd tk（经典桌面版）、setup.cmd web（浏览器版）、setup.cmd selftest（离线自检）。
已有旧版（~/.forge 或 ~/.revon）的配置、密钥与对话会被直接沿用。
'''


def git(*args):
    return subprocess.run(['git', *args], cwd=ROOT, check=True, capture_output=True).stdout


def wanted(path):
    return any(path == item or path.startswith(item) for item in INCLUDE) and not EXCLUDE.search(path)


def build(ref='HEAD', out=ROOT / 'dist'):
    commit = git('rev-parse', '--short', ref).decode().strip()
    version = tomllib.loads(git('show', f'{ref}:pyproject.toml').decode())['project']['version']
    target = Path(out) / f'Pledge-revon-{version}-{commit}-windows.zip'
    target.parent.mkdir(parents=True, exist_ok=True)
    archive = tarfile.open(fileobj=io.BytesIO(git('archive', '--format=tar', ref)))
    files, leaks = 0, []
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for member in archive.getmembers():
            if not member.isfile() or not wanted(member.name):
                continue
            data = archive.extractfile(member).read()
            if member.name.endswith(CRLF):
                data = data.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
            if SECRET.search(data) and '/test' not in member.name and not member.name.startswith('tests/'):
                leaks.append(member.name)
            info = zipfile.ZipInfo(PREFIX + member.name, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, data)
            files += 1
        bundle.writestr(PREFIX + '一键部署.cmd', LAUNCHER.replace('\n', '\r\n').encode('utf-8'))
        bundle.writestr(PREFIX + '部署说明.txt',
                        GUIDE.format(version=version, commit=commit).replace('\n', '\r\n').encode('utf-8'))
    if leaks:
        target.unlink()
        raise SystemExit('疑似密钥，已中止：' + ', '.join(leaks[:10]))
    return target, files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--ref', default='HEAD')
    parser.add_argument('--out', default=str(ROOT / 'dist'))
    args = parser.parse_args(argv)
    target, files = build(args.ref, args.out)
    print(f'{target}  ({files + 2} files, {target.stat().st_size / 1e6:.1f} MB)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
