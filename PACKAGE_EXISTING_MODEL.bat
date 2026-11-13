@echo off
setlocal
cd /d "%~dp0"
echo 使用上一次训练结果打包，不重新训练。
runtime\python38\python.exe project\tools\client_builder.py repack
exit /b %ERRORLEVEL%
