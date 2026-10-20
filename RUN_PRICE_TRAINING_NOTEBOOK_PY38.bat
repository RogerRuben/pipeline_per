@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PYEXE=%CD%\runtime\venvs\price_training38\Scripts\python.exe"
if not exist "%PYEXE%" (
  echo [ERROR] Price training environment does not exist.
  echo Run CREATE_PRICE_TRAINING_ENV_PY38.bat first.
  pause
  exit /b 1
)

set "JUPYTER_CONFIG_DIR=%CD%\runtime\venvs\price_training38\jupyter_config"
set "JUPYTER_DATA_DIR=%CD%\runtime\venvs\price_training38\jupyter_data"
set "JUPYTER_PATH=%CD%\runtime\venvs\price_training38\share\jupyter"
if not exist "%JUPYTER_CONFIG_DIR%" mkdir "%JUPYTER_CONFIG_DIR%"
if not exist "%JUPYTER_DATA_DIR%" mkdir "%JUPYTER_DATA_DIR%"

set "NOTEBOOK="
for %%N in ("%CD%\*V19_6*.ipynb") do if not defined NOTEBOOK set "NOTEBOOK=%%~fN"
if not defined NOTEBOOK (
  echo [ERROR] Training Notebook matching *V19_6*.ipynb was not found.
  pause
  exit /b 1
)

echo [INFO] Starting local Notebook with the isolated Python 3.8 environment.
"%PYEXE%" -m notebook "%NOTEBOOK%"
exit /b %ERRORLEVEL%
