@echo off
setlocal
cd /d "%~dp0"
if "%~1"=="" (
  echo 用法: REBUILD_ENGINE.bat 输出目录 构建工具目录 [--check-only]
  echo 示例: REBUILD_ENGINE.bat outputs\engine_new C:\pyinstaller-tools --check-only
  exit /b 1
)
set PY=runtime\python38\python.exe
if not exist "%PY%" set PY=python
if exist "project\tools\rebuild_engine_cli.py" (
  "%PY%" project\tools\rebuild_engine_cli.py --out "%~1" --build-tools "%~2" %3 %4
) else (
  "%PY%" tools\rebuild_engine_cli.py --out "%~1" --build-tools "%~2" %3 %4
)
exit /b %ERRORLEVEL%
