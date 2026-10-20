@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PYEXE="
if defined PYTHON_EXE if exist "%PYTHON_EXE%" set "PYEXE=%PYTHON_EXE%"
if not defined PYEXE for /f "delims=" %%P in ('where python 2^>nul') do if not defined PYEXE set "PYEXE=%%P"
if not defined PYEXE (
  echo [ERROR] A connected Python with pip was not found.
  pause
  exit /b 1
)

if not exist "services\price_service\wheelhouse_automl024_win7" mkdir "services\price_service\wheelhouse_automl024_win7"
echo [INFO] Downloading AutoML sklearn 0.24 / Python 3.8 / Win64 wheels.
"%PYEXE%" -m pip download --only-binary=:all: --no-deps --implementation cp --python-version 38 --abi cp38 --platform win_amd64 --dest "services\price_service\wheelhouse_automl024_win7" -r "services\price_service\requirements_automl_024_py38_win7.txt"
if errorlevel 1 (
  echo [FAIL] AutoML wheel download failed.
  pause
  exit /b 1
)

rem pip target-marker compatibility: download Python 3.8 transitive wheels explicitly.
"%PYEXE%" -m pip download --only-binary=:all: --no-deps --implementation cp --python-version 38 --abi cp38 --platform win_amd64 --dest "services\price_service\wheelhouse_automl024_win7" alembic==1.13.1 colorlog==6.8.2 packaging==24.0 SQLAlchemy==2.0.29 tqdm==4.66.2 PyYAML==6.0.1 Mako==1.3.3 greenlet==3.0.3 python-dateutil==2.8.2 pytz==2024.1 six==1.16.0 typing-extensions==4.11.0 importlib-metadata==7.1.0 importlib-resources==6.4.5 zipp==3.19.2 colorama==0.4.6 MarkupSafe==2.1.5
if errorlevel 1 (
  echo [FAIL] AutoML transitive wheel download failed.
  pause
  exit /b 1
)

"%PYEXE%" tools\wheelhouse_manifest.py --wheelhouse "services\price_service\wheelhouse_automl024_win7" --output "services\price_service\wheelhouse_automl024_win7\WHEELHOUSE_MANIFEST.json"
if errorlevel 1 (
  echo [FAIL] AutoML wheelhouse manifest generation failed.
  pause
  exit /b 1
)
echo [PASS] AutoML 0.24 wheelhouse is ready.
pause
exit /b 0
