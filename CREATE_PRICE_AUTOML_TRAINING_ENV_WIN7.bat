@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "BASEPY="
if defined PYTHON38_EXE if exist "%PYTHON38_EXE%" set "BASEPY=%PYTHON38_EXE%"
if not defined BASEPY if defined CONDA_PREFIX if exist "%CONDA_PREFIX%\python.exe" set "BASEPY=%CONDA_PREFIX%\python.exe"
if not defined BASEPY for /f "delims=" %%P in ('where python 2^>nul') do if not defined BASEPY set "BASEPY=%%P"
if not defined BASEPY (
  echo [ERROR] 需要 64 位 Python 3.8。
  pause
  exit /b 1
)
"%BASEPY%" -c "import struct,sys; raise SystemExit(0 if sys.version_info[:2]==(3,8) and struct.calcsize('P')==8 else 1)"
if errorlevel 1 (
  echo [ERROR] 解释器不是 64 位 Python 3.8。
  pause
  exit /b 1
)
if not exist "vendor\automl_sklearn_021_024_bundle.zip" (
  echo [ERROR] 缺少用户提供的 AutoML 源码包。
  pause
  exit /b 1
)
if not exist "services\price_service\wheelhouse_automl024_win7" (
  echo [ERROR] 缺少 AutoML 0.24 wheelhouse。
  pause
  exit /b 1
)

"%BASEPY%" tools\prepare_automl_sources.py --zip "vendor\automl_sklearn_021_024_bundle.zip"
if errorlevel 1 goto failed
set "VENV=%CD%\runtime\venvs\automl02438"
if not exist "%VENV%\Scripts\python.exe" (
  "%BASEPY%" -m venv "%VENV%"
  if errorlevel 1 goto failed
)
"%VENV%\Scripts\python.exe" -m pip install --no-index --disable-pip-version-check ^
  --find-links "services\price_service\wheelhouse_automl024_win7" ^
  -r "services\price_service\requirements_automl_024_py38_win7.txt"
if errorlevel 1 goto failed
"%VENV%\Scripts\python.exe" tools\verify_price_training_only_environment.py --profile automl024 --check-automl-bundle
if errorlevel 1 goto failed
echo [PASS] AutoML 0.24 价格训练环境已就绪。
pause
exit /b 0

:failed
echo [FAIL] AutoML 价格训练环境创建或验证失败。
pause
exit /b 1
