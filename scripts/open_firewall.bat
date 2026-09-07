@echo off
chcp 866 >nul
:: 1) открывает порт 8022 в брандмауэре Windows (для всех профилей сети),
:: 2) возвращает трафик офисной сети 192.168.150.x на Wi-Fi, а не в VPN-туннель AmneziaVPN
:: (нужны права администратора)
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Запрашиваю права администратора...
  powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
echo.
echo [1/2] Брандмауэр: разрешаю входящие на порт 8022...
netsh advfirewall firewall delete rule name="M22 Product Radar 8022" >nul 2>&1
netsh advfirewall firewall add rule name="M22 Product Radar 8022" dir=in action=allow protocol=TCP localport=8022 profile=any
echo.
echo [2/2] Маршрут: офисная сеть 192.168.150.x через Wi-Fi (в обход VPN)...
for /f "tokens=*" %%i in ('powershell -NoProfile -Command "(Get-NetIPConfiguration | Where-Object { $_.IPv4Address.IPAddress -like '192.168.150.*' } | Select-Object -First 1).InterfaceIndex"') do set IFIDX=%%i
if "%IFIDX%"=="" (
  echo   Wi-Fi адаптер с адресом 192.168.150.x не найден - маршрут не добавлен.
) else (
  route delete 192.168.150.0 mask 255.255.255.128 >nul 2>&1
  route delete 192.168.150.128 mask 255.255.255.128 >nul 2>&1
  route -p add 192.168.150.0 mask 255.255.255.128 0.0.0.0 if %IFIDX% metric 1
  route -p add 192.168.150.128 mask 255.255.255.128 0.0.0.0 if %IFIDX% metric 1
)
echo.
echo Проверка: доступен ли радар по адресу компьютера в сети...
powershell -NoProfile -Command "$r = Test-NetConnection -ComputerName 192.168.150.221 -Port 8022 -WarningAction SilentlyContinue; if ($r.TcpTestSucceeded) { Write-Host '  OK: порт 8022 отвечает по 192.168.150.221' } else { Write-Host '  Порт пока не отвечает. Если включён AmneziaVPN - выключите его и запустите этот файл ещё раз.' }"
echo.
echo Ссылка для коллег: http://%COMPUTERNAME%:8022  (или http://192.168.150.221:8022), пароль в .env
pause
