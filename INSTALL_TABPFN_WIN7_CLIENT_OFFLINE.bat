@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools\bootstrap_tabpfn_win11.ps1 -Profile win7_client
exit /b %errorlevel%
