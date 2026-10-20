@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
if "%~1"=="" (
  echo 用法：START_TABPFN_DEMO_WIN7.bat http://Win11机局域网IP:18901
  exit /b 2
)
if not defined TABPFN_WIN7_PYTHON if exist "runtime\tabpfn38\python.exe" set "TABPFN_WIN7_PYTHON=%~dp0runtime\tabpfn38\python.exe"
if not defined TABPFN_WIN7_PYTHON if exist "runtime\venvs\automl02438\Scripts\python.exe" set "TABPFN_WIN7_PYTHON=%~dp0runtime\venvs\automl02438\Scripts\python.exe"
if defined TABPFN_WIN7_PYTHON (
  "%TABPFN_WIN7_PYTHON%" tools\run_tabpfn_win7_client.py --price-url "%~1"
) else (
  py -3.8 tools\run_tabpfn_win7_client.py --price-url "%~1"
)
exit /b %errorlevel%
