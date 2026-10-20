@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "HF_HUB_OFFLINE=1"
set "HF_HUB_DISABLE_TELEMETRY=1"
call ENSURE_TABPFN_WIN11_ENV.bat
if errorlevel 1 exit /b 1
if "%~1"=="" (
  runtime\tabpfn311\python.exe tools\product_pipeline.py build tests\fixtures\product_pipeline_v2\virtual_10x50.xlsx --price-engine tabpfn --price-config docs\examples\price_tabpfn_micro_robust.config.json --product-code TABPFN_PRODUCT --product-name "属性评分微样本修正版" --virtual-data
) else (
  runtime\tabpfn311\python.exe tools\product_pipeline.py build %* --price-engine tabpfn
)
exit /b %errorlevel%
