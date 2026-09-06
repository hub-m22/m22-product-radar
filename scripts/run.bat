@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "C:\Users\Adm\Documents\Claude Васильев\m22-product-radar"
echo Запускаю M22 Product Radar. Не закрывайте это окно, пока работаете с радаром.
echo Через несколько секунд откроется браузер. Если не открылся - зайдите на http://127.0.0.1:8022
start "" http://127.0.0.1:8022
"C:\Users\Adm\AppData\Local\Programs\Python\Python312\python.exe" -m radar serve
pause
