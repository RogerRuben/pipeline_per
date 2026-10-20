@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
"%~dp0runtime\venvs\automl02438\Scripts\python.exe" tests\product_pipeline_test.py
exit /b %errorlevel%
