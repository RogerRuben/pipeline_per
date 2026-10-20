@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
call ENSURE_TABPFN_WIN11_ENV.bat
if errorlevel 1 exit /b 1
runtime\tabpfn311\python.exe tools\launch_tabpfn_demo.py %*
exit /b %errorlevel%
