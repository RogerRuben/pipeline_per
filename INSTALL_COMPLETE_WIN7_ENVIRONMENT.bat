@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ============================================================
echo Industrial Protocol Demo - Complete Win7 Offline Environment
echo Requires: 64-bit CPython 3.8 installed on this computer
echo ============================================================

call CREATE_MODEL_RUNTIME_ENV_WIN7.bat
if errorlevel 1 exit /b 1

call CREATE_PRICE_TRAINING_ENV_PY38.bat
if errorlevel 1 exit /b 1

call CREATE_AUTOML_ENV_PY38_WIN7.bat
if errorlevel 1 exit /b 1

call VERIFY_COMPLETE_WIN7.bat
exit /b %ERRORLEVEL%
