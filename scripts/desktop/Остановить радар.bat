@echo off
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*radar serve*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Радар остановлен.
timeout /t 3 >nul
