@echo off
title M22 Product Radar - обновление данных
set PYTHONUTF8=1
set PYTHONPATH=C:\Users\Adm\Documents\Claude Васильев\m22-product-radar
cd /d "C:\Users\Adm\Documents\Claude Васильев\m22-product-radar"
echo Собираю данные с сайтов M22 и конкурентов, считаю сигналы и отчёт. Это займёт 15-25 минут.
"C:\Users\Adm\AppData\Local\Programs\Python\Python312\python.exe" -m radar pipeline
echo.
echo Готово. Откройте радар кнопкой "Запустить радар" и посмотрите раздел "Отчёты".
pause
