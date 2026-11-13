@echo off
setlocal
cd /d "%~dp0"
set PIPELINE_STATIC_DIR=%~dp0overrides
echo 从源码启动。页面覆盖目录: %PIPELINE_STATIC_DIR%
runtime\python38\python.exe project\run_app.py --no-browser
exit /b %ERRORLEVEL%
