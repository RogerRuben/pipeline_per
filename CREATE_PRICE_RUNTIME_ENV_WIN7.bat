@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "BASEPY="
if defined PYTHON38_EXE if exist "%PYTHON38_EXE%" set "BASEPY=%PYTHON38_EXE%"
if not defined BASEPY if defined CONDA_PREFIX if exist "%CONDA_PREFIX%\python.exe" set "BASEPY=%CONDA_PREFIX%\python.exe"
if not defined BASEPY for /f "delims=" %%P in ('where python 2^>nul') do if not defined BASEPY set "BASEPY=%%P"

if not defined BASEPY (
  echo [ERROR] 需要 64 位 Python 3.8。
  echo 可设置 PYTHON38_EXE=完整的python.exe路径。
  pause
  exit /b 1
)

"%BASEPY%" -c "import struct,sys; raise SystemExit(0 if sys.version_info[:2]==(3,8) and struct.calcsize('P')==8 else 1)"
if errorlevel 1 (
  echo [ERROR] 解释器不是 64 位 Python 3.8: %BASEPY%
  pause
  exit /b 1
)
if not exist "services\price_service\wheelhouse_win7" (
  echo [ERROR] 缺少价格模型离线 wheelhouse。
  pause
  exit /b 1
)

set "VENV=%CD%\runtime\venvs\price_runtime38"
if not exist "%VENV%\Scripts\python.exe" (
  echo [INFO] 创建价格模型运行环境: %VENV%
  "%BASEPY%" -m venv "%VENV%"
  if errorlevel 1 goto failed
)

"%VENV%\Scripts\python.exe" -m pip install --no-index --disable-pip-version-check ^
  --find-links "services\price_service\wheelhouse_win7" ^
  -r "services\price_service\requirements_win7_exact.txt"
if errorlevel 1 goto failed

"%VENV%\Scripts\python.exe" tools\verify_price_only_environment.py --profile runtime --check-wheelhouse --smoke-model
if errorlevel 1 goto failed

echo.
echo [PASS] 价格模型运行环境已就绪。
echo 启动服务请运行 START_PRICE_MODEL_WIN7.bat
pause
exit /b 0

:failed
echo.
echo [FAIL] 价格模型运行环境创建或验证失败。
pause
exit /b 1
