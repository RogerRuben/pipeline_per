@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PYEXE="
if exist "runtime\venvs\model_runtime38\Scripts\python.exe" set "PYEXE=%CD%\runtime\venvs\model_runtime38\Scripts\python.exe"
if not defined PYEXE if defined PYTHON38_EXE if exist "%PYTHON38_EXE%" set "PYEXE=%PYTHON38_EXE%"
if not defined PYEXE for /f "delims=" %%P in ('where python 2^>nul') do if not defined PYEXE set "PYEXE=%%P"
if not defined PYEXE (echo [ERROR] Python 3.8 not found.&pause&exit /b 1)

"%PYEXE%" tools\verify_complete_win7_environment.py --profile runtime --check-wheelhouses --check-automl-bundle
set "RC=%ERRORLEVEL%"
if exist "runtime\venvs\price_training38\Scripts\python.exe" (
  echo.
  "runtime\venvs\price_training38\Scripts\python.exe" tools\verify_complete_win7_environment.py --profile training
  if errorlevel 1 set "RC=1"
)
if exist "runtime\venvs\automl02438\Scripts\python.exe" (
  echo.
  "runtime\venvs\automl02438\Scripts\python.exe" tools\verify_complete_win7_environment.py --profile automl024
  if errorlevel 1 set "RC=1"
)
if "%RC%"=="0" (echo [PASS] Complete Win7 package verification passed.) else (echo [FAIL] Complete Win7 package verification failed.)
pause
exit /b %RC%
