@echo off
setlocal
cd /d "%~dp0"
set PY=runtime\python38\python.exe
if not exist "%PY%" set PY=python
"%PY%" tools\package_existing_model.py %*
exit /b %ERRORLEVEL%
