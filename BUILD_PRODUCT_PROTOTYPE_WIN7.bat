@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PIPELINE_PY=%~dp0runtime\venvs\automl02438\Scripts\python.exe"
if not exist "%PIPELINE_PY%" (
  echo 请先运行 CREATE_AUTOML_ENV_PY38_WIN7.bat 创建 Python 3.8 AutoML 环境。
  exit /b 1
)
if "%~1"=="" (
  "%PIPELINE_PY%" tools\product_pipeline.py build --demo
) else (
  "%PIPELINE_PY%" tools\product_pipeline.py build %*
)
if errorlevel 1 exit /b 1
echo 请在输出目录运行 START_PROTOTYPE_WIN7.bat 打开独立试用系统。
endlocal
