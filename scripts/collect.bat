@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "C:\Users\Adm\Documents\Claude Васильев\m22-product-radar"
echo Собираю данные с сайтов M22 и конкурентов, считаю сигналы и отчёт. Это займёт 15-25 минут.
"C:\Users\Adm\AppData\Local\Programs\Python\Python312\python.exe" -m radar pipeline
echo.
echo Готово. Откройте радар кнопкой "Запустить радар" и посмотрите раздел "Отчёты".
pause
