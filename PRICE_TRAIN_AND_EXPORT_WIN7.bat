@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

set "PYEXE=%CD%\runtime\python38\python.exe"
if not exist "%PYEXE%" (
  echo [ERROR] 包内 Python 3.8 运行时缺失，请重新解压完整 ZIP。
  goto failed
)
"%PYEXE%" -c "import struct,sys; raise SystemExit(0 if sys.version_info[:2]==(3,8) and struct.calcsize('P')==8 else 1)"
if errorlevel 1 (
  echo [ERROR] 包内 Python 运行时不是 64 位 Python 3.8。
  goto failed
)

if "%~1"=="" (
  set "INPUT=%CD%\sample_virtual_price_history.csv"
  set "PRODUCT_CODE=VIRTUAL_PRICE_DEMO"
  set "PRODUCT_NAME=Virtual Price Demo"
  set "TARGET=price_wan"
) else (
  set "INPUT=%~1"
  set "PRODUCT_CODE=%~2"
  set "PRODUCT_NAME=%~3"
  set "TARGET=%~4"
)
if "%PRODUCT_CODE%"=="" set "PRODUCT_CODE=PRICE_PRODUCT"
if "%PRODUCT_NAME%"=="" set "PRODUCT_NAME=%PRODUCT_CODE%"
if "%TARGET%"=="" set "TARGET=price_wan"
if not exist "%INPUT%" (
  echo [ERROR] 找不到训练数据: %INPUT%
  goto failed
)
if not exist output mkdir output

echo [INFO] 开始训练并导出价格模型...
"%PYEXE%" tools\train_price_service_model.py "%INPUT%" "%PRODUCT_CODE%" ^
  --product-name "%PRODUCT_NAME%" --target "%TARGET%" --target-unit wan ^
  --output "%CD%\output\price_native_bundle.pkl"
if errorlevel 1 goto failed

echo.
echo [PASS] 价格模型训练和导出完成。
echo 模型: %CD%\output\price_native_bundle.pkl
echo 报告: %CD%\output\price_native_bundle.pkl.training.json
echo.
pause
exit /b 0

:failed
echo [FAIL] 价格模型训练或依赖安装失败。
echo.
echo 请检查上面的错误信息；如果包内运行时缺失，请重新解压完整 ZIP。
echo   runtime\python38\python.exe
pause
exit /b 1
