@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
"%~dp0runtime\python38\python.exe" -X utf8 "%~dp0tools\upgrade_product_v6.py"  %*
if errorlevel 1 (
  if not defined PRODUCT_NO_PAUSE pause
  exit /b 1
)
endlocal
