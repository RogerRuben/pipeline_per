@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYEXE=runtime\venvs\price_training38\Scripts\python.exe"
if not exist "%PYEXE%" if defined PYTHON38_EXE set "PYEXE=%PYTHON38_EXE%"
if not exist "%PYEXE%" set "PYEXE=python"
"%PYEXE%" tools\verify_price_training_only_environment.py --profile training --check-wheelhouse
exit /b %ERRORLEVEL%
