@echo off
rem Legacy name kept for scripts written before the rename to Pledge Keystone.
call "%~dp0keystone.cmd" %*
exit /b %ERRORLEVEL%
