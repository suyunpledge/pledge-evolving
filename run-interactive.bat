@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
echo.
echo  =============================
echo   Planet evolving (forge)
echo  =============================
echo.
set /p TASK=Enter task: 
if not defined TASK goto :eof
python run.py run !TASK!
echo.
pause