@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if not exist logs mkdir logs

set "PYEXE="
if exist "runtime\venvs\price_runtime38\Scripts\python.exe" set "PYEXE=%CD%\runtime\venvs\price_runtime38\Scripts\python.exe"
if not defined PYEXE if exist "runtime\venvs\price_training38\Scripts\python.exe" set "PYEXE=%CD%\runtime\venvs\price_training38\Scripts\python.exe"
if not defined PYEXE if defined PYTHON38_EXE if exist "%PYTHON38_EXE%" set "PYEXE=%PYTHON38_EXE%"
if not defined PYEXE for /f "delims=" %%P in ('where python 2^>nul') do if not defined PYEXE set "PYEXE=%%P"

if not defined PYEXE (
  echo [ERROR] 未找到 64 位 Python 3.8。请先运行 CREATE_PRICE_RUNTIME_ENV_WIN7.bat。
  pause
  exit /b 1
)
if not exist "services\price_service\model\price_native_bundle.pkl" (
  echo [ERROR] 缺少原生价格模型文件。
  pause
  exit /b 1
)

"%PYEXE%" -c "import struct,sys; raise SystemExit(0 if sys.version_info[:2]==(3,8) and struct.calcsize('P')==8 else 1)"
if errorlevel 1 (
  echo [ERROR] 必须使用 64 位 Python 3.8。
  pause
  exit /b 1
)

echo [INFO] 启动独立价格模型服务: http://127.0.0.1:18101/docs
echo [INFO] 日志: logs\price_service.log
"%PYEXE%" -m services.price_service.app --host 127.0.0.1 --port 18101 ^
  --model "services\price_service\model\price_native_bundle.pkl" ^
  --fallback-json "" >> logs\price_service.log 2>&1
exit /b %ERRORLEVEL%
