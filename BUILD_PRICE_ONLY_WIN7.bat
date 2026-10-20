@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYEXE=python"
if defined PYTHON38_EXE if exist "%PYTHON38_EXE%" set "PYEXE=%PYTHON38_EXE%"
if exist "runtime\venvs\price_training38\Scripts\python.exe" set "PYEXE=%CD%\runtime\venvs\price_training38\Scripts\python.exe"
if exist "runtime\venvs\model_runtime38\Scripts\python.exe" set "PYEXE=%CD%\runtime\venvs\model_runtime38\Scripts\python.exe"
if not exist deliverables\price_only_win7 mkdir deliverables\price_only_win7
"%PYEXE%" tools\build_price_only_win7.py --output-dir deliverables\price_only_win7 --name IndustrialProtocolDemo_V19_6_14_PRICE_ONLY_WIN7
exit /b %ERRORLEVEL%
