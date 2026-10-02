@echo off
chcp 866 >nul
cd /d "C:\Users\Adm\Documents\Claude Васильев\m22-product-radar"
echo Отправка изменений радара на GitHub...
git push origin master
echo.
echo Готово. Нажмите любую клавишу.
pause >nul
