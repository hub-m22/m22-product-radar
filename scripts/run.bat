@echo off
title M22 Product Radar
powershell -NoProfile -Command "try { (Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8022/health -TimeoutSec 3) | Out-Null; exit 0 } catch { exit 1 }"
if %errorlevel%==0 (
  start "" http://127.0.0.1:8022
  exit /b 0
)
echo Запускаю радар в фоне, браузер откроется через 10 секунд...
wscript "C:\Users\Adm\Documents\Claude Васильев\m22-product-radar\scripts\start_hidden.vbs"
timeout /t 10 /nobreak >nul
start "" http://127.0.0.1:8022
