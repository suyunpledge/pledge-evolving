@echo off
chcp 65001 >nul
echo.
echo  =============================
echo   pledge-evolving 运行任务
echo  =============================
echo.
cd /d "%~dp0"
python run.py run %*
echo.
echo  =============================
echo  按任意键关闭...
echo  =============================
pause >nul
