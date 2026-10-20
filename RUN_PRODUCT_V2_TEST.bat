@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PRODUCT_V2_TEST_PY=%~dp0runtime\venvs\automl02438\Scripts\python.exe"
if not exist "%PRODUCT_V2_TEST_PY%" (
  echo 请先运行 CREATE_AUTOML_ENV_PY38_WIN7.bat。
  exit /b 1
)
"%PRODUCT_V2_TEST_PY%" tests\product_price_automl_v2_test.py
if errorlevel 1 exit /b 1
"%PRODUCT_V2_TEST_PY%" tests\price_expert_fusion_probe_test.py
if errorlevel 1 exit /b 1
echo V2微样本流水线与专家融合测试通过。
endlocal
