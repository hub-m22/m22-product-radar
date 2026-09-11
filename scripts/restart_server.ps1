# Перезапуск сервера радара: вызывается из веб-интерфейса («Управление» → «Перезапустить радар») отдельным процессом.
param([int]$Delay = 2)
$log = Join-Path $PSScriptRoot '..\data\logs\restart.log'
"$(Get-Date -Format s) start, delay $Delay" | Out-File -Append -Encoding utf8 $log
Start-Sleep $Delay
$procs = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*radar serve*' })
"$(Get-Date -Format s) found: $($procs.ProcessId -join ',')" | Out-File -Append -Encoding utf8 $log
foreach ($p in $procs) { try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; "$(Get-Date -Format s) killed $($p.ProcessId)" | Out-File -Append -Encoding utf8 $log } catch { "$(Get-Date -Format s) kill $($p.ProcessId) failed: $_" | Out-File -Append -Encoding utf8 $log } }
Start-Sleep 2
try { Start-ScheduledTask -TaskName 'M22 Product Radar' -ErrorAction Stop; "$(Get-Date -Format s) task started" | Out-File -Append -Encoding utf8 $log }
catch { "$(Get-Date -Format s) task failed: $_ ; fallback vbs" | Out-File -Append -Encoding utf8 $log; Start-Process wscript.exe -ArgumentList ('"' + (Join-Path $PSScriptRoot 'start_hidden.vbs') + '"') }
