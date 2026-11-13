@echo off
setlocal
cd /d "%~dp0"
set PY=runtime\python38\python.exe
if not exist "%PY%" set PY=python
"%PY%" tools\train_price_only.py %*
exit /b %ERRORLEVEL%
