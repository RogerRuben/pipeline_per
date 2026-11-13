@echo off
setlocal
cd /d "%~dp0"
if "%~1"=="" (
  echo 用法: TRAIN_PRICE_ONLY.bat optuna_v3 input\products.xlsx
  echo 可选版本: optuna_v2 optuna_v3 optuna_v4 optuna_v5 optuna_v6 optuna_v7
  echo 只训练价格模型，不生成整套软件。
  exit /b 1
)
set BACKEND=%~1
set INPUT=%~2
if "%INPUT%"=="" set INPUT=input\products.xlsx
runtime\python38\python.exe -m automl_pipeline.product_price_%BACKEND% --data "%INPUT%"
exit /b %ERRORLEVEL%
