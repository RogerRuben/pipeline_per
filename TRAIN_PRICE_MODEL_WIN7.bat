@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

set "TRAIN_PY=%CD%\runtime\venvs\price_training38\Scripts\python.exe"
if not exist "%TRAIN_PY%" (
  echo [ERROR] 价格训练环境不存在，请先运行 CREATE_PRICE_MODEL_TRAINING_ENV_WIN7.bat
  pause
  exit /b 1
)
if not exist "services\price_service\model" mkdir "services\price_service\model"

echo ============================================================
echo 价格模型训练与原生模型导出
echo 只需要：历史成品 CSV/XLSX + 成品代号
echo 输出：services\price_service\model\price_native_bundle.pkl
echo ============================================================
set /p "PRICE_TABLE=请输入历史成品 CSV/XLSX 完整路径: "
set /p "PRODUCT_CODE=请输入成品代号: "
set /p "PRODUCT_NAME=请输入成品名称（可直接回车）: "
set /p "TARGET_COLUMN=请输入价格列名（可直接回车自动识别）: "
if "%PRICE_TABLE%"=="" goto usage_error
if "%PRODUCT_CODE%"=="" goto usage_error

if "%TARGET_COLUMN%"=="" (
  "%TRAIN_PY%" tools\train_price_service_model.py "%PRICE_TABLE%" "%PRODUCT_CODE%" --output services\price_service\model\price_native_bundle.pkl --product-name "%PRODUCT_NAME%"
) else (
  "%TRAIN_PY%" tools\train_price_service_model.py "%PRICE_TABLE%" "%PRODUCT_CODE%" --output services\price_service\model\price_native_bundle.pkl --product-name "%PRODUCT_NAME%" --target "%TARGET_COLUMN%"
)
if errorlevel 1 goto failed

echo.
echo [PASS] 价格模型训练和导出完成。
echo 模型文件：services\price_service\model\price_native_bundle.pkl
echo 报告文件：services\price_service\model\price_native_bundle.pkl.training.json
pause
exit /b 0

:usage_error
echo [ERROR] 历史成品表和成品代号不能为空。
pause
exit /b 2

:failed
echo [FAIL] 价格模型训练或导出失败。
pause
exit /b 1
