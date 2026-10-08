@echo off
chcp 65001 >nul
echo.
echo  =============================
echo   pledge-evolving selftest
echo  =============================
echo.
cd /d "%~dp0"
python run.py selftest
echo.
echo  =============================
echo  按任意键关闭...
echo  =============================
pause >nul
