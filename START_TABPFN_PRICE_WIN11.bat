@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "HF_HUB_OFFLINE=1"
set "HF_HUB_DISABLE_TELEMETRY=1"
call ENSURE_TABPFN_WIN11_ENV.bat
if errorlevel 1 exit /b 1
set "TABPFN_SERVE_MODEL=%~1"
if "%TABPFN_SERVE_MODEL%"=="" set "TABPFN_SERVE_MODEL=prototype\price\price_native_bundle.pkl"
runtime\tabpfn311\python.exe -m services.tabpfn_price_service.app --model "%TABPFN_SERVE_MODEL%" --host 0.0.0.0 --port 18901
exit /b %errorlevel%
