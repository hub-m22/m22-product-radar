@echo off
chcp 866 >nul
:: Открывает порт 8022 в брандмауэре Windows для коллег из локальной сети (нужны права администратора)
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Запрашиваю права администратора...
  powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
netsh advfirewall firewall delete rule name="M22 Product Radar 8022" >nul 2>&1
netsh advfirewall firewall add rule name="M22 Product Radar 8022" dir=in action=allow protocol=TCP localport=8022 profile=private,domain
echo.
echo Готово: порт 8022 открыт для локальной сети. Ссылка для коллег: http://%COMPUTERNAME%:8022
pause
