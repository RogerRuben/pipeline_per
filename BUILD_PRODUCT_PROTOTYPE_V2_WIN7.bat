@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PRODUCT_V2_PY=%~dp0runtime\venvs\automl02438\Scripts\python.exe"
if not exist "%PRODUCT_V2_PY%" (
  echo 请先运行 CREATE_AUTOML_ENV_PY38_WIN7.bat。
  exit /b 1
)
if "%~1"=="" (
  "%PRODUCT_V2_PY%" tools\product_pipeline.py build tests\fixtures\product_pipeline_v2\virtual_10x50.xlsx --price-engine v2 --price-config tests\fixtures\product_pipeline_v2\price_config.json --virtual-data --product-code TINY_V2_PRODUCT --product-name "50属性虚拟成品（V2测试）"
) else (
  "%PRODUCT_V2_PY%" tools\product_pipeline.py build %* --price-engine v2
)
if errorlevel 1 exit /b 1
echo 构建完成后，在输出目录运行 START_PROTOTYPE_WIN7.bat。
endlocal
