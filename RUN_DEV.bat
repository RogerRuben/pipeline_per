@echo off
setlocal
cd /d "%~dp0"
set PIPELINE_STATIC_DIR=%~dp0page_overrides
set PY=runtime\python38\python.exe
if not exist "%PY%" set PY=python
if exist "project\run_app.py" (
  "%PY%" project\run_app.py --no-browser %*
) else (
  "%PY%" run_app.py --no-browser %*
)
exit /b %ERRORLEVEL%
