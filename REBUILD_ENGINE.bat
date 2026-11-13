@echo off
setlocal
cd /d "%~dp0"
echo 源码变更后重新冻结引擎。不会沿用旧的源码摘要。
echo 需要本机已有离线 PyInstaller 与 Python 3.8 构建环境。
runtime\python38\python.exe project\tools\build_frozen_engine.py
exit /b %ERRORLEVEL%
