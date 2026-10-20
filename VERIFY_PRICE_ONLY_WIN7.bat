@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYEXE=runtime\venvs\price_runtime38\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=runtime\venvs\price_training38\Scripts\python.exe"
if not exist "%PYEXE%" if defined PYTHON38_EXE set "PYEXE=%PYTHON38_EXE%"
if not exist "%PYEXE%" set "PYEXE=python"
"%PYEXE%" tools\verify_price_only_environment.py --profile runtime --check-wheelhouse --smoke-model
exit /b %ERRORLEVEL%
