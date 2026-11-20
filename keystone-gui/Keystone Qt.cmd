@echo off
setlocal
set "KEYSTONE_QT_PYTHON=%~dp0..\.venv\qt\Scripts\pythonw.exe"
if not exist "%KEYSTONE_QT_PYTHON%" (
    echo Qt runtime is missing. Run desktop\install-qt.ps1 first.
    pause
    exit /b 1
)
start "" "%KEYSTONE_QT_PYTHON%" "%~dp0keystone_qt.py" %*
