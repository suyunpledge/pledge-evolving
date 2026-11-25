@echo off
setlocal
set "REVON_QT_PYTHON=%~dp0..\.venv\qt\Scripts\pythonw.exe"
if not exist "%REVON_QT_PYTHON%" (
    echo Qt runtime is missing. Run desktop\install-qt.ps1 first.
    pause
    exit /b 1
)
start "" "%REVON_QT_PYTHON%" "%~dp0revon_qt.py" %*
