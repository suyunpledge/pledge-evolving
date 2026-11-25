@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "MODE=%~1"
if "%MODE%"=="" set "MODE=gui"

echo ============================================================
echo   Pledge Keystone 一键部署（Windows）
echo   唯一前提：能上网。缺 Python 会自动静默安装；
echo   桌面版（Qt）运行时装在本目录 .venv\qt，不改动全局 Python。
echo   用法：setup.cmd [gui/tk/web/check/selftest]
echo     gui      默认：Qt 桌面版（Qt 安装失败时自动退回经典版或 Web）
echo     tk       经典 Tk 桌面版
echo     web      浏览器版 http://127.0.0.1:7712
echo     check    只检查环境
echo     selftest 离线自检（无需网络与密钥）
echo ============================================================
echo.

set "PYEXE="
call :detect

if not defined PYEXE (
  echo [1/4] 未检测到 Python 3.10+，开始自动安装（几分钟，请勿关窗）...
  call :install_python
  call :detect
)
if not defined PYEXE goto :py_fail
echo [1/4] Python 就绪：
"%PYEXE%" --version

set "HAS_TK="
"%PYEXE%" -c "import tkinter" >nul 2>nul && set "HAS_TK=1"
if defined HAS_TK (echo [2/4] tkinter 就绪，桌面 GUI 可用) else (echo [2/4] tkinter 缺失：GUI 不可用，将自动转 Web 模式)

if /i "%MODE%"=="check" (
  echo [4/4] 环境检查完成，未做安装以外的任何改动。
  exit /b 0
)

if /i "%MODE%"=="selftest" (
  echo [3/4] 运行离线自检（无需网络与密钥）...
  "%PYEXE%" run.py selftest
  exit /b %errorlevel%
)

set "PYW=%PYEXE%"
for %%I in ("%PYEXE%") do set "PYDIR=%%~dpI"
if exist "%PYDIR%pythonw.exe" set "PYW=%PYDIR%pythonw.exe"

set "HAS_QT="
set "QTPY=%~dp0.venv\qt\Scripts\python.exe"
set "QTPYW=%~dp0.venv\qt\Scripts\pythonw.exe"
if /i "%MODE%"=="gui" call :qt_runtime

call :shortcut
echo [3/4] 桌面快捷方式已创建：Pledge Keystone

if /i "%MODE%"=="web" goto :web
if /i "%MODE%"=="tk" goto :tk
if defined HAS_QT (
  echo [4/4] 启动 Keystone 桌面版（Qt）...
  start "" "%QTPYW%" keystone-gui\keystone_qt.py
  exit /b 0
)
:tk
if not defined HAS_TK goto :web
echo [4/4] 启动 Keystone 经典桌面窗口...
start "" "%PYW%" keystone-gui\keystone_gui_v2.py
exit /b 0

:web
echo [4/4] 启动 Web 客户端，浏览器将自动打开 http://127.0.0.1:7712 ...
start "" "%PYW%" client\server.py
timeout /t 2 /nobreak >nul
start http://127.0.0.1:7712
exit /b 0

:detect
set "PYEXE="
py -3 -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul && for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%i"
if defined PYEXE exit /b 0
python -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul && for /f "delims=" %%i in ('python -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%i"
if defined PYEXE exit /b 0
for %%P in ("%LocalAppData%\Programs\Python\Python312\python.exe" "%LocalAppData%\Programs\Python\Python313\python.exe" "%LocalAppData%\Programs\Python\Python311\python.exe" "%ProgramFiles%\Python312\python.exe" "%ProgramFiles%\Python313\python.exe") do (
  if not defined PYEXE if exist "%%~P" "%%~P" -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul && set "PYEXE=%%~P"
)
exit /b 0

:install_python
where winget >nul 2>nul
if not errorlevel 1 (
  echo       正在通过 winget 安装 Python 3.12，请等待完成...
  winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
  call :detect
)
if defined PYEXE exit /b 0
echo       winget 不可用或失败，改从 python.org 下载安装器静默安装...
where curl >nul 2>nul
if errorlevel 1 (
  echo [错误] 此机器没有 winget 也没有 curl，无法自动下载。
  echo        请手动安装 Python 3.10+ 后重跑本脚本：https://www.python.org/downloads/
  pause
  exit /b 1
)
curl -L -o "%TEMP%\keystone-py-installer.exe" "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
"%TEMP%\keystone-py-installer.exe" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_tcltk=1
exit /b 0

:qt_runtime
echo [3/4] 准备 Qt 桌面运行时（首次约需下载 200 MB）...
if not exist "%QTPY%" "%PYEXE%" -m venv "%~dp0.venv\qt"
if not exist "%QTPY%" goto :qt_fail
"%QTPY%" -c "from importlib.util import find_spec; import sys; sys.exit(any(find_spec(n) is None for n in ('PySide6','comtypes','rapidocr_onnxruntime')))" >nul 2>nul && goto :qt_ready
"%QTPY%" -m pip install --disable-pip-version-check -q -r keystone-gui\requirements-qt.txt
if errorlevel 1 (
  echo       官方源失败，改用清华镜像重试...
  "%QTPY%" -m pip install --disable-pip-version-check -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r keystone-gui\requirements-qt.txt
)
"%QTPY%" -c "import PySide6" >nul 2>nul || goto :qt_fail
:qt_ready
"%QTPY%" keystone-gui\qt_frontend\build_shaders.py >nul 2>nul
set "HAS_QT=1"
echo       Qt 运行时就绪。
exit /b 0
:qt_fail
echo       [提示] Qt 运行时安装失败，将改用经典桌面版或 Web 版；稍后可重跑 setup.cmd 重试。
exit /b 0

:shortcut
set "SC_TARGET=%PYW%"
set "SC_SCRIPT=keystone-gui\keystone_gui_v2.py"
if defined HAS_QT set "SC_TARGET=%QTPYW%"
if defined HAS_QT set "SC_SCRIPT=keystone-gui\keystone_qt.py"
powershell -NoProfile -Command "$repo='%~dp0'; $ws=New-Object -ComObject WScript.Shell; $desk=[Environment]::GetFolderPath('Desktop'); $s=$ws.CreateShortcut((Join-Path $desk 'Pledge Keystone.lnk')); $s.TargetPath='%SC_TARGET%'; $s.Arguments=[char]34+(Join-Path $repo '%SC_SCRIPT%')+[char]34; $s.WorkingDirectory=(Join-Path $repo 'keystone-gui'); $ico=Join-Path $repo 'keystone-gui\desktop\keystone.ico'; if(Test-Path $ico){$s.IconLocation=$ico}; $s.Save()" >nul 2>nul
exit /b 0

:py_fail
echo.
echo [错误] Python 3.10+ 检测/安装失败。
echo        请手动安装后重跑本脚本：https://www.python.org/downloads/
pause
exit /b 1
