@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "BASEPY="
if defined PYTHON38_EXE if exist "%PYTHON38_EXE%" set "BASEPY=%PYTHON38_EXE%"
if not defined BASEPY if exist "runtime\venvs\model_runtime38\Scripts\python.exe" set "BASEPY=%CD%\runtime\venvs\model_runtime38\Scripts\python.exe"
if not defined BASEPY if defined CONDA_PREFIX if exist "%CONDA_PREFIX%\python.exe" set "BASEPY=%CONDA_PREFIX%\python.exe"
if not defined BASEPY for /f "delims=" %%P in ('where python 2^>nul') do if not defined BASEPY set "BASEPY=%%P"

if not defined BASEPY (
  echo [ERROR] Python 3.8 x64 was not found.
  echo Set PYTHON38_EXE to the full path of python.exe.
  pause
  exit /b 1
)

"%BASEPY%" -c "import struct,sys; raise SystemExit(0 if sys.version_info[:2]==(3,8) and struct.calcsize('P')==8 else 1)"
if errorlevel 1 (
  echo [ERROR] Selected interpreter is not 64-bit Python 3.8: %BASEPY%
  pause
  exit /b 1
)

if not exist "vendor\automl_sklearn_021_024_bundle.zip" (
  echo [ERROR] Missing vendor\automl_sklearn_021_024_bundle.zip.
  pause
  exit /b 1
)
if not exist "services\price_service\wheelhouse_automl024_win7" (
  echo [ERROR] Missing AutoML 0.24 wheelhouse.
  echo Run DOWNLOAD_AUTOML_024_WHEELS_PY38_WIN64.bat on a connected computer first.
  pause
  exit /b 1
)

"%BASEPY%" tools\prepare_automl_sources.py --zip "vendor\automl_sklearn_021_024_bundle.zip"
if errorlevel 1 goto failed

set "VENV=%CD%\runtime\venvs\automl02438"
if not exist "%VENV%\Scripts\python.exe" (
  echo [INFO] Creating AutoML environment: %VENV%
  "%BASEPY%" -m venv "%VENV%"
  if errorlevel 1 goto failed
)

"%VENV%\Scripts\python.exe" -m pip install --no-index --disable-pip-version-check ^
  --find-links "services\price_service\wheelhouse_automl024_win7" ^
  -r "services\price_service\requirements_automl_024_py38_win7.txt"
if errorlevel 1 goto failed

"%VENV%\Scripts\python.exe" tools\verify_complete_win7_environment.py --profile automl024
if errorlevel 1 goto failed

echo.
echo [PASS] AutoML sklearn 0.24 environment is ready.
echo Source: tools\automl\sklearn_0_24\automl_search.py
pause
exit /b 0

:failed
echo.
echo [FAIL] AutoML environment creation or verification failed.
pause
exit /b 1
