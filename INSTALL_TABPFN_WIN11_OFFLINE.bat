@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools\bootstrap_tabpfn_win11.ps1
if errorlevel 1 exit /b 1
echo TabPFN离线环境准备完成。
endlocal
