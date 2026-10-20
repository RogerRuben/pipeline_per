@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist "runtime\venvs\automl02438\Scripts\python.exe" (
  echo [INFO] AutoML environment is missing; creating it now.
  call CREATE_AUTOML_ENV_PY38_WIN7.bat
  if errorlevel 1 exit /b 1
)

"runtime\venvs\automl02438\Scripts\python.exe" tools\prepare_automl_sources.py --zip "vendor\automl_sklearn_021_024_bundle.zip"
if errorlevel 1 exit /b 1

if "%~1"=="" (
  echo.
  echo AutoML 0.24 usage:
  echo   RUN_AUTOML_024_WIN7.bat --csv data\your_data.csv --target "价格_万元" --trials 50 --output outputs\automl
  echo   RUN_AUTOML_024_WIN7.bat --predict-model outputs\automl\final_model.joblib --predict-csv data\new_data.csv --output outputs\predictions.csv
  echo.
  "runtime\venvs\automl02438\Scripts\python.exe" tools\automl\sklearn_0_24\automl_search.py --help
  pause
  exit /b 0
)

"runtime\venvs\automl02438\Scripts\python.exe" tools\automl\sklearn_0_24\automl_search.py %*
set "RC=%ERRORLEVEL%"
pause
exit /b %RC%
