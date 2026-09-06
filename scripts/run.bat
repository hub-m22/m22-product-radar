@echo off
title M22 Product Radar
set PYTHONUTF8=1
set PYTHONPATH=C:\Users\Adm\Documents\Claude Васильев\m22-product-radar
cd /d "C:\Users\Adm\Documents\Claude Васильев\m22-product-radar"
if errorlevel 1 (echo Не найдена папка радара: C:\Users\Adm\Documents\Claude Васильев\m22-product-radar & pause & exit /b 1)
echo Запускаю M22 Product Radar. НЕ ЗАКРЫВАЙТЕ это окно, пока работаете с радаром.
echo Браузер откроется через 8 секунд. Адрес радара: http://127.0.0.1:8022
start "" /min cmd /c "timeout /t 8 /nobreak >nul & start "" http://127.0.0.1:8022"
"C:\Users\Adm\AppData\Local\Programs\Python\Python312\python.exe" -m radar serve
echo.
echo Радар остановлен. Если выше есть ошибка - пришлите её текст.
pause
