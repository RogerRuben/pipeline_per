@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PYEXE="
if defined PYTHON_EXE if exist "%PYTHON_EXE%" set "PYEXE=%PYTHON_EXE%"
if not defined PYEXE if exist "runtime\venvs\model_runtime38\Scripts\python.exe" set "PYEXE=%CD%\runtime\venvs\model_runtime38\Scripts\python.exe"
if not defined PYEXE for /f "delims=" %%P in ('where python 2^>nul') do if not defined PYEXE set "PYEXE=%%P"
if not defined PYEXE (echo [ERROR] Python not found.&pause&exit /b 1)

"%PYEXE%" tools\build_complete_win7_offline.py %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" pause
exit /b %RC%
