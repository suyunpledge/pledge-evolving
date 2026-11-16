@echo off
setlocal
set "FORGE_QT_PYTHON=%~dp0..\.venv\qt\Scripts\pythonw.exe"
if not exist "%FORGE_QT_PYTHON%" (
    echo Qt runtime is missing. Run desktop\install-qt.ps1 first.
    pause
    exit /b 1
)
start "" "%FORGE_QT_PYTHON%" "%~dp0forge_qt.py" %*
