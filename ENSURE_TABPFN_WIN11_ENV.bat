@echo off
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
if not exist "runtime\tabpfn311\environment_ready.json" goto install
runtime\tabpfn311\python.exe tools\check_tabpfn_environment.py --profile win11
if not errorlevel 1 exit /b 0
:install
echo 正在离线准备或修复包内环境，请等待安装与实际预测检查完成。
call INSTALL_TABPFN_WIN11_OFFLINE.bat
exit /b %errorlevel%
