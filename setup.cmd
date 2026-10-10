@echo off
setlocal
cd /d "%~dp0"

set "MODE=%~1"
if "%MODE%"=="" set "MODE=gui"

echo ============================================================
echo   Forge 一键配置（Windows）
echo   唯一前提：能上网。缺 Python 会自动静默安装。
echo   用法：setup.cmd [gui/web/check/selftest]
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

call :shortcut
echo [3/4] 桌面快捷方式已创建：Forge

set "PYW=%PYEXE%"
for %%I in ("%PYEXE%") do set "PYDIR=%%~dpI"
if exist "%PYDIR%pythonw.exe" set "PYW=%PYDIR%pythonw.exe"

if /i "%MODE%"=="web" goto :web
if not defined HAS_TK goto :web
echo [4/4] 启动 Forge 桌面窗口...
start "" "%PYW%" forge-gui\forge_gui_v2.py
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
curl -L -o "%TEMP%\forge-py-installer.exe" "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
"%TEMP%\forge-py-installer.exe" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_tcltk=1
exit /b 0

:shortcut
powershell -NoProfile -Command "$py='%PYEXE%'; $pyw=Join-Path (Split-Path $py) 'pythonw.exe'; if(-not(Test-Path $pyw)){$pyw=$py}; $repo='%~dp0'; $ws=New-Object -ComObject WScript.Shell; $desk=[Environment]::GetFolderPath('Desktop'); $s=$ws.CreateShortcut((Join-Path $desk 'Forge.lnk')); $s.TargetPath=$pyw; $s.Arguments=[char]34+(Join-Path $repo 'forge-gui\forge_gui_v2.py')+[char]34; $s.WorkingDirectory=$repo; $s.IconLocation=$pyw; $s.Save()" >nul 2>nul
exit /b 0

:py_fail
echo.
echo [错误] Python 3.10+ 检测/安装失败。
echo        请手动安装后重跑本脚本：https://www.python.org/downloads/
pause
exit /b 1
