@echo off
rem Local launcher so the framework is usable without installing it.
rem Prefers a system Python, falls back to whatever `python` resolves to.
setlocal
set HERE=%~dp0..
if exist "%ProgramFiles%\nodejs\node.exe" set NODE_OK=1
where py >nul 2>nul
if %ERRORLEVEL%==0 (
  py -3 "%HERE%\run.py" %*
  exit /b %ERRORLEVEL%
)
python "%HERE%\run.py" %*
exit /b %ERRORLEVEL%
